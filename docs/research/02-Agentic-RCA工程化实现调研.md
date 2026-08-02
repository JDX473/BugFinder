# 基于 LLM 的 Agentic RCA（根因定位）系统工程化实现调研报告

**面向对象**：微服务团队技术负责人
**调研时间**：2026-08
**范围**：agent 编排框架、RCA Agent 与日志/指标/链路数据源的交互、RAG 应用、工程落地细节（故障输入、token 管理、可靠性、输出格式）、现有开源/商业 LLM-RCA 项目清单、端到端技术选型
**说明**：本报告中来自商业产品官方宣传的准确率/能力描述均标注"厂商口径"；第三方独立评估单列。学术论文给出年份，供检索。

---

## 1. 执行摘要（TL;DR）

- **本质判断**：Agentic RCA 的核心不是"让模型读日志然后猜"，而是把**故障排查流程本身工程化**——用确定性框架约束 agent 的排查动作（查拓扑→查链路→查日志→查指标→查变更→下结论），让 LLM 只负责推理与查询生成，证据与校验交给工具与规则。业界与学术界共识是：**"LLM + 工具 + 确定性校验层"的混合架构**，而不是纯"问一个很大的模型"。
- **编排框架**：生产级 RCA 多轮工具调用场景，业界事实标准是 **LangGraph**（显式状态机 + checkpoint + 人机回环 + LangSmith 可观测）；**OpenAI Agents SDK** 适合快速原型；**Claude Agent SDK** 适合以 Claude 模型 + MCP 生态为主、需要开箱即用工具集的团队；**AutoGen 已进入维护模式**，不建议新项目选型。**MCP 协议是"把可观测性系统接给 agent"的当前事实标准**。
- **数据源交互**：日志走"LLM 生成查询（KQL/Lucene/ES DSL）+ 执行校验 + 命中再精读"；指标走"text2PromQL（RAG 带指标元数据）+ 执行校验"，数值理解优先用确定性异常检测/时序基础模型而非让 LLM 硬读数字；Trace 走"traceId 关联 → 还原 span 调用树 → 逐 span 排查"的结构化流程。
- **可靠性**：幻觉缓解四件套——结构化输出 schema 校验、强制证据引用、确定性校验层、human-in-the-loop 门控与失败回退。评估用 RCAEval / AIOpsLab / OpenRCA / RCA-Bench 等公开基准 + 自有历史故障回归集。
- **一个清醒认知**：即使在公开基准 RCAEval 上，最佳学术 LLM agent 方法 Top-1 也只有约 42%（GALA）；经典方法（Nezha ~9%、TraceRCA ~13%）更低。**Agentic RCA 的当前定位是"加速器/初筛器"，产出高质量草稿 + 证据链，人来做最终决策**，而非全自动确诊。
- **推荐栈（详见第 8 节）**：数据接入用 OTel + Prometheus + 日志系统 + 链路系统 + 变更/部署事件源；检索层用 ES/OpenSearch + Prometheus + 向量库；agent 层用 LangGraph + MCP 可观测服务器 + ReAct 排查工具；LLM 用 Claude 或国产旗舰（Qwen/DeepSeek），小任务下放小模型；报告层用结构化 schema（证据链 + 置信度 + 时间线 + 修复建议）。

---

## 2. 总览：Agentic RCA 在 AIOps 版图中的定位

### 2.1 传统 AIOps 与 LLM Agentic RCA 的分工

| 维度 | 传统 AIOps（告警关联/异常检测） | LLM Agentic RCA（排查/根因） |
|---|---|---|
| 时间点 | 告警产生**之前**：降噪、聚簇、相关性 | 告警产生**之后**：主动收集证据、推理根因 |
| 手段 | 统计聚类、时序异常检测、关联规则、依赖拓扑 | LLM 多轮推理 + 工具调用（查日志/指标/链路/变更） |
| 输出 | 异常簇、相关事件分组 | 根因结论 + 证据链 + 修复建议 + 置信度 |
| 代表 | Splunk ITSI、BigPanda、PagerDuty AI、Davis 因果层 | Datadog Bits AI SRE、Davis CoPilot、LangGraph RCA 系统 |

2026 年市场已普遍区分两个品类：**"AI triage 助手"**（PagerDuty AI、FireHydrant AI，目标降低告警疲劳）与 **"AI 根因 agent"**（Datadog Bits AI SRE、New Relic Grok、Dynatrace Davis，目标 time-to-innocence）。Agentic RCA 属于后者，且通常是**分层接入**：传统 AIOps 负责把 100 条告警压成 1 个"故障事件"，Agentic RCA 负责对这个事件展开调查。

