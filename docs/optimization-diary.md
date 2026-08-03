# 优化日记（踩坑记录）

> 本文件记录开发过程中**踩过的坑 + 解决办法**，与设计文档（`docs/implementation/`）分开。
> 设计文档回答"模块为什么这么设计"；本日记回答"这个坑是怎么踩的、以后怎么避免"。
> **约定：每次解决完一个坑，在本文件追加一条**（编号递增，按模块归类），不混入设计文档。

---

## 检测 / 路由类

### #1 指标名裸子串误匹配（`rt` 命中 `cart_abandonment_rate`）
- **坑**：场景路由用 `"rt" in metric` 做子串匹配，`cart_abandonment_rate`、`support_ticket_count` 等业务指标被误路由到延迟/资源场景。
- **根因**：子串匹配对短关键词（rt/load/io）无区分度。
- **解决**：指标名按非字母数字分隔符切词后做**精确词比对**（`_METRIC_SEPARATORS`），`rt` 不再命中含它的长词。
- **避坑**：指标名 → 场景/类型的映射一律用词边界匹配，不用 `in` 子串。

### #2 "点数不足"被当"观测到健康"
- **坑**：`_tech_signal_clean` 把 `detect_anomaly` 对点数不足序列返回的 `is_anomaly=False` 当成"该指标健康"，据此误判 business_logic。
- **根因**：点数不足（`< min_points`）是"无数据可判定"，不是"观测到健康"。
- **解决**：加 `_has_sufficient_data` 检查 `detail` 是否含"点数不足"。
- **避坑**：凡依赖"正常"语义的判定，先区分"真正常"和"没数据"。

### #3 LLM 低置信被当权威路由决策
- **坑**：LLM 返回 `confidence: 0.05` 也被当作权威路由结论（进特定场景 SOP）。
- **根因**：路由出口没设置信度门槛。
- **解决**：加 `_LLM_MIN_CONFIDENCE=0.5`，低置信降级 other。
- **避坑**：LLM 判定必须过置信度门槛，低于阈值一律降级确定性兜底。

### #4 LLM 返回 OTHER + 高置信 语义矛盾
- **坑**：LLM 返回 `other` 却带 `confidence: 0.9`，与"other = 低置信兜底出口"矛盾。
- **解决**：LLM 返回 OTHER 时钳制置信度到 0.1。
- **避坑**：枚举语义要一致——兜底出口的置信度恒为低值。

### #5 最早异常不可映射 → 整条指标分支被丢弃
- **坑**：时间最早的可映射异常不存在时，`next(...)` 返回 None，整个指标分支塌掉，不路由。
- **解决**：按时间序遍历全部异常，取**第一个可映射**者作主场景；`earliest_anomaly` 仍记录时间最早者供假设打分。
- **避坑**：多信号路由别因"最强/最早信号不可用"就放弃整条证据分支。

### #6 裸实体词"车门"误判 business_logic
- **坑**：白名单用 `"车门"` 做子串，技术告警（"车门服务灰度发布"）含"车门"被误判业务故障。
- **解决**：白名单只用**业务症状短语**（车门打不开/支付失败），不用裸实体词。
- **避坑**：业务判定用症状短语，实体词在技术告警里太常见。

### #7 假设定位用"列表第一个异常"（真实数据暴露）
- **坑**：`_build_metric_hypotheses` 用 `raw_anomalies[0]` 定位服务，但该列表是**输入序**（非时间/非幅度序）。RCAEval 真实数据下：adservice 故障 ratio 46x，但列表第一个是 checkoutservice 的弱异常 → 假设错指 checkoutservice，Top-3 命中 58.3%。
- **根因**：`route_scenario` 的 `raw_anomalies` 保留输入顺序，`_pick_earliest` 只影响 `earliest_anomaly`。
- **解决**：按服务分组生成假设（Top-3 多样性）+ **幅度最大（|ratio| 最大）的服务优先**。修复后 Top-3 95.2%。
- **避坑**：**永远不要依赖列表顺序**（输入序/字典序都可能）。选"最强"用幅度，选"最可能"用时间，都别用 index 0。**这条是 mock 测试发现不了、只有真实数据能暴露的**——因为 mock 数据都是精心构造的，异常顺序恰好是意图顺序。

---

## 报告 / CLI 类

