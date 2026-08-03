# 场景路由实现细节

> 模块：`app/pipeline/scenario_router.py`
> 关联 PRD：§6.2（步骤 2 场景认知）、§5.2（指标接入）、§8.1（IncidentEvent）
> 状态：Phase 1 首个模块，已落地

## 解决什么问题

把一次事件判定为 **6 类场景之一**，决定后续走哪套 SOP 剧本。
场景不是按"症状叫什么"分类，而是按"这类故障该走哪套排查剧本"分类——
**桶 = 可执行的 SOP 剧本**，这就是场景路由决定工作流如何分叉的原因。

**为什么新增 `business_logic`（方案 A，用户确认）**：
技术形态信号（错误率/延迟/资源/可用性）能覆盖"机器/接口怎么坏了"，
但覆盖不了**技术信号干净、功能却不对**的业务/功能类故障——
如"车门打不开"：日志无错误、指标全绿，但业务规则拒绝（行程未开始）。
这类故障之前只能落 `other` 当"不知道咋查"兜底，是浪费。
新增第 6 类 `business_logic` 走**业务剧本**（业务规则/配置开关/数据状态/业务文档）。

## 核心方法：四优先级判定（确定性优先，LLM 兜底）

| 优先级 | 触发条件 | 判定 | 置信度 |
|---|---|---|---|
| **1 指标** | 有异常指标且指标名可映射 | 指标名 → 场景映射；多指标取**最早异常**者为主（时间先验） | 0.9 |
| **2 业务** | 技术信号干净 + 命中业务白名单 | `business_logic`，业务上下文从白名单抽 | 0.85 |
| **3 LLM** | 以上都无法确定 + 提供了 LLM | ask_json 强约束，只能从 6 枚举选，带置信度 | LLM 给的 |
| **4 other** | LLM 也判不出 / 无 LLM | `other` 兜底（通用剧本，强制人工介入） | 0.1 |

### 1. 指标证据（主信号）

- 基于 MAD/3σ 检测器的 `MetricAnomaly` 输出，**指标名精确词匹配**映射场景：
  指标名按非字母数字分隔符（`./_-`）切分后与关键词做精确词比对（大小写不敏感），
  场景优先级 = 元组顺序（延迟 > 错误率 > 资源 > 可用性）：

  | 关键词（精确词） | 场景 |
  |---|---|
  | latency / p99 / response / rt / duration | `latency_spike` |
  | error / errors / errorrate / failure / exception / 5xx | `error_rate_spike` |
  | cpu / memory / mem / disk / io / gc / connection / thread / load | `resource_saturation` |
  | availability / success / sla / uptime | `availability_drop` |

- **为什么精确词匹配**：裸子串会误命中（如 `rt` ⊂ `cart_abandonment_rate`），
  把业务指标错误路由到延迟场景。评审修复：改为按词比对，`cart_abandonment_rate` 不再误映射。
- **多指标同时异常**：按时间遍历全部异常（时间先验：更早异常的更可能是因，
  起始时间相同按幅度降序 tie-break），取**第一个可映射**的指标作主场景。
  最早异常不可映射时（如网络类自定义指标），仍可路由到同窗内的可映射异常，
  不把整个指标分支丢弃。`earliest_anomaly` 始终记录时间最早者，供假设打分。
- 全部异常都不可映射 → 指标分支落空，走下一优先级。

### 2. 业务证据

- **前提**：技术信号干净。判定 = 无异常指标 **且 观测到健康且数据充分的资源指标**。
  - "数据充分"：`detect_anomaly` 对点数不足的序列（`< min_points=8`）返回
    `is_anomaly=False` + detail "点数不足"——这是"无数据可判定"，不是"观测到健康"，
    不能据此走 business_logic（评审修复：`_has_sufficient_data` 检查 detail）。
  - 资源指标用**独立**的精确词集（cpu/memory/mem/disk/io/gc/connection/thread/load），
    不依赖场景映射词——避免 `upload_bytes`（含 load 子串）等业务指标被误当资源指标。
- **注意**：传入的 anomalies 必须是**完整**的 detect_anomaly 结果（含 `is_anomaly=False`
  的正常指标），不能是 detect_anomalies() 过滤后的异常子集——否则"全部正常"退化成
  空列表，无法证明资源指标被观测过。
- 命中业务白名单（`车门打不开`/`无法开门`/`支付失败`/`订单卡住`/`收不到验证码`/…）
  → `business_logic`，并从白名单条目抽 `BusinessContext(entity, symptom)`。
- **白名单只用症状短语，不用裸实体词**：实体词（如"车门"）在技术告警里出现很常见
  （服务名/发布公告），会误判 business_logic（评审修复：移除 `("车门", "车门", "异常")` 条目）。

### 3. LLM 兜底（ask_json 强约束）

