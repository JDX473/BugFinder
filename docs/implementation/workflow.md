# LangGraph 7 步工作流 实现细节

> 模块：`app/graph/state.py`、`app/graph/nodes.py`、`app/graph/workflow.py`、`app/graph/bounded_react.py`
> 关联 PRD：§6.1（总览）、§6.2（分步细节）、§6.3（关键设计决策）、§6.4（LLM 调用规格）、§7.3（成本与性能）
> 需求：RCA-010（全流程自动编排）、RCA-011（预算控制）、RCA-012（失败降级）、RCA-013（人工介入）、RCA-042（中间态查看）
> 状态：Phase 1 已落地，7 步状态机端到端跑通（事件 → 报告）

## 解决什么问题

前面各模块（事件归一化/场景路由/trace 重建/日志聚类/异常检测/假设打分/报告生成）是**独立的纯函数**，
但系统需要一个**编排层**把它们串成一次完整调查：告警进来 → 按 7 步顺序执行 → 产出一份 `RCAReport`，
过程中可中断、可恢复、可预算收敛、可失败降级。这就是 LangGraph 状态机要解决的——它是"确定性工作流"
骨架（PRD §6.3 #1：LLM 不拥有"想查就查"的无界自由）。

## 核心方法

### 1. State 设计（`state.py`）

`WorkflowState` 是 TypedDict，字段显式声明合并语义（reducer）：
- `evidence` 用 `operator.add`（列表累加——每次节点往共享状态追加证据）
- 其余字段用 `replace_reducer`（整体覆盖）
- `hitl_interrupts` / `hitl_resume_value` 记录 HITL 中断与恢复

State 直接存 Pydantic/dataclass 对象（`IncidentEvent`/`ScenarioResult`/`TraceGraph`/`RCAReport`）——
langgraph MemorySaver 对 dataclass 走 msgpack 序列化，对象引用随 checkpoint 恢复。

### 2. 7 个节点（`nodes.py`）

每个节点封装一个确定性模块，统一契约 `(state) -> dict`。**依赖注入用闭包工厂**（`make_*_node`）：
langgraph 节点签名必须是 `(state) -> dict`（或 `(state, config)`），不能直接挂 llm 参数——第二个位置参数
会被当 config 注入。

| 节点 | 封装模块 | 输出 |
|---|---|---|
| `1_parse` | event_normalizer 的 IncidentEvent | incident_text / event_start / services |
| `2_scenario` | scenario_router.route_scenario | scenario / metric_series / metric_anomalies |
| `3_trace` | trace_reconstruction.rebuild_trace | graph + ev-trace |
| `4_logs` | log_clustering.cluster_logs | ev-log（+ 可选 ReAct 深挖） |
| `5_metrics` | anomaly_detection（复用 2 步检测结果） | ev-metric |
| `5_agent`（仅 llm 注入） | bounded_react + 工具（query_logs/query_metric） | ev-agent（LLM 决策结论） |
| `6_hypotheses` | hypothesis_scoring.generate_hypotheses | hypotheses |
| `7_report` | report_generation.generate_report | report |

**指标检测前置**（`2_scenario`）：取序列 → detect_anomaly → 完整结果（含正常）存进 state.metric_anomalies，
`5_metrics` 只做"结论落 Evidence"不重复检测——保证全链路一致性。

### 3. 编排（`workflow.py`）

`RCAWorkflow` 封装编译后的 StateGraph：

**图结构**（含预算守卫 + LLM 决策循环）：
```
START → 1_parse
   ├─ budget_route[over] → 7_report → END     （预算收敛，评审 #1：在首个 LLM 调用点前判断）
   └─ continue → (hitl_gate? →) 2_scenario → 3_trace
        ├─ budget_route[over] → 7_report
        └─ continue → 4_logs → 5_metrics
             ├─ [llm 注入] → 5_agent → 6_hypotheses → 7_report → END
             └─ [llm=None] → 6_hypotheses → 7_report → END    （确定性路径不变）
```

