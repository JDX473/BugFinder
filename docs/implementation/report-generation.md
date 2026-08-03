# 假设生成/打分 + 报告生成 实现细节

> 模块：`app/pipeline/hypothesis_scoring.py`、`app/pipeline/report_generation.py`
> 关联 PRD：§6.2（步骤 6 假设生成/打分、步骤 7 报告生成）、§8.3（RCAReport）、§8.4（校验与兜底）、§12.1（只读优先）
> 状态：Phase 1 已落地，全流程"事件 → 报告"打通

## 解决什么问题

场景路由（步骤 2）做完后，Agent 只回答"这是哪类故障"。本模块补上最后两步，让系统能产出一份**完整的 RCAReport**：

- **步骤 6 假设生成/打分**：基于已采集证据 + 场景 + 调用链，生成 **Top-3 候选根因**，每个带置信度、支持/反驳证据、推理链。
- **步骤 7 报告生成**：把假设 + 证据 + 审计组装成 `RCAReport`，含时间线、修复建议、校验降级、审计轨迹。

这是整个系统的**价值出口**——MVP 验收口径（§9.1）打分的对象就是这份报告。前面所有模块（事件/场景/trace/日志/指标）都是为它服务的。

**为什么新增 `business_logic` 假设**：场景路由把"技术信号干净但功能不对"（车门打不开）判为 `business_logic` 后，若假设打分因"无异常指标"而产出空候选，报告就退化成"无候选 + partial"——浪费了业务上下文。因此技术信号干净 + 有业务上下文时，生成一条**业务假设**（"疑似业务规则/状态异常，非技术故障"）。

## 核心方法

### 步骤 6：假设生成/打分（确定性优先，LLM 只排序）

**三路假设生成（按确定性从高到低）**：

| 来源 | 触发 | 假设 | 证据基础分 |
|---|---|---|---|
| **trace 慢/错跳** | 调用链有错误/慢跳 | "调用链 A->B 出现慢/错（下游 B 处理失败/超时）" | 3.0~3.3 |
| **指标异常** | 有异常指标 | 场景主假设模板（错误率/延迟/资源/可用性） | 2.0~2.5 |
| **日志错误簇** | 有日志错误簇 | "X 服务出现错误日志" | 1.5 |
| **business_logic** | 技术信号干净 + 业务上下文 | "实体 症状（疑似业务规则/状态异常）" | 2.2 |

**trace 假设的"最深错误边优先"**：错误发源地（target 不是任何错误边的 source）的边加分（3.3），症状传播边 3.0。如 `gateway→checkout→payment` 里 payment 是发源地，`checkout->payment` 假设优先于 `gateway->checkout`——错误向调用方传播，最深者即根因所在。

**确定性打分**（`_score_hypothesis`）：
```
score = 证据基础分 × 时间一致性系数 + 信号一致性奖励 − 反驳扣分
```
- **证据基础分**：trace > 指标 > 日志（调用链级事实最强）。
- **时间先验**：原因时间 ≤ 事件起点；证据晚于起点按分钟数扣分（最多 40%）。
- **跨信号一致性**：同一假设有 trace+metric+log 多类证据时加分（上限 0.15）。
- **反驳扣分**：每条矛盾证据 -0.1。
- **归一化**：sigmod 压缩 `1/(1+e^-(raw-1.6))` 到 [0,1]，避免原始分（0.5~3.5）撞 1.0 上限、Top-3 失去区分度。

**LLM 只排序、不生成**（PRD §2.2"LLM 直出终审结论"约束）：
- LLM 只对**规则已生成的假设**做 pairwise 排名（`top` 数组 = 按可能性降序的假设序号），不生成根因文本——生成文本是无界自由发挥，极易幻觉。
- 排名结果受规则分约束：LLM 失败/低置信（`<0.5`）/坏 JSON → 纯规则兜底，LLM 排序不覆盖规则分。
- 重排后按名次微调总分（rank1 保留，rank2 -0.05，rank3 -0.1）。

**指标证据合成**：场景路由只产出 `ScenarioResult`（含 `raw_anomalies`），不落 Evidence。若调用方未传指标证据，用 `scenario.raw_anomalies` 合成一条 `ev-metric-synth` 证据，保证假设生成有指标事实可用（报告 evidence_list 不重复写）。

### 步骤 7：报告生成（纯确定性组装，不调 LLM）

PRD §6.2 步骤 7 明确"**不调 LLM 二次推理，仅拼装与措辞**"。本模块分五步：

1. **候选校验**（`ReportValidator`）：候选 ≤ 3、必有 `supporting_evidence`、引用必须在 `evidence_list` 里。**校验失败降级而非整份丢弃**（PRD §8.4）：缺证据/坏引用的候选被裁剪，报告 meta 标 `partial` + `validation_violations` 说明。
2. **时间线**：只收编**离散事件**（场景最早异常指标、trace 首个错误跳、事件触发），不收"采集窗证据"（整窗的指标检测摘要是证据清单，不是时间线事件）。显著性：原因侧 ≤ 事件起点 → CAUSE，事件触发 → SYMPTOM，按时间排序。
3. **修复建议**：场景级建议（business_logic → "检查业务规则/配置开关/数据状态"）+ 根因级建议（rank1 含"慢/超时"时补一条）。全部只读（PRD §12.1，无写操作路径）。
4. **审计轨迹**：写入场景路由 + 假设打分的执行记录（RCA-060）。
5. **元信息**：状态（completed / partial）、token 成本、耗时、降级说明。