- 提示词显式声明"技术指标全正常但告警描述业务功能不正常时选 business_logic"。
- 走 `ask_json`：枚举强约束（6 类），LLM 只能选枚举值；非法值被 jsonschema 拒绝。
- LLM 还能返回 `business_entity` / `business_symptom`（覆盖白名单外的业务语义，如"充电桩充不进电"）。
- **置信度门槛**（`_LLM_MIN_CONFIDENCE=0.5`）：LLM 判定置信度 <0.5 视为"判不出"，
  不当作权威路由决策，降级 other（评审修复：此前 LLM 返回 0.05 也会被当权威结论）。
- **LLM 返回 OTHER 时钳制置信度为 0.1**：与确定性 other 出口语义一致
  （other = 低置信兜底出口），避免 "other + 高置信" 的矛盾。
- **容忍多余字段**：schema 不用 `additionalProperties:False`——DeepSeek 常在 JSON 里
  附带 reason/说明，严格拒绝会把整次判定降级 other（评审修复）。
- LLM 连续坏输出/抛异常 → 不影响整体，落到优先级 4。

### 4. other 兜底

无指标证据、无业务命中、无 LLM（或 LLM 判定失败）→ `other`，置信度 0.1。
basis 如实记录失败原因（审计可追溯）。

## 输出契约

```python
@dataclass
class ScenarioResult:
    scenario: ScenarioType          # 主场景（6 枚举之一）
    confidence: float               # 0~1
    basis: str                      # 判定依据（审计/debug）
    source: str                     # metric / business / llm / other
    business_context: BusinessContext  # 业务上下文
    earliest_anomaly: MetricAnomaly | None  # 最早异常指标（主场景依据）
    raw_anomalies: list[MetricAnomaly]      # 全部异常指标（供假设打分）
    to_summary() -> str             # 一行摘要（进 incident_context/Evidence.summary）
```

`schema/models.py` 同步新增：
- `ScenarioType.BUSINESS_LOGIC = "business_logic"`（第 6 类场景）
- `BusinessContext`（entity/symptom/action/confidence/source，`is_present` 属性）

## 设计边界

- **纯逻辑模块，不直接依赖具体 LLM**：LLMClient 注入（生产 DeepSeek / 测试 FakeLLM），
  `llm=None` 即禁用 LLM 兜底（纯规则确定性模式）。
- **指标优先于业务**：即使业务文本命中（"车门打不开"），只要该服务 error_rate 异常，
  也归 `error_rate_spike`——因为技术剧本能定位到那个业务错误码；业务上下文仍抽取进报告。
- **business_logic 是确定性出口**：不依赖 LLM（白名单命中即判），LLM 只兜底白名单外的
  业务语义。这保证"技术信号干净 + 明确业务症状"这类故障一定走业务剧本。
- 业务白名单是**可配置**的（`BusinessWhitelist(entries=...)`），按业务域调整。

## 已知边界与局限

- **业务白名单是关键词匹配**：覆盖不了语义等价但措辞不同的描述（"开不了门"vs"车门打不开"），
  白名单外的语义兜底靠 LLM（Phase 2 可升级为业务文档语义检索/RAG）。
  词边界问题：指标名精确词匹配已解决，但业务白名单仍是子串匹配（"车门打不开"不会命中
  "车门打不开啊"之外的同义表达）。
- **多指标异常只取一个主场景**：复合故障的次场景不单独路由（进假设打分）。
- **LLM 置信度阈值是经验值**：`_LLM_MIN_CONFIDENCE=0.5` 需按真实数据回归调参。
- **JITTER 异常的起始时间系统性偏早**：`detect_anomaly` 对 JITTER 把 `anomaly_start`
  设为检测窗口第一个点（未做起始点搜索），而 SPIKE/RISE/FALL 用超阈值点（恒 ≥ 窗口起点），
  因此同窗口内 JITTER 的起始时间恒 ≤ 其他形态——`_pick_earliest` 会把 JITTER 排在前面。
  已知局限，够用版取舍（异常检测器的后续改进项）。
- **真实接线的 `incident_text` 来源**（告警 title/正文 vs 手动 free_text）需在事件解析步骤确认。

## 本地验证

```bash
# 模块测试（35 例，含评审修复补强）
.venv/Scripts/python.exe -m pytest tests/test_scenario_router.py -v

# CLI 演示（mock 数据，--service 限定服务指标）
.venv/Scripts/python.exe scripts/run_trace_rebuild.py --scenario "用户反馈支付失败" --service checkout   # → error_rate_spike
.venv/Scripts/python.exe scripts/run_trace_rebuild.py --scenario "用户反馈车门打不开" --service car-door  # → business_logic（技术信号干净）
.venv/Scripts/python.exe scripts/run_trace_rebuild.py --scenario "奇怪的告警:模块变慢" --service car-door # → other（未命中业务白名单）
```