### 2.2 一套典型的 Agentic RCA 工作流

```
告警/变更/异常 → 故障事件(Incident)封装 → Agent 编排(计划-执行)
   ├─① 场景认知：拓扑图/服务依赖/变更事件 → 缩小排查范围
   ├─② 链路排查：traceId → span 调用树 → 定位慢/错节点
   ├─③ 日志精读：按服务/时间窗/错误模式 检索日志 → 提取异常
   ├─④ 指标验证：text2PromQL → 异常/拐点 交叉验证
   ├─⑤ RAG 参考：历史工单/runbook/已知问题 → 匹配已知原因
   ├─⑥ 因果推理：假设 → 证据校验 → 打分 → 收敛
   └─⑦ 报告生成：结构化输出(根因/证据链/置信度/时间线/修复建议)
        ↓
     人机确认 → 修复动作(可选自动化) → 复盘入库(postmortem) → 反馈回知识库
```

这个流程与多个生产系统一致：阿里云 STAROps、Datadog Bits AI SRE、腾讯 FastReject。**模式共性**：先用确定性手段（拓扑、依赖图、变更）圈定范围，再用工具收集证据，最后 LLM 做假设与收敛，人在关键节点把关。

### 2.3 关键事实提醒

- **"微软 AutoRCA"存在名称混淆**：微软的实绩是论文《Exploring LLM-Based Agents for Root Cause Analysis》（Roy et al., **FSE Companion 2024**），ReAct agent + 检索工具（BM25 + Sentence-BERT），用微软内部约 10.7 万真实生产事件评估。名为 **AutoRCA** 的论文实为国防科技大学团队（**APSEC 2025**）的图序列多模态 RCA 方法，与微软无关。

---

## 3. Agent 编排框架对比

### 3.1 框架清单与定位

| 框架 | 作者/维护 | 心智模型 | 生产成熟度 | 2026 状态 |
|---|---|---|---|---|
| **LangGraph** | LangChain，v1.0（2025-10） | 显式状态机（节点/边/条件路由/checkpoint） | 最高：Klarna/Uber/LinkedIn/Elastic 生产使用 | 活跃 |
| **OpenAI Agents SDK** | OpenAI（2025-03 起） | 极简：Agent/Runner/Handoff/Guardrail/Trace | 中：pre-1.0 | 活跃，行业"基准模板" |
| **Claude Agent SDK** | Anthropic | agent loop 内置；原生工具；MCP 集成最深 | 中高 | 活跃 |
| **AutoGen** | 微软 | 多智能体对话 | 中 | **已维护模式**；新项目 → Microsoft Agent Framework（MAF）；社区分叉 AG2 |
| **CrewAI** | CrewAI 公司 | 角色化 Crew/Process | 中：原型最快 | 活跃 |
| **Google ADK** | Google | 多 agent 状态机（OpenTelemetry 原生） | 中高 | 活跃 |
| **自研 ReAct loop** | 团队自己 | while loop + tool call 解析 | 视团队而定 | 最可控，但需自行处理状态/重试/可观测 |

### 3.2 对"RCA 半结构化工具调用 + 多轮推理"场景的优劣

**LangGraph** —— 推荐首选。
- 优：显式状态机与 RCA 的"分步排查"天然匹配（AWS sample-ai-investigation-demo 就用 LangGraph 状态机，决策节点分支出 Trace Path / Resource Path）；内置 checkpoint 支持断点续跑与 time-travel 重放；人机回环成熟；token 开销在主流框架中最低（Swarms 对比：同任务约 22k token、质量 8.5；AgentMail 2026-07 实测 LangGraph 输入 token 最低且可靠性满分）；LangSmith 提供完整 trace 可观测。
- 劣：学习曲线最陡；简单场景"杀鸡用牛刀"。

**OpenAI Agents SDK** —— 快速原型/OpenAI 栈。
- 优：极简（~16 行跑通 tool-calling agent），自带 guardrail 与 tracing。
- 劣：无内置状态持久化；handoff 是线性链而非任意图拓扑；pre-1.0 API 变动。