**给 LLM 控制权**（5_agent，本轮新增）：`llm` 注入时，证据收集完（5_metrics）后插入
**LLM 决策循环节点**——让 LLM 读已收集证据摘要，用工具（`query_logs`/`query_metric`）决定
"还要不要查、查什么、何时收敛"，结论压 `ev-agent` Evidence 进共享状态。这是从"固定管线"
到"LLM 主导调查"的关键节点：LLM 不再只是固定点上的兜底，而是真正决定下一步查什么。
`llm=None` 时该节点不接入图，确定性路径完全不变。

**预算控制**（RCA-011）：`budget_route` 是 `add_conditional_edges` 的**路由函数**（只读返回分支名），
不是节点——返回 `'continue'`/`'report'`。超预算 → 跳过富集步骤直接出报告（收敛到当前最佳）。
`7_report` 节点在 meta 里标记 `budget_exceeded=True`（预算路由不写状态）。

**失败降级**（RCA-012）：每个节点失败写"占位 Evidence"（`Evidence.error=True`），报告如实标注
该信号缺失/失败，不抛异常中断图。评审修复后，**所有数据源通用异常**（ES 连接失败/网络断/权限）
也降级——不只捕业务异常。

**HITL**（RCA-013）：`hitl=True` 时 `1_parse` 后插 `hitl_gate` 节点，`interrupt()` 等人工确认。
恢复时 `Command(resume=value)` 投递答复，**返回值被消费**（写进 `hitl_resume_value`，且支持补充
service/trace_id 并入 incident）。resume 必须显式 thread_id（否则找不到中断线程静默空跑）。

**checkpoint**：默认 MemorySaver（内存级，生产换 Postgres/Redis）。`make_checkpointer()` 说明——
langgraph 4.1.1 默认 serde 允许所有类型，反序列化警告是未来硬化提示，当前不阻塞。

### 4. 有界 ReAct（`bounded_react.py`）

通用 harness，用于"需要 LLM 判断下一步查什么"的调查点（PRD §6.3 #2）。**四重有界**：
- 迭代上限（max_iters≈4，到点强制收敛）
- 工具受限（只能调注册的工具）
- 动作结构化（ask_json 强约束：tool / final_answer 二选一）
- 结论压制（压成一条 Evidence，LLM 中间思考不进共享状态）

当前接线：
- `4_logs` 注入 llm 时，若聚类含异常簇，用有界 ReAct 让 LLM 判断"是否深挖该簇"
  （工具：`query_logs`，失败落确定性兜底"不深挖"）。
- `5_agent`（本轮新增，见下）把有界 ReAct 提升为**工作流级 LLM 决策循环**——工具集
  扩展为 `query_logs` + `query_metric`，LLM 决定整个调查的下一步。
- `llm=None` → 纯确定性（不走 ReAct）。

### 5. LLM 决策循环（5_agent，本轮新增）

**解决的问题**：此前 LLM 只在 3 个局部兜底点被调用（场景兜底/日志深挖/假设排序），
它从不决定"下一步查什么"——图是固定线性链，LLM 无控制权，ReAct 在 mock 数据下
（无 error 级异常簇）实际是死代码。

**实现**（`nodes.py: make_agent_node`）：
- 证据收集完（5_metrics）后插入，内部跑 `run_bounded_react`（max_iters=4）。
- **观察前缀** = 已收集证据摘要（场景判定 + 各证据 summary），LLM 基于它决策。
- **工具集**：`query_logs`（查故障窗口日志，复用）+ `query_metric`（查指标 +
  detect_anomaly，新增）——工具真实作用于 mock 数据（`checkout_error_rate` 40 倍
  突增、3 条 error 日志都能查到），ReAct 从死代码变成每次调查都运转的主决策循环。
- **fallback**：LLM 失败/坏 JSON/低置信 → `{"conclude": True}`（确定性证据已收集，
  建议进假设生成）——agent 失败不阻塞主线。
- 结论压 `ev-agent` Evidence 进共享状态（证据压制：LLM 中间思考不进报告）。

**控制权语义**：LLM 决定"还要不要查、查哪个工具、何时收敛"（final_answer），
图按 LLM 决策走向假设生成。但受三重约束：max_iters=4 封顶、工具受限、失败落确定性
兜底——"LLM 有控制权，但安全网保留"。