### schema 修订（评审遗留问题收口）

上轮对抗性评审记了 3 个"成立但属前瞻"的 schema 问题，本模块一并解决：

| 问题 | 修订 |
|---|---|
| `BusinessContext.source` 用裸 str | 新增 `ExtractionSource` 枚举（RULE/LLM/NONE），`BusinessContext.source` 类型改为枚举 + 宽松校验（外部字符串自动映射，未知值落 NONE） |
| `RCAReport` 无业务上下文承载字段 | 新增 `business_context: BusinessContext` 字段，business_logic 场景的业务语义进报告 |
| `ScenarioResult` 是 dataclass 序列化有坑 | 新增 `to_dict()`（场景/来源枚举转值，供 Evidence.payload/报告持久化）；`source` 保持 str（dataclass 无 Pydantic 校验层，宽松处理外部字符串） |

## 输出契约

```python
# hypothesis_scoring.py
@dataclass
class HypothesisScoringResult:
    candidates: list[RootCauseCandidate]   # Top-3，rank=1 最可能
    basis: str                              # 打分依据摘要（审计）
    used_llm: bool                          # 是否走了 LLM 排序

def generate_hypotheses(
    *, evidence: list[Evidence], scenario: ScenarioResult,
    graph: TraceGraph | None = None,
    event_start: datetime | None = None,
    llm: LLMClient | None = None,
) -> HypothesisScoringResult

# report_generation.py
class ReportValidator:  # max_candidates=3, required_evidence, require_valid_refs
    def validate(self, candidates, evidence_list) -> (usable, violations)

def generate_report(
    *, report_id, incident_id, event_start, scenario: ScenarioResult,
    hypotheses: HypothesisScoringResult, evidence_list: list[Evidence],
    graph: TraceGraph | None = None,
    token_cost=0, duration_sec=0, validator=None,
) -> RCAReport
```

`schema/models.py` 同步新增：
- `ExtractionSource` 枚举（RULE/LLM/NONE）
- `RCAReport.business_context` 字段
- `BusinessContext.source` 类型改为 `ExtractionSource`（带宽松校验）

## 设计边界

- **纯逻辑模块，不直接依赖具体 LLM**：LLMClient 注入（生产 DeepSeek / 测试 FakeLLM），`llm=None` 即纯规则确定性模式。报告生成**完全不调 LLM**。
- **LLM 不生成根因文本**：只对规则假设排序，且排序受规则分约束（低分假设不被 LLM 抬进 Top-3）。LLM 失败/低置信/坏 JSON/抛异常均降级到规则结果，不炸整条链路。
- **校验降级而非整份丢弃**：`ReportValidator` 显式校验候选，把 Pydantic 的"整份 ValidationError"降级为"标注 partial + 降级说明"——保证不完整候选不炸掉报告。
- **`RootCauseCandidate` 由 Pydantic 校验**（≤3、必有支持证据、引用一致），假设打分产出的是合法候选；`ReportValidator` 是报告层对**外部传入候选**的二次防御。

## 已知边界与局限

- **假设生成依赖证据齐全**：trace 缺失（traceId 空/聚合不到）时无 trace 假设；日志簇非异常时无日志假设。这是硬约束（数据信号只有三类），报告如实标注。
- **指标证据合成是"够用版"**：`ev-metric-synth` 不写进报告 evidence_list（避免重复），只服务假设打分。报告层拿到的指标证据仍需编排层显式构造。
- **sigmod 归一化参数（`-1.6` 中心点）是经验值**：需按真实数据回归调参，确保分档（high/medium/low）分布合理。
- **时间先验锚点 `event_start` 语义**：用事件起点（告警触发/故障起点），不是采集窗起点（否则整窗证据都算"原因侧"，时间线失去区分度）。
- **修复建议是模板映射**：覆盖 6 类场景的通用方向 + 慢/超时根因，不针对具体业务深挖（Phase 2 RAG 历史案例后增强）。
- **业务上下文只进报告顶层 + 场景假设**：`RootCauseCandidate` 无独立业务上下文字段，业务语义靠假设文本承载（够用版取舍）。

## 本地验证

```bash
# 模块测试（18 + 16 例）
.venv/Scripts/python.exe -m pytest tests/test_hypothesis_scoring.py tests/test_report_generation.py -v

# CLI 全流程演示（--report 一键产出完整 RCAReport）
.venv/Scripts/python scripts/run_trace_rebuild.py --report --scenario "用户反馈支付失败" --service checkout --trace-id tr-mock-0001   # error_rate 场景 + trace 假设
.venv/Scripts/python scripts/run_trace_rebuild.py --report --scenario "用户反馈车门打不开" --service car-door                          # business_logic + 业务上下文
.venv/Scripts/python scripts/run_trace_rebuild.py --report --scenario "奇怪的告警:模块变慢" --service car-door                          # other → 强制人工
```