**Claude Agent SDK** —— Claude 生态团队的"开箱即用"。
- 优：MCP 集成最深；内置权限系统与 14+ 生命周期钩子（`PreToolUse` 等，适合给写工具做安全门）；一等公民 subagent（上下文隔离）。Anthropic Cookbook 有 "The observability agent" 与 "The site reliability agent" 两个高度相关示例。
- 劣：**锁死 Claude 模型**；上下文开销是"整套 harness"（AgentMail 实测约 35k 输入 token/run 的缓存读开销，延迟 8.5s vs 竞品 2.2–2.5s）。

**AutoGen / MAF** —— 建议仅存量沿用。多智能体对话模式 token 开销最大（每次重读全部对话历史，"re-read tax"）。

**CrewAI** —— 原型/角色化团队最快，但 Sequential 模式逐个执行偏慢；生产级 RCA 少用。

**自研 ReAct loop** —— 适合"只做固定 3-5 个工具、流程稳定"的最小实现；一旦需要多步回溯、并发调查、人机回环、长流程持久化，自研成本陡增。

**MCP（Model Context Protocol）** —— 协议层，与编排强互补。暴露 tools/resources/prompts 三类原语，走 stdio 或 Streamable HTTP。RCA 场景解决"agent 如何接入 ES/Prometheus/Jaeger/告警系统"。2026 年 MCP + A2A 纳入 Linux Foundation 治理。

### 3.3 给 RCA 场景的框架决策建议

1. **首选 LangGraph**：RCA 是"长流程、有分支、有重试、要人机回环、要可观测"的状态机问题。
2. 团队深度绑定 Claude 且流程较线型，可用 **Claude Agent SDK**，用 `PreToolUse` 钩子保护写操作。
3. 只想要最小 demo 验证思路，用 **OpenAI Agents SDK**。
4. 无论选哪个，**工具层统一走 MCP 服务器**，保证未来可换编排框架而不换数据接入。

### 3.4 可观测性 MCP 服务器生态（可直接接入的工具边界）

- **Grafana 官方 MCP 服务器**：按域暴露 dashboards/folders/datasources（Prometheus、Loki、InfluxDB、ClickHouse、CloudWatch、Elasticsearch、Pyroscope）/alerting/incidents/Sift/OnCall/annotations。
- **@moebiusx/otel-mcp-server**：约 110 个工具/25 个 skill 插件，覆盖 Jaeger/Zipkin/Tempo/SkyWalking（trace）、Prometheus/InfluxDB/OpenTSDB（指标）、Loki/ClickHouse/Graylog（日志）、Elasticsearch、Alertmanager、Grafana、K8s、Pyroscope、服务网格。
- **ThoTischner/observability-mcp**："统一可观测网关"，单端点接任意后端，内置跨信号异常检测（median/MAD、趋势、健康评分），工具含 query_metrics/query_logs/get_service_health/detect_anomalies/get_topology/get_blast_radius。
- **lgtm-mcp**（adarshba/lgtm-mcp）：Grafana 栈单二进制，"意图级"工具（investigate_latency_spike/find_errors/trace_slow_requests/correlate_error_to_trace/summarize_service_health），**LLM 不直接见 PromQL/LogQL/traceId**，工具返回结构化摘要——"半结构化工具调用"的理想形态。
- **cloud-native-mcp-server**（mahmut-Abi）：K8s 优先，381 个工具，含 Kibana/Grafana/Prometheus/Loki/Elasticsearch/Jaeger/OpenTelemetry/Helm/ArgoCD/Sentry。
- **autotel-mcp**：OTel trace/metric/log 调查，工具含 search_traces/find_root_cause/correlate/check_slos。

> 结论：MCP 已是 RCA agent 与可观测后端之间的"标准接线"。刻意设计**高内聚、意图级、返回结构化结果的工具**（而不是把裸查询语言塞给 LLM），是业界反复强调的最佳实践。

---

## 4. RCA Agent 与数据源的交互

### 4.1 日志：让 LLM 查日志的四种方式

**4.1.1 LLM 生成查询 DSL + 执行校验（业界主流）**
- **NL2KQL**（Microsoft, KDD 2024）：首个端到端 NL→Kusto Query Language 框架。pipeline = 语义数据目录（LLM 生成 schema 摘要 + 向量化）→ Schema Refiner（按语义相关度裁剪 schema）→ Few-shot Selector → Prompt Builder（CoT + schema + 语法指南）→ Query Refiner（用官方 parser 校验/修复）。去掉 few-shot 平均执行分掉 55%。后续工作（2025）用 SLM + LoRA 精调 + LLM judge 校验，token 成本低至 GPT-5 的 1/10。
- **AWS Prescriptive Guidance 模式**：NL → Query DSL for OpenSearch/Elasticsearch，few-shot 生成 + 执行校验。
- **DocKit**：开源的 agentic 桌面客户端，NL → ES DSL，自带 schema 检查、权限门、凭据不暴露给 LLM。