**已知行为观察**（真实 DeepSeek 实测）：DeepSeek 在有界循环里倾向继续调工具而非收敛
（4 次 query_logs 后落兜底）。这是真实 LLM agent 的固有行为（业界实证：LLM 易在
低价值查询打转），prompt 已强化引导收敛，但收敛质量是后续调优项，不阻塞架构落地。
`used_llm=False`（落兜底）时结论仍是 `conclude=True`，确定性主线不受影响。

## 输出契约

```python
# workflow.py
class RCAWorkflow:
    def __init__(self, *, log_source=None, metric_source=None, llm=None,
                 checkpointer=None, token_budget=200_000, time_budget_sec=600, hitl=False)
    def invoke(self, incident: IncidentEvent, *, thread_id=None, resume_value=None) -> dict
    def invoke_manual(self, *, service=None, free_text=None, trace_id=None, thread_id=None) -> dict
    def is_interrupted(self, thread_id) -> bool
    def get_state(self, thread_id) -> dict

# bounded_react.py
class ReActTool(Protocol):  # name / description / args_schema / run(args)->str
def run_bounded_react(*, task, tools, llm=None, fallback=None, max_iters=4) -> ReActResult
```

返回的 state dict 含 `report`（RCAReport）、`meta`（token_cost/duration_sec/budget_exceeded）、
`step_index`、`evidence`、`hitl_resume_value` 等。

## 设计边界

- **确定性优先**：7 个节点默认全确定性（mock/规则），LLM 只在关键判断点（场景兜底、假设排序、
  日志深挖）注入。`llm=None` 即纯规则模式（测试/离线）。
- **失败不中断**：所有节点 + 所有数据源异常都降级为占位证据，图永不因单点失败崩溃。
- **预算收敛不丢报告**：超预算跳过富集，但报告必然产出（可能 partial）。
- **HITL 语义清晰**：中断 = 停在 `1_parse` 后；恢复 = `Command(resume=value)` + 同 thread_id；
  resume_value 被消费（写 state、可补充输入）。
- **mock 数据源惰性共享**：模块级单例，多个 workflow 实例共享一份（评审 #5 修复）。

## 已知边界与局限

- **MemorySaver 无淘汰**：checkpoint 全量常驻内存，长时间运行会增长（生产换 Redis/Postgres
  checkpointer，这是已知的部署事项，非本期缺陷）。
- **token 预算未真正计量**：`token_cost` 由调用方注入（ask_json 目前不返回 usage），`token_budget`
  是记录字段；真实计费需扩展 LLM 协议返回 usage（Phase 2）。
- **并发无锁**：同一 thread_id 并发 invoke 无保护（MemorySaver 最后写入胜出）；生产需外部
  锁/队列（单实例并发上限 3，RCA-014）。
- **HITL 只在前置确认点**：调查中段的"卡住接管"（RCA-042 中间态查看）需要更多 interrupt 点，
  当前只做调查前确认。
- **`_incident_window` 步长假设 30 分钟**：事件起点 ± 30min，真实数据源需按业务窗口调整。

## 本地验证

```bash
# 模块测试（workflow 22 + bounded_react 12 例）
.venv/Scripts/python.exe -m pytest tests/test_workflow.py tests/test_bounded_react.py -v

# 端到端（Python API）
.venv/Scripts/python.exe -c "
from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload
wf = RCAWorkflow()
incident = normalize_alert_payload({'title': 'checkout error_rate 异常', 'service': 'checkout',
    'timestamp': '2026-08-02T21:00:00Z', 'trace_id': 'tr-mock-0001'})
report = wf.invoke(incident)['report']
print(report.scenario.value, report.meta.status.value, len(report.root_cause_candidates))
"

# HITL 中断 + 恢复
.venv/Scripts/python.exe -c "
from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload
wf = RCAWorkflow(hitl=True)
incident = normalize_alert_payload({'title': 'checkout error_rate 异常', 'service': 'checkout',
    'timestamp': '2026-08-02T21:00:00Z'})
tid = 'demo'
wf.invoke(incident, thread_id=tid)                       # 停在中断点
print('interrupted:', wf.is_interrupted(tid))
wf.invoke(incident, thread_id=tid, resume_value='确认')  # 恢复
print('done:', wf.is_interrupted(tid))
"
```
