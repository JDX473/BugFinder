# RCA Agent 项目

线上微服务故障根因定位 Agent（Root Cause Analysis Agent）。

## 目录

- [docs/PRD.md](docs/PRD.md) —— 产品需求文档（v0.1 评审稿，评审通过后进入 MVP 开发）
- [docs/research/](docs/research/) —— 业界 RCA 架构调研报告（三份）
  - [01-业界RCA架构全景调研.md](docs/research/01-业界RCA架构全景调研.md)
  - [02-Agentic-RCA工程化实现调研.md](docs/research/02-Agentic-RCA工程化实现调研.md)
  - [03-评测与业界案例调研.md](docs/research/03-评测与业界案例调研.md)

## 当前状态

- [x] 调研：业界 RCA 架构 / Agentic RCA 工程化 / 评测与业界案例（2026-08）
- [x] PRD：v0.1 评审稿（2026-08-03）
- [ ] MVP 开发：等待 PRD 评审通过

## 硬约束

1. 可用数据信号：线上日志 + 机器监控指标 + traceId（无完整链路存储，靠日志按 traceId 重建调用链）
2. 拿不到变更/发布事件，不依赖变更关联作为主信号
3. 大模型选型 DeepSeek（不支持 Structured Output，需 ask_json shim）