**4.1.2 向量检索/混合检索（语义召回）**：日志文本向量化 + BM25 混合（RRF 融合 + 重排）。实践中混合检索常用于知识库，日志本体排查仍以 DSL 为主。

**4.1.3 关键词检索 + LLM 精读（最省 token）**：先用确定性手段（错误码、字段过滤、正则、时间窗）粗筛，再让 LLM 精读少量命中行。

**4.1.4 日志专用小模型做异常提取**：日志先经异常检测/聚类，把"异常日志簇"而非原始洪流喂给 LLM。

> 实践建议：**"NL→查询 DSL→执行→命中日志→再精读/摘要"**是最优路径。关键：给 LLM 提供字段 schema + 少样本示例；用解析器做查询校验；执行结果先确定性筛选再喂 LLM。

### 4.2 指标：LLM 如何"理解"时序数据

**4.2.1 不要把裸数字序列扔给 LLM**，业界通行做法分四层：

1. **确定性异常检测先行**（推荐首选）：MAD/3σ/趋势分解先算出"哪个指标、哪个时间窗、什么形态的异常"，把结论作为文本喂 LLM。
2. **text2PromQL + 执行校验**：
   - **PromAssistant**（arXiv 2024）：text-to-PromQL + 指标知识图谱；GPT-4-Turbo 查询准确率 69.1%；去掉指标知识/组件知识分别掉到 27.8%/12.2%——"指标元数据 RAG"是命门。
   - **阿里云 PromQL Copilot**（2025）：NL→PromQL，指标知识库 RAG + 查询改写 + 执行验证；已上线 CloudMonitor 与可观测性 MCP。
   - 其他：prometheus-rag、SODA ts-ai-agent、VizGenie（LangGraph 多 agent 编排 NL→Grafana dashboard）。
3. **时序基础模型**：**TimesFM**（Google, ICML 2024）、**Chronos**（Amazon, ICML 2024）、**Time-LLM**（ICLR 2024）、**LLMTime**（2023）。关键结论：**通用 LLM 在纯数值预测上打不过专用时序基础模型**；LLM 的正确定位是"解释/编排/报告层"。
4. **text2SQL（时序表）**：指标落在大数据表/OLAP 时走 text2SQL。

> 实践建议：**"确定性异常检测出摘要 + text2PromQL 查询"双通道**。LLM 不做数值计算，只做关联与判断。

### 4.3 Trace/链路：只有 traceId 时怎么组织排查

链路是 RCA 中"最结构化、最可靠"的信号。排查方法论：

1. **traceId 关联日志**：从一条错误日志/慢请求拿 traceId，去链路系统取完整 trace，再按 traceId/spanId 反查各节点日志（前提：日志系统对 traceId 建索引）。
2. **还原调用链**：用 span 父子关系 + 服务名重建调用序列，标注每跳耗时、状态码、错误。
3. **定位耗时/错误点**：对比"正常基线 span 耗时分布"与"故障期 span 耗时"；注意**错误可能在上游被下游拖垮**，需区分"表现异常的节点"与"因果源"。
4. **依赖图辅助**：trace 长期聚合得服务依赖图（SDG），先圈定受影响子图（爆炸半径）再逐点深挖。

代表性方法/基准：
- **TraceRCA**（WWW 2023）与 **MicroRank**：经典基于 trace 的 RCA，在 RCAEval 上 Top-1 仅 13.19%/更低，说明"纯 trace 特征法"上限有限。
- **RCLAgent**（2025）：多 agent "Recursion-of-Thought"，按 trace 图拓扑把每个 span 交给独立 agent 并行递归推理；RCAEval RE2-OB 上 R@1 56.67%。
- **Nezha**（FSE 2023）：自建数据集很高，但 RCAEval 上仅 ~9% Top-1——**"自建基准高分 ≠ 迁移能力"**。
- **CausalRCA**（2025）：时序因果候选图 + RAG 接地 LLM 推理，DeathStarBench Top-1 86%（厂商口径）。
- **PRAXIS**（2025）：LLM 在服务依赖图 + 程序依赖图上做结构化遍历。