### #8 `--report` 位置参数被 argparse 当 traceId
- **坑**：`--report "用户反馈车门打不开"` 里，位置文本被解析成 `trace_id` 位置参数，`--scenario` 变 None → 走兜底模板，business_logic 判定失效。
- **解决**：位置参数只在形如 `tr-mock-*` 时才当 traceId；场景文本必须走 `--scenario`。
- **避坑**：CLI 里位置参数和 flag 复用同一字段时，显式约束位置参数的值域。

### #9 假设打分未归一化，Top-3 全撞 1.0
- **坑**：证据基础分（2.5）直接 `min(score, 1.0)` 全撞上限，Top-3 无区分度。
- **解决**：sigmod 压缩 `1/(1+e^-(score-1.6))` 到 [0,1]。
- **避坑**：分数归一化别用硬截断，用有区分度的单调映射。

### #10 时间线被"采集窗证据"污染
- **坑**：整窗的指标检测摘要（time_range = 全程窗）被当离散时间线事件，所有证据挤在同一时刻。
- **解决**：时间线只收编**离散事件**（场景最早异常/trace 错误跳/事件触发），采集窗证据只进证据清单。
- **避坑**：时间线要的是"发生了什么、什么时候"，不是"采集了哪些信号"。

---

## 编排层类（LangGraph）

### #11 条件分支函数被当节点 add_node → 返回值当状态更新报错
- **坑**：`budget_guard` 返回字符串 `"continue"`，但 `add_node` 后返回值被当状态更新，报 `InvalidUpdateError: Expected dict, got continue`。
- **根因**：`add_conditional_edges` 的 `path` 是**路由函数**（只读返回分支名），不是节点（返回 dict 更新状态）。
- **解决**：budget 路由函数不 `add_node`，直接挂在条件边；条件边从源节点出发。
- **避坑**：LangGraph 里"判断走哪条边"用条件边路由函数，"产出状态"用节点，两者分开。

### #12 HITL resume_value 被丢弃，人工答复对调查零影响
- **坑**：`hitl_gate` 里 `interrupt(...)` 的返回值直接丢弃，恢复时 `Command(resume=value)` 投递的答复没消费。
- **解决**：interrupt 返回值写进 `hitl_resume_value`，且支持 `{"trace_id":...}` 补充输入并入 incident。
- **避坑**：interrupt 的返回值 = 恢复时的人工答复，**必须消费**，否则 HITL 是假交互。

### #13 resume 未传 thread_id 静默空跑
- **坑**：恢复时自动生成新 tid（基于恢复时刻），找不到中断线程 → 从零重跑且不产出 report。
- **解决**：`resume_value is not None and thread_id is None` 时抛 `RCAWorkflowError` 强制。
- **避坑**：恢复操作必须显式标识"恢复哪个线程"，自动生成 ID 在恢复场景是错的。

### #14 MemorySaver 的 allowlist 是 no-op
- **坑**：`with_msgpack_allowlist` 传类型对象以为能注册，但 langgraph 4.1.1 默认 serde `_allowed_msgpack_modules=True`（允许所有），该方法短路返回。
- **解决**：承认默认允许所有即可（反序列化警告是未来硬化提示，当前不阻塞），移除误导性的注册代码。
- **避坑**：**工具内部 API 的"默认宽松"配置先实测再设计防御**——别对着源码猜，跑一下看 `_allowed_msgpack_modules` 的真实值。

### #15 数据源通用异常穿透炸全图
- **坑**：`make_trace_node` 只捕 `TraceReconstructionError`，ES 连接失败/RuntimeError 直接炸掉整条工作流。
- **解决**：所有节点捕 `Exception` 写占位证据（RCA-012）。
- **避坑**：降级逻辑捕具体业务异常是错的，通用异常也要占位。

### #16 thread_id 秒级粒度碰撞
- **坑**：`f"inc-{id}-{int(t_start)}"` 秒级粒度，同一 incident 同秒两次 invoke 共用 checkpoint。
- **解决**：加毫秒 + `id(incident)` 随机后缀。
- **避坑**：自动生成 ID 要防同输入同刻碰撞。

---

## 数据 / 环境类

### #17 RCAEval zip 解压嵌套一层目录
- **坑**：zip 内是 `RE1-OB/RE1-OB/{case}/...`，适配器硬拼路径找不到 data.csv。
- **解决**：`rglob` 递归定位 case 叶子（`**/inject_time.txt`），不硬拼层级。
- **避坑**：数据集解压层级未知时，用递归扫描而不是假设固定深度。

