# RCA Agent 项目

线上微服务故障根因定位 Agent（Root Cause Analysis Agent）。

## 目录

- [docs/PRD.md](docs/PRD.md) —— 产品需求文档（v0.1 评审稿，评审通过后进入 MVP 开发）
- [docs/code-access-strategy.md](docs/code-access-strategy.md) —— 代码/业务文档接入策略（决策记录）
- [docs/optimization-diary.md](docs/optimization-diary.md) —— 优化日记（踩坑 + 解决办法，与设计文档分开）
- [docs/research/](docs/research/) —— 业界 RCA 架构调研报告（三份）
  - [01-业界RCA架构全景调研.md](docs/research/01-业界RCA架构全景调研.md)
  - [02-Agentic-RCA工程化实现调研.md](docs/research/02-Agentic-RCA工程化实现调研.md)
  - [03-评测与业界案例调研.md](docs/research/03-评测与业界案例调研.md)
- [app/](app/) —— MVP 代码（骨架阶段）

## 当前状态

- [x] 调研：业界 RCA 架构 / Agentic RCA 工程化 / 评测与业界案例（2026-08）
- [x] PRD：v0.1 评审稿（2026-08-03）
- [x] MVP 骨架：数据模型、ask_json shim、mock 数据源、traceId 链路重建（CLI 原型）
- [x] Phase 1 确定性积木：事件归一化、异常检测、日志聚类、场景路由、假设打分、报告生成（端到端"事件 → 报告"已打通）
- [x] 编排层：LangGraph 7 步状态机（预算路由 + HITL 中断/恢复 + checkpoint + 有界 ReAct harness）
- [x] 真实数据接入：RCAEval 开放数据集（RE1-OB 125 cases Top-3 95.2% + RE2-SS 90 cases Top-3 91.1% + RE2-TT 90 cases trace 验证）
- [x] 真实 LLM 接入：DeepSeek 三注入点接通（场景兜底/假设排序/日志深挖），规则 vs LLM 全量对比（Top-3 96.0%）
- [x] 真实 trace 验证：RE2-TT 慢节点定位 52%（耗时高≠根因 → 多信号交叉验证的必要性）
- [x] 评估基线固化：Top-1 83-85% vs 业界 42-57%（详见 [evaluation-baseline](docs/implementation/evaluation-baseline.md)）
- [ ] 报告 Web 页 / 反馈闭环 / traceId 日志重建验证（下一步）

## 开发状态（MVP 骨架，Phase 0 → Phase 1 起步）

已落地（纯库 + CLI + LangGraph 工作流 + 测试，零 Web）：

| 模块 | 说明 |
|---|---|
| `app/schema/models.py` | IncidentEvent / Evidence / RCAReport / BusinessContext（Pydantic，PRD §8 唯一权威实现） |
| `app/llm/ask_json.py` | DeepSeek 结构化输出 shim（提示词约束→json.loads→jsonschema→≤3 重试→确定性兜底） |
| `app/tools/base.py` | 数据源适配器协议 + 查询护栏（时间窗/白名单/上限） |
| `app/tools/mock_datasource.py` | mock 数据源（gateway→checkout→payment 故障 + car-door 业务故障，离线开发/测试） |
| `app/tools/trace_reconstruction.py` | traceId 聚合重建调用链（PRD §5.3 关键路径，强/弱重建 + 慢错定位） |
| `app/pipeline/anomaly_detection.py` | MAD/3σ 指标异常检测（确定性，形态/起始时间/幅度，PRD §5.2） |
| `app/pipeline/event_normalizer.py` | 事件接收/归一化 + 去重（脏告警→IncidentEvent，RCA-003/004） |
| `app/pipeline/log_clustering.py` | 日志聚类/降噪（噪音过滤 + 模板聚类 + 簇摘要，PRD §5.1/§6.2 步骤 4） |
| `app/pipeline/scenario_router.py` | 场景路由（6 类场景：指标优先 + 业务白名单 + LLM 兜底，PRD §6.2 步骤 2） |
| `app/pipeline/hypothesis_scoring.py` | 假设生成/打分（Top-3 候选根因：trace/指标/日志三路生成 + 确定性打分 + LLM 只排序，PRD §6.2 步骤 6） |
| `app/pipeline/report_generation.py` | 报告生成（RCAReport 组装：校验降级 + 时间线 + 修复建议 + 审计，纯确定性不调 LLM，PRD §6.2 步骤 7） |
| `app/graph/state.py` | 工作流共享状态（TypedDict + reducer，贯穿 7 步） |
| `app/graph/nodes.py` | 7 步工作流节点（封装确定性模块，失败降级写占位证据，RCA-012） |
| `app/graph/workflow.py` | LangGraph 7 步状态机（预算路由 + HITL 中断/恢复 + checkpoint，PRD §6.1） |
| `app/graph/bounded_react.py` | 有界 ReAct harness（max_iters≈4 + 工具受限 + ask_json 强约束 + 证据压制，PRD §6.3） |
| `app/tools/rcaeval_datasource.py` | RCAEval 开放数据集适配器（RE1 data.csv / RE2 metrics.csv 探测，跳空值，带 ground truth 标注） |
| `scripts/run_trace_rebuild.py` | CLI 原型：traceId 重建 / 日志聚类 / 场景路由 / 一键产出报告 |
| `scripts/eval_rcaeval.py` | 真实数据评估（RCAEval RE1/RE2 指标，Top-1/Top-3 命中率，PRD §9 雏形） |
| `scripts/verify_re2_trace.py` | RE2-TT 真实 trace 验证（调用链还原 + 慢节点定位，52% 命中 → 多信号交叉） |