> 实践建议：**链路排查必须是"结构化流程 + 确定性基线对比"，LLM 负责叙事与假设**。核心前提：日志系统对 traceId 建索引、链路系统按 service 聚合 span 耗时基线。按 span 粒度拆给子 agent 并行分析再汇总（RCLAgent 范式）是控制上下文的有效手段。

---

## 5. RAG 在 RCA 中的应用：知识库如何辅助根因定位

### 5.1 检索什么
- 历史工单/事件（最高价值）、runbook/SOP、已知问题库、变更/发布记录、故障模式库/知识图谱。

### 5.2 怎么切分与检索
- 按"问题症状 + 根因 + 处置"结构化切块，带元数据；**混合检索**（BM25 + 稠密向量，RRF 融合 + cross-encoder 重排），RRF + 重排可把 MRR 从 0.16 推到 0.75。
- Top-k 注入 top 3–10 条相关历史事件，明确要求"仅把历史用于参考、必须用当前证据验证"。

### 5.3 与当前故障信息如何融合
- 融合时机："假设生成"阶段与"修复建议"阶段各用一次，不一开始灌全文。
- 多 agent 分工（MA-RCA 范式）：Retrieval Agent 用 RAG 把假设接地，Validation Agent 用运行时数据验证假设。
- 证据约束：知识库检索结果与实时数据都以"证据 ID"形式引用。
- 闭环回写：RCA 完成后 postmortem 自动入库，形成持续进化的知识库。

> 实践建议：KB-RAG 的核心价值是**用历史压缩搜索空间**。注意知识治理——脏 KB 比没 KB 更危险。

---

## 6. 工程落地细节

### 6.1 故障触发与输入：如何定义一次"故障事件"给 agent

"故障事件"输入应是一个**统一封装的结构化对象**（incident envelope），而非一段自然语言：

```json
{
  "incident_id": "INC-20260802-001",
  "trigger": {"type": "alert_rule", "alert_id": "AR-8812", "source": "Prometheus", "rule": "error_rate>5%", "fired_at": "2026-08-02T21:14:00Z"},
  "affected_scope": {"service": ["checkout", "payment"], "namespace": "prod", "topology_snapshot": "dep_graph_snapshot_id"},
  "time_window": {"start": "2026-08-02T21:00:00Z", "end": "2026-08-02T21:30:00Z"},
  "linked_telemetry": {"metrics": ["P99_latency", "error_rate"], "logs": {"indices": ["prod-app-*"]}, "traces": ["sample_trace_ids"]},
  "changes": {"deployments": [...], "config_changes": [...], "flagged": true},
  "kb_hits": {"similar_incidents": ["INC-2025-11-003"], "runbook": "runbook/checkout-slow.md"}
}
```

要点：触发源应是"告警 + 变更事件 + 异常检测"三通道；接入时先确定性拉取初始上下文；**拓扑快照**尤其重要（保证可复现、可审计）。

### 6.2 上下文窗口 / token 管理

核心原则："**fewer, better-chosen tokens beat a bigger pile of them**"。手法按"从便宜到贵"排序：
1. 预过滤（确定性优先）；2. 降采样/聚合；3. 分步检索（工具化拉取）；4. 子 agent 委派 + 汇总；5. 总结树/Map-Reduce（最后手段）；6. 压缩工具（LLMLingua 最高 20× 压缩）；7. 警惕 "lost in the middle"（关键指令放首尾）；8. Prompt caching（静态前缀放前面并保持稳定）。

### 6.3 可靠性：幻觉缓解、失败回退、评估

**幻觉缓解四件套**：
1. **结构化输出 + schema 校验**（JSON Schema/Zod，不合规即重试）；
2. **强制证据引用**（每个论断附证据 ID，无工具验证不得下结论，chain-of-custody）；
3. **确定性校验层**（查询可执行、时间先验、跨信号一致性、counterfactual 因果检查）；
4. **human-in-the-loop 门控**（置信度 < 阈值转人工）+ 失败回退（重试 → 换小模型 → 换关键词检索 → 交人工）。

