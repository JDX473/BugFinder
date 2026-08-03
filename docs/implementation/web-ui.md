# 报告 Web 页面 实现细节

> 模块：`app/web/api.py`（FastAPI）、`app/web/static/index.html`（前端）
> 关联 PRD：§4.5（RCA-040 报告 Web 页面）、§7.2（FastAPI 技术选型）
> 状态：MVP 骨架已完成（事件列表 + 触发调查 + 报告详情）

## 解决什么问题

Agent 此前只能 CLI 打印报告（`--report`），值班的人没法看。本模块提供**报告 Web 页面**
（PRD §4.5 P0 需求）——事件列表、一键触发调查、完整报告详情（根因候选/证据链/时间线/
修复建议），让 Agent 从"验证工具"变成"可用产品"。也是反馈闭环（RCA-050）、真实灰度的底座。

## 架构

```
浏览器 (index.html, 原生 JS)
   │  fetch
   ▼
FastAPI (app/web/api.py)
   ├─ GET  /                    → 报告台首页（HTML）
   ├─ GET  /api/incidents        → 可用事件列表（mock 预设故障样例）
   ├─ POST /api/incidents/{id}/investigate → 触发调查，产出报告
   ├─ GET  /api/reports/{rid}    → 报告详情（RCAReport 完整序列化）
   └─ GET  /api/reports          → 已产出报告列表（内存）
   │
   └─ RCAWorkflow（复用编排层：7 步确定性 + mock 数据源）
```

**无状态设计**：骨架阶段不落库，报告存内存 dict（`_reports`），按 report_id 取。
接真实环境换 Postgres（PRD §7.2 存储层）。

## 端点契约

| 端点 | 入参 | 返回 |
|---|---|---|
| `GET /api/incidents` | — | `{incidents: [{incident_id, title, severity, service, triggered_at, desc}]}` |
| `POST /api/investigate` | body `{free_text, service?, trace_id?}` | `{report_id, incident_id, scenario, status, n_candidates}`（手动触发，RCA-002） |
| `GET /api/investigate/stream` | query `{free_text, service?, trace_id?}` | **SSE 流式调查**：`step`（进度）→ `report`（最终报告）|
| `POST /api/incidents/{id}/investigate` | 路径 id | `{report_id, incident_id, scenario, status, n_candidates}` |
| `GET /api/reports/{report_id}` | 路径 id | 完整 `RCAReport`（Pydantic `model_dump(mode="json")`） |
| `GET /api/reports` | — | `{reports: [{report_id, incident_id, scenario, status}]}` |

错误处理：未知事件/报告 → 404（含可用列表提示）；调查失败 → 500；空 free_text → 422。

## 流式调查（SSE）

`GET /api/investigate/stream` 用 `StreamingResponse` 推送事件流（`data: {json}\n\n`）：
- `{"type": "step", "step": 1..7, "name", "label"}`：每步完成后推一次
- `{"type": "report", "report": {...RCAReport...}}`：最后推完整报告
- `{"type": "error"}` / `{"type": "interrupt"}`：异常/HITL

实现：`workflow.stream()` yield 每步（langgraph `stream_mode="updates"`），API 层包成 SSE。
前端 `fetch` + `ReadableStream` 逐行解析，实时更新"调查进度"区块（7 个 step-chip 逐个打勾）。

## 默认 LLM + ReAct

Web 层默认 `create_deepseek_client()` 注入 workflow（`_build_workflow`）：
- 场景兜底（无指标证据时 LLM 判场景）
- 假设排序（多候选时 LLM 排名）
- **有界 ReAct**（日志异常簇时 LLM 决定是否深挖，`_react_log_dig`）
未配置 `RCA_LLM_API_KEY` 时降级纯规则（`create_deepseek_client` 返回 None）。

## 前端页面

单 HTML（`index.html`）+ 原生 JS，无构建（无 React/Vue，符合"MVP 骨架"定位）：
- **给 Agent 发消息**：自由文本输入框 + 可选服务名，手动触发调查（RCA-002）——值班人
  直接描述故障即可发起
- **事件列表**：mock 预设的 2 个故障样例（error_rate + 业务故障），带严重度徽章
- **触发调查**：点"调查 →"调 investigate，产出报告后滚动到报告区
- **报告详情**：根因候选（rank/置信度/假设/推理/证据引用）、时间线（cause/symptom）、
  证据链（类型/ID/摘要/失败标记）、修复建议（优先级）

## 关键决策

1. **复用 RCAWorkflow 而非重写**：触发调查直接调 `workflow.invoke(incident)`，7 步
   编排 + 失败降级全复用，API 层只做"归一化 → 调查 → 序列化"。
2. **前端零依赖**：原生 fetch + 模板字符串，无 npm/构建步骤——骨架阶段最轻。
3. **Pydantic 序列化**：`report.model_dump(mode="json")` 一次转换，枚举转 str，
   datetime 转 ISO，前端直接消费。

## 已知边界与局限

- **报告不落库**：进程重启后报告丢失（内存存储）。接 Postgres 前仅演示用。
- **mock 数据源固定**：事件列表是预设的 2 个样例，不是真实告警接入（RCA-001
  webhook 是后续工作）。
- **无人工反馈入口**：RCA-033（采信/纠偏/驳回）未做，是反馈闭环（下一项）。
- **并发/队列未做**：单实例同步处理，无告警风暴排队（RCA-014 Phase 2）。

## 本地验证

```bash
# 起服务（浏览器打开 http://localhost:8787）
.venv/Scripts/python.exe -m uvicorn app.web.api:app --port 8787

# API 测试（10 例）
.venv/Scripts/python.exe -m pytest tests/test_web_api.py -v
```
