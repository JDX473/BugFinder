# RCA Agent 项目

线上微服务故障根因定位 Agent（Root Cause Analysis Agent）。

## 目录

- [docs/PRD.md](docs/PRD.md) —— 产品需求文档（v0.1 评审稿，评审通过后进入 MVP 开发）
- [docs/code-access-strategy.md](docs/code-access-strategy.md) —— 代码/业务文档接入策略（决策记录）
- [docs/research/](docs/research/) —— 业界 RCA 架构调研报告（三份）
  - [01-业界RCA架构全景调研.md](docs/research/01-业界RCA架构全景调研.md)
  - [02-Agentic-RCA工程化实现调研.md](docs/research/02-Agentic-RCA工程化实现调研.md)
  - [03-评测与业界案例调研.md](docs/research/03-评测与业界案例调研.md)
- [app/](app/) —— MVP 代码（骨架阶段）

## 当前状态

- [x] 调研：业界 RCA 架构 / Agentic RCA 工程化 / 评测与业界案例（2026-08）
- [x] PRD：v0.1 评审稿（2026-08-03）
- [ ] MVP 开发（进行中）：已完成骨架阶段——数据模型、ask_json shim、mock 数据源、traceId 链路重建（CLI 原型）

## 开发状态（MVP 骨架，Phase 0）

已落地（纯库 + CLI + 测试，零 Web / 零 LangGraph / 零 ReAct）：

| 模块 | 说明 |
|---|---|
| `app/schema/models.py` | IncidentEvent / Evidence / RCAReport（Pydantic，PRD §8 唯一权威实现） |
| `app/llm/ask_json.py` | DeepSeek 结构化输出 shim（提示词约束→json.loads→jsonschema→≤3 重试→确定性兜底） |
| `app/tools/base.py` | 数据源适配器协议 + 查询护栏（时间窗/白名单/上限） |
| `app/tools/mock_datasource.py` | mock 数据源（gateway→checkout→payment 故障场景，离线开发/测试） |
| `app/tools/trace_reconstruction.py` | traceId 聚合重建调用链（PRD §5.3 关键路径，强/弱重建 + 慢错定位） |
| `scripts/run_trace_rebuild.py` | CLI 原型：traceId → 粗糙调用序列 |

### 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"

# 2. 跑测试
.venv/Scripts/pytest

# 3. 用 mock 数据源试 CLI（无需任何线上配置）
.venv/Scripts/python scripts/run_trace_rebuild.py tr-mock-0001   # 故障 trace
.venv/Scripts/python scripts/run_trace_rebuild.py tr-mock-0002   # 正常 trace（基线对比）
.venv/Scripts/python scripts/run_trace_rebuild.py --list         # 列出可用 traceId
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