### #18 大文件下载被网络反复截断（245MB → 拉不动）
- **坑**：RE2-SS（245MB）下载反复中断（curl 66MB 损坏、urllib 慢爬 2.3MB）。
- **解决**：改选 RE1-OB（31MB）单次拉取成功；下载脚本用 Python urllib（比 curl 单次更可控）。
- **避坑**：大文件在受限网络下先评估替代品；下载优先"够用的小数据"而不是"全量的大数据"。

### #19 zenodo / figshare / GitHub 网络可达性差异
- **坑**：GitHub 000（不通）、figshare 403、zenodo 200——同一个数据源，三个入口三种结果。
- **解决**：先测可达性再选下载通道；zenodo API 直链可下载。
- **避坑**：拉外部数据前先 `curl -sI` 探可达性，别假设 GitHub 一定通。

---

## LLM 接入类

### #20 真实 LLM 在纯指标定位上增量很小（+0.8%）
- **坑**：接入真实 DeepSeek 后，RCAEval 125 cases 对比——Top-3 从 95.2% 只升到 96.0%。
  起初预期 LLM 能显著提升命中率。
- **根因**：RE1 是纯指标数据，规则（幅度优先定位）已把确定性信息榨干，LLM 在"有确定证据
  的定位"上没有增量空间。LLM 真正能补的是**规则覆盖不到的地方**（白名单外业务语义、
  需要推理的复杂调用链）。
- **解决**：接受"确定性优先"架构的正确性——LLM 不该在规则已够的地方刷存在感。
  验证到 LLM 的真实价值在业务语义兜底（`充电桩充不进电` → business_logic，纯规则会落 other）。
- **避坑**：**别用"命中率提升"衡量 LLM 价值**，用"规则覆盖不到的场景数"。纯指标故障的
  命中率是确定性算法的成绩，不是 LLM 的。

---

## 数据适配类（RE2 真实数据）

### #21 数据集文件命名不同（RE1 `data.csv` vs RE2 `metrics.csv`）
- **坑**：RE2-SS 用 `metrics.csv`（含 `simple_metrics.csv`），RE1 用 `data.csv`。适配器硬编码
  `data.csv` 导致 RE2 全部 case 加载失败。
- **解决**：`load_case_csv` 按存在性探测 `data.csv` / `metrics.csv` / `simple_metrics.csv`。
- **避坑**：数据集格式差异按"探测文件名"适配，别假设固定命名。

### #22 真实指标 CSV 有空值（脏数据）
- **坑**：RE2 `metrics.csv` 443 列里有的列有空值（`''`），`float()` 转换抛 ValueError，
  整个 case 检测失败。
- **解决**：`metric_series`/`anomaly_series` 跳过空值/脏数据点。
- **避坑**：真实数据一定有缺失值，解析时跳脏点而非整体放弃。

### #23 RE2 日志全是 info 级，`min_level=warn` 过滤成 0 条
- **坑**：RE2-SS 的 44353 条日志全部是 `info` 级（Go 服务结构日志、access log 不分级），
  `cluster_logs` 默认 `min_level=warn` 把全部日志过滤成 0 个簇。mock 假设"异常日志至少
  warn 级"在真实数据上失效——真实日志的异常靠**内容**（Slow query / 5xx / error）而非 level。
- **解决**：RE2 场景把 `min_level` 降到 `info`，靠噪音黑名单 + 内容识别异常。
- **避坑**：**日志级别分级是 mock 数据的约定，不是真实数据的**。真实系统日志常全 info，
  异常判定必须回到内容信号。

### #24 真实指标名带 k8s 前缀（RE2），`_svc_from_metric` 可能解析错
- **坑**：RE2 指标名如 `catalogue_container-cpu-usage-seconds-total`、`gke-...-node-network-...`，
  `_svc_from_metric` 取第一个 `_` 前的段——`gke-...` 会被解析成 `gke-gke-cluster...`（错），
  但 `catalogue_container-...` 恰好解析成 `catalogue`（对）。
- **解决**：幅度优先定位（上一轮修复）让根因服务的强信号（cpu 614x）压过 node 级弱干扰，
  即使个别服务名解析错，rank1 仍正确（RE2 90 cases Top-3 91.1% 验证）。
- **避坑**：**k8s 指标名的服务提取不能靠简单 split**——真实 Prometheus 指标名带
  container/istio/node/pod 层级，需要按 `{service}_{kind}-{metric}` 的语义解析
  （Phase 2 改进项，当前靠幅度优先兜底）。