**评估 agent 效果**（必须分层）：
- 公开基准：**RCAEval**（735 故障/11 类，Top-1/Top-3/Top-5）、**AIOpsLab**（MLSys 2025，在线多任务）、**OpenRCA**（ICLR 2025，335 故障/68GB，最佳约 54% F1/38% acc）、**阿里云 RCA-Bench**（RCA-100，定因/定界/过程三维）。
- 自有回归集：历史故障（标注根因 + 完整遥测）沉淀为回归测试集。
- 过程质量 + 可靠性指标：工具调用失败率、查询语法合法率、证据引用完整率、幻觉率、token/时长、HITL 打断率、置信度校准。
- 清醒认知：**当前 Agentic RCA 可靠上限有限（最优 ~42% Top-1）**，生产落地的正确姿势是"AI 出草稿 + 人复核"。

### 6.4 输出形态：RCA 报告的标准格式

业界已收敛出一套"证据链 + 结构化字段"的报告 schema（综合 AI Reliability Copilot 的 Zod 9 节、Siamese/Casefile、阿里 STAROps 证据链 RCA）：

```json
{
  "incident_id": "INC-20260802-001",
  "summary": "一句话结论",
  "severity": "SEV1|SEV2|SEV3",
  "root_causes": [
    {"hypothesis": "根因陈述", "confidence": 0.87, "evidence_refs": ["E-M12", "E-L05"], "reasoning": "推理路径简述", "status": "confirmed|likely|possible|ruled_out"}
  ],
  "evidence_chain": [{"evidence_id": "E-M12", "type": "metric", "source": "prometheus/error_rate", "timestamp": "...", "content": "checkout 错误率 0.1%→45%", "confidence_note": "high"}],
  "timeline": [{"t": "21:00", "event": "部署 v2.4.1", "type": "change", "uncertainty": false}],
  "remediation": [{"action": "回滚 checkout 到 v2.4.0", "owner": "checkout-owner", "priority": "P0", "risk": "低", "rollback": "...", "verification": "..."}],
  "ruled_out": ["被排除的假设及依据"],
  "open_questions": ["需人工确认的点"],
  "confidence_overall": 0.85,
  "llm_audit": {"model": "...", "tool_calls": [...], "steps": [...]}
}
```

要点：**区分"已证实/可能/已排除"**；时间线带不确定性标记；修复建议带 owner/优先级/风险/回滚/验证；保留审计轨迹。

---

## 7. 现有开源/商业 LLM-RCA 项目清单

### 7.1 开源/学术项目

| 项目 | 定位 | 链接/线索 |
|---|---|---|
| Exploring LLM-Based Agents for RCA（微软，2024） | ReAct agent + 双路检索，10.7 万生产事件评估 | Roy et al., FSE Companion 2024 |
| OpenRCA（微软，ICLR 2025） | LLM 根因识别基准（335 故障/68GB） | microsoft.github.io/OpenRCA/ |
| AIOpsLab（微软，MLSys 2025） | 在线环境多任务 agent 基准 | github.com/Microsoft/AIOpsLab |
| RCAEval | RCA 公开基准（735 故障，15+ 基线） | github.com/phamquiluan/RCAEval |
| RCLAgent（2025） | 按 trace 图拓扑多 agent 递归推理 | github.com/LLM4AIOps/RCLAgent-V2 |
| Nezha（FSE 2023） | 多模态事件图 + GNN RCA | github.com/IntelligentDDS/Nezha |
| TraceRCA（WWW 2023） | trace 特征 RCA + 数据集 | github.com/NetManAIOps/TraceRCA |
| AWS sample-ai-investigation-demo | LangGraph 事件调查骨架 | github.com/aws-samples/sample-ai-investigation-demo |
| RunbookAI | 把 RCAEval 转成 agent eval fixture | github.com/Runbook-Agent/RunbookAI |
| GALA（多伦多大学，2025） | 图增强多模态 + LLM 迭代推理 | arXiv 2508.12472 |
| CausalRCA（2025） | 因果图 + RAG 接地推理 | IEEE 11566729 |
| PromAssistant（2024） | text-to-PromQL + 知识图谱，69.1% | arXiv 2403（text-to-PromQL） |
| NL2KQL（微软，KDD 2024） | NL→Kusto Query Language | arXiv 2404.02933 |

### 7.2 商业产品