> 模块实现细节见 [`docs/implementation/`](docs/implementation/)。

### 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"

# 2. 跑测试
.venv/Scripts/pytest

# 3. 用 mock 数据源试 CLI（无需任何线上配置）
.venv/Scripts/python scripts/run_trace_rebuild.py tr-mock-0001   # 故障 trace（重建 + 日志聚类）
.venv/Scripts/python scripts/run_trace_rebuild.py tr-mock-0002   # 正常 trace（基线对比）
.venv/Scripts/python scripts/run_trace_rebuild.py tr-mock-0003   # 业务故障（车门打不开）
.venv/Scripts/python scripts/run_trace_rebuild.py --list         # 列出可用 traceId

# 4. 场景路由演示（--service 限定服务指标）
.venv/Scripts/python scripts/run_trace_rebuild.py --scenario "用户反馈支付失败" --service checkout   # → error_rate_spike
.venv/Scripts/python scripts/run_trace_rebuild.py --scenario "用户反馈车门打不开" --service car-door  # → business_logic

# 5. 一键产出完整 RCAReport（全流程：场景→trace→日志→指标→假设→报告）
.venv/Scripts/python scripts/run_trace_rebuild.py --report --scenario "用户反馈支付失败" --service checkout --trace-id tr-mock-0001   # error_rate 场景 + trace 假设
.venv/Scripts/python scripts/run_trace_rebuild.py --report --scenario "用户反馈车门打不开" --service car-door                          # business_logic + 业务上下文

# 6. LangGraph 工作流（7 步状态机，Python API）
.venv/Scripts/python -c "
from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload
wf = RCAWorkflow()
incident = normalize_alert_payload({'title': 'checkout error_rate 异常', 'service': 'checkout', 'timestamp': '2026-08-02T21:00:00Z', 'trace_id': 'tr-mock-0001'})
report = wf.invoke(incident)['report']
print(report.scenario.value, report.meta.status.value, len(report.root_cause_candidates))
"
# HITL：hitl=True 时在调查开始前中断等人工确认，resume 恢复
.venv/Scripts/python -c "
from app.graph.workflow import RCAWorkflow
from app.pipeline.event_normalizer import normalize_alert_payload
wf = RCAWorkflow(hitl=True)
incident = normalize_alert_payload({'title': 'checkout error_rate 异常', 'service': 'checkout', 'timestamp': '2026-08-02T21:00:00Z'})
tid = 'demo'; wf.invoke(incident, thread_id=tid)                 # 停在中断点
wf.invoke(incident, thread_id=tid, resume_value='确认')          # 恢复调查
print('done:', not wf.is_interrupted(tid))
"

# 7. 真实数据评估（RCAEval 开放数据集，需先下载 RE1-OB 到 rca-data/）
.venv/Scripts/python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --limit 125   # 纯规则：Top-3 95.2%
.venv/Scripts/python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --case adservice_cpu/1 --verbose

# 8. 真实 DeepSeek（需设 RCA_LLM_API_KEY 环境变量）
RCA_LLM_API_KEY=sk-xxx .venv/Scripts/python scripts/eval_rcaeval.py --limit 125 --llm   # LLM 模式：Top-3 96.0%
```

### 环境变量

全部配置走环境变量（`config/settings.py`），骨架阶段默认 mock，无需配置即可运行。
接真实数据源/LLM 时设置：

- `RCA_DATA_SOURCE=real`（默认 `mock`）
- `RCA_LLM_API_KEY`（DeepSeek key，未设置时 LLM 走 mock/兜底）
- `RCA_LLM_MODEL`、`RCA_LLM_BASE_URL`（默认 `deepseek-chat` / `https://api.deepseek.com/v1`）

## 硬约束

1. 可用数据信号：线上日志 + 机器监控指标 + traceId（无完整链路存储，靠日志按 traceId 重建调用链）
2. 拿不到变更/发布事件，不依赖变更关联作为主信号
3. 大模型选型 DeepSeek（不支持 Structured Output，需 ask_json shim）