### #25 递归子树耗时在真实 trace 上慢到无法批量验证
- **坑**：`verify_re2_trace.py` 最初用递归算每个 span 的子树耗时（O(span×深度)），
  真实 trace 16 万 span/case 下 10 case 超 120s（后台任务跑不完）。
- **根因**：递归对每个 span 遍历子树，真实数据量（16 万）下是 O(n×depth)。
- **解决**：Jaeger 根 span 的 `duration` 已包含整棵调用链耗时，直接取根 span
  duration，O(span) 单遍。全量 90 cases 可跑（命中率不变）。
- **避坑**：**真实数据量下先算复杂度**——mock 数据（几十行）测不出 O(n×depth) 的
  性能问题，16 万行才暴露。能用聚合字段（根 span duration）就别递归。

### #26 慢节点定位在真实 trace 上只有 ~52% 命中（耗时高 ≠ 根因）
- **坑**：RE2-TT 90 cases 里 trace 慢节点定位命中率 52.2%，socket/mem 都约等于
  抛硬币。初版 10 case 90% 是采样偏差（恰好前 10 个都命中）。
- **根因**：耗时高的服务可能是**受害者**（请求堆积/排队）而非加害者——socket 故障下
  `ts-auth-service` 耗时 376844ms 排第一，根因 `ts-travel-service` 只有 21605ms。
  高吞吐服务累计耗时天然高，不是故障信号。
- **解决**：接受"trace 慢节点只能当疑似候选"，架构上靠多信号交叉验证（指标幅度
  优先 91% + trace 候选 + 日志异常簇）弥补，不单靠 trace。
- **避坑**：**单信号定位必须在小样本上怀疑**——10 case 的 90% 会被全量 90 case 的
  52% 打脸。用全量评估，且"命中率"要看够大的样本才有意义。

### #27 LangGraph TypedDict 缺字段 → 节点返回被静默丢弃（指标结果丢失）
- **坑**：`make_scenario_node` 返回 `metric_series` / `metric_anomalies` 两个 key，但
  `WorkflowState` 的 TypedDict **没声明这两个字段**。langgraph 静默丢弃未声明字段
  （不报错），导致步骤 5 的 `make_metrics_node` 读不到指标 → 报告里 `ev-metric`
  变成"失败：无指标序列"占位。**只有浏览器看报告时才暴露**（CLI 的 --report 是
  手动传指标，不走 state）。
- **根因**：langgraph 的 TypedDict schema 是白名单——节点返回未声明字段被忽略，
  不是报错。
- **解决**：`WorkflowState` 补 `metric_series` / `metric_anomalies` 字段声明。
- **避坑**：**langgraph 节点返回的每个 key 都必须先在 WorkflowState 声明**。
  测试要覆盖"节点 A 产出 → 节点 B 消费"的端到端路径（这次 CLI 测试没覆盖
  state 传递，只有 Web 页暴露）。

### #28 浏览器验证暴露"修改后未重启服务"（旧代码仍在跑）
- **坑**：改完 `state.py` 后浏览器仍显示失败证据——因为 uvicorn 无热重载，
  跑的还是修改前的进程。
- **解决**：改代码后 `preview_stop` + `preview_start` 重启服务再验证。
- **避坑**：浏览器验证前确认服务加载的是最新代码（无热重载时手动重启）。

### #29 Web 层漏了"手动触发调查"入口（RCA-002 未落地到页面）
- **坑**：做了 Web 报告页，但只支持"点预设事件按钮"。用户问"为啥不能给 Agent 发消息"——
  暴露了 `workflow.invoke_manual()` 早就实现、但 Web 层没暴露手动入口的断层。
- **根因**：Web 层按"事件列表 → 触发"的告警视角设计，漏了 PRD RCA-002 的
  "手动触发/补录"视角（自由文本发起调查）。
- **解决**：API 加 `POST /api/investigate`（free_text + 可选 service/trace_id），
  前端加"给 Agent 发消息"输入框。实测：输入"用户反馈支付失败"→ error_rate_spike
  + 业务上下文 支付/支付失败。
- **避坑**：**做产品层时先对 PRD 需求清单逐条核对**——工作流层实现了的
  （invoke_manual），不代表产品层暴露了。需求覆盖要按 PRD 编号自查（RCA-001~004）。