| 产品 | 厂商 | 定位/形态 | 备注 |
|---|---|---|---|
| Davis AI / Davis CoPilot | Dynatrace | 确定性因果 AI（Smartscape 拓扑）+ 生成式 CoPilot | 因果引擎自 2017 生产；受采集遥测边界限制 |
| Bits AI SRE（2025-12 GA） | Datadog | 跨 logs/metrics/traces/deploys/code 自主调查，假设树 + Action Catalog | 只覆盖已接入 Datadog 的遥测 |
| New Relic Grok | New Relic | AI 调查 + 全栈相关性 | 厂商口径 |
| Splunk ITSI + AI Assistant | Splunk | 事件预测/关联 + AI 助手 | 与 Splunk 搜索生态绑定 |
| STAROps | 阿里云 | AI 原生全域智能运维平台（2026-05）：UModel + 证据链 RCA 引擎 + RCA-100 基准 + MCP 开放 | 开源 UModel 与 RCA-100 |
| PromQL Copilot | 阿里云 | NL→PromQL（RAG + 执行校验） | 已上线 CloudMonitor 与可观测 MCP |
| 华为云运维智能体 | 华为云 | 全栈故障数字孪生 + 故障模式库 + 三级自愈 | 厂商口径：诊断效率 +300% |
| 腾讯 FastReject | 腾讯 WXG | 多模态结构化 + Law Agent 经验规则自迭代 + SOP 推理校验 | 公开赛事方案 |
| PagerDuty AI / AIOps | PagerDuty | 告警分类 + war room 摘要 + postmortem 草稿 | 定位是 triage 而非深度 RCA |
| BigPanda | BigPanda | 事件关联/降噪专精 | 厂商口径：90%+ 告警压缩 |
| Moogsoft | Moogsoft | 纯 AIOps：降噪 + 异常检测 + 根因识别 | 老牌 AIOps |

### 7.3 选型视角的一句话评价

- **买商业产品**的前提：已有该厂商的完整遥测栈（否则只买到 60–70% 能力，余下自研补）。
- **自研**的前提：开源基准（RCAEval/AIOpsLab）+ 开源 agent 骨架（AWS demo、LangGraph + MCP）+ 学术方法（RCLAgent/GALA 范式）已足够组装 MVP，核心投入在数据接入质量与评估回归集。

---

## 8. 推荐技术选型（微服务 + 日志系统 + 机器指标 + traceId 场景）

### 8.1 端到端技术栈清单

| 层 | 选型 | 理由 |
|---|---|---|
| 数据接入/埋点 | OpenTelemetry（SDK + Collector）统一 trace/metric/log 关联 | 保证"traceId 关联日志"成立的前提工程 |
| 指标存储/查询 | Prometheus（+ Thanos/Cortex） | 微服务指标事实标准；text2PromQL 生态成熟 |
| 日志存储/检索 | Elasticsearch / OpenSearch（或 Loki） | ES 生态 NL2DSL 工具最成熟 |
| 链路存储/查询 | Jaeger / Tempo | span 调用树还原 + 基线耗时对比 |
| 拓扑/依赖 | 服务依赖图（trace 聚合）+ K8s 拓扑 | 缩小排查范围、爆炸半径 |
| 变更/部署事件 | 发布系统 + CI 事件流 → Kafka | 变更是最常见根因 |
| 向量库/KB | Qdrant/Milvus/pgvector + 历史工单/runbook | RAG 用；BM25 + 稠密向量混合 + RRF |
| Agent 编排 | **LangGraph**（首选） | 状态机 + checkpoint + HITL + 低 token 开销 |
| 工具边界 | 自建"意图级"MCP 服务器（包装 ES/Prometheus/Jaeger/告警） | 让 agent 调"查错误率拐点"而非裸 PromQL |
| LLM 策略 | 编排/推理用 Claude 或国产旗舰（Qwen/DeepSeek）；查询生成可用 SLM + judge 校验；时序数值交给 TimesFM/Chronos | 分层：贵模型做因果推理与报告 |
| 确定性预处理 | 异常检测 + 日志聚类 + span 耗时基线 | "LLM 之前"的把关层 |
| 评估/回归 | RCAEval + AIOpsLab + 自有历史故障回归集 | 每次改 prompt/工具跑回归 |
| 报告生成 | 结构化 JSON schema + Zod/JSON Schema 校验 | 机器可消费 + 人工可复核 |
| 可观测 agent 自身 | LangSmith + OTel 打点 | 排查失败/审计/回归的前提 |

### 8.2 架构图（文字版）

```
 [OTel 埋点]──▶[Collector]──┬─▶[Prometheus]────┐
                            ├─▶[ES/OpenSearch]─┼─▶[确定性预处理层]
                            ├─▶[Jaeger/Tempo]──┤   (异常检测/日志聚类/基线对比)
                            └─▶[变更事件/Kafka]─┘         │
                                                         ▼
   [KB: 工单/runbook/已知问题] ──▶ [混合检索 Qdrant+BM25] ──▶ [LangGraph RCA Agent]
                                                                   │  MCP 意图级工具
 [故障事件封装 incident envelope] ────────────────────────────────┤
                                                                   ▼
                    [结构化报告: 根因/证据链/置信度/时间线/修复]
                                    │
                                    ├─ HITL 门控 ──▶ [人类确认/否决]
                                    └─▶ [postmortem 入库 → 知识库闭环]
```

### 8.3 落地路线图建议

1. **第 0 阶段（基础设施）**：打通 OTel → Prometheus/ES/Jaeger；保证 **traceId 贯穿日志索引**；沉淀变更事件流。（决定成败，没有干净关联数据 agent 再强也白搭）
2. **第 1 阶段（确定性 + 基础 agent）**：incident envelope + 确定性预处理；LangGraph 搭"拓扑→链路→日志→指标→变更"固定流程。
3. **第 2 阶段（检索增强 + 推理增强）**：KB-RAG；假设生成/打分/证据引用；text2PromQL；置信度 + HITL。
4. **第 3 阶段（评估与迭代）**：自有历史故障回归集；对每次变更跑评估指标。
5. **第 4 阶段（扩展）**：多 agent 并行调查、自动修复（权限门）、postmortem 回写知识库。

### 8.4 风险与注意

- **别在遥测质量上省钱**：garbage in, garbage out。
- **别追求 100% 自动化**："AI 草稿 + 人复核"是最优人机分工。
- **成本可控**：SLM 做查询/摘要、缓存静态前缀、控制每轮上下文。
- **安全边界**：agent 的"读"工具放开，所有"写"工具必须过权限门 + HITL。

---

## 9. 参考资料与链接汇总（核心）

**框架/协议**
- LangGraph：github.com/langchain-ai/langgraph（v1.0, 2025-10）
- OpenAI Agents SDK：github.com/openai/openai-agents-sdk
- Claude Agent SDK：platform.claude.com（Cookbook 含 "The observability agent"、"The site reliability agent"）
- AutoGen / Microsoft Agent Framework / AG2：microsoft.github.io/autogen / microsoft.github.io/agent-framework
- MCP：modelcontextprotocol.io

**论文/基准**
- Exploring LLM-Based Agents for RCA（Roy et al., FSE Companion 2024）
- OpenRCA（ICLR 2025）：microsoft.github.io/OpenRCA/
- AIOpsLab（MLSys 2025）：github.com/Microsoft/AIOpsLab
- RCAEval：github.com/phamquiluan/RCAEval
- NL2KQL（KDD 2024）：arXiv 2404.02933
- PromAssistant：arXiv（text-to-PromQL + 知识图谱）
- RCLAgent：github.com/LLM4AIOps/RCLAgent-V2
- Nezha（FSE 2023）：github.com/IntelligentDDS/Nezha
- TraceRCA（WWW 2023）：github.com/NetManAIOps/TraceRCA
- GALA：arXiv 2508.12472
- Time-LLM（ICLR 2024）/ LLMTime（2023）/ Chronos（ICML 2024）/ TimesFM（ICML 2024）
- LLMLingua：github.com/microsoft/LLMLingua
- Lost in the Middle（Liu et al., 2023）

**开源项目/工具**
- AWS sample-ai-investigation-demo：github.com/aws-samples/sample-ai-investigation-demo
- RunbookAI：github.com/Runbook-Agent/RunbookAI
- lgtm-mcp：github.com/adarshba/lgtm-mcp
- observability-mcp：github.com/ThoTischner/observability-mcp
- otel-mcp-server：@moebiusx/otel-mcp-server
- 阿里云 RCA-Bench（RCA-100）：sls.aliyun.com/doc/starops/benchmark/rca/
- Grafana MCP：grafana.com/docs/grafana/v13.1/developer-resources/mcp/

**商业产品**
- Dynatrace Davis AI / Davis CoPilot；Datadog Bits AI / Bits AI SRE；New Relic AI / Grok；Splunk ITSI；阿里云 STAROps；华为云运维智能体；腾讯 FastReject；PagerDuty AI；BigPanda；Moogsoft

*（报告完。商业产品能力描述含厂商宣传口径，采购前建议以自有数据 PoC 验证。）*
