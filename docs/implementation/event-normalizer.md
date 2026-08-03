# 事件接收 / 归一化实现细节

> 模块：`app/pipeline/event_normalizer.py`
> 关联 PRD：§4.1（RCA-003 事件归一化与去重、RCA-004 事件解析）、§8.1（IncidentEvent schema）
> 状态：Phase 0 已落地

## 解决什么问题

把告警平台的**脏载荷**（webhook payload）归一化为统一的 `IncidentEvent`，并为"告警风暴"场景提供时间窗去重。它是 Agent 的**门卫**——保证进入 7 步工作流的事件是干净的、唯一的、有身份的（incident_id）。

**真实告警载荷的脏**：
- 字段名不统一（`ts` / `time` / `timestamp`，`message` / `msg` / `text`）
- 时间缺时区、可能是 epoch 毫秒/秒
- severity 可能是中文 / 大写 / 数字
- 没有 traceId（需从自由文本提取）
- 没有显式时间窗（需从告警时间推断）

## 核心方法

### `normalize_alert_payload(payload) -> IncidentEvent`

1. **取 title**：title / message / msg / text 第一个非空；全缺抛 `EventNormalizeError`。
2. **解析时间**：支持 ISO8601（含/不含时区）、epoch 秒/毫秒、datetime 对象；naive 视为 UTC（由 schema `UTCDateTime` 保证）；**没有时间字段回退到当前时刻**。
3. **severity 映射**：`严重/critical/p0 → CRITICAL`，`警告/warn/p1 → WARNING`，`信息/notice/p2 → INFO`，未知默认 WARNING。
4. **traceId 提取**：先从 payload 的 `trace_id/traceId/trace-id` 字段取，再正则扫自由文本（32/16 位 hex、UUID 形态）。
5. **labels 抽取**：service（service/service_name/app）+ host（host/instance/hostname）+ metric（metric/alertname）+ trace_id；缺省时从自由文本兜底（`服务:xxx`）。
6. **时间窗回填**：有 starts_at/end 用显式值，缺省用告警时间 ± 30 分钟。
7. 组装 `IncidentEvent`（source=alert_webhook）。

### `dedup_key(event) -> str`（RCA-003）

去重键 = `md5(service | metric | 小时窗口)`。同一服务 + 同一指标 + 同一小时内算重复。

### `AlertDedupStore`

时间窗去重器：记录已见事件的去重键 + 到达时间，TTL 内重复返回 True。骨架阶段为**内存实现**（`dict`），接真环境后换 Redis（TTL 与并发由外部存储保证）。

## 设计边界

- **本模块纯确定性，不调 LLM**。LLM 的"语义抽取"（第 1 步事件解析）在进入工作流后做，这里只做**结构层面的归一化**，保证事件干净可消费。
- 去重粒度是"告警事件"而非"故障根因"——同一故障可能多指标告警，各自算新事件（更细的关联归并是工作流第 1 步的事）。
- 事件身份 `incident_id`：payload 显式给则用，否则 `INC-{时间戳}-{hash}` 生成。

## 输出契约

统一的 `IncidentEvent`（见 `app/schema/models.py`，PRD §8.1 权威实现）。

## 已知边界与局限

- 内存去重在多实例下不共享（单实例并发上限 3 的骨架场景够用；多实例需 Redis）。
- traceId 提取依赖日志/告警里确实写了 traceId；没有则 `trace_id` 为空，链路重建退化为弱重建。
- 时间回退到"当前时刻"会使去重窗口漂移——仅限缺时间的异常告警。

## 本地验证

```bash
.venv/Scripts/python.exe -m pytest tests/test_event_normalizer.py -v
```
覆盖：脏载荷归一化、traceId 提取、severity 映射、缺 title 抛错、时间窗回填、去重键、去重器，共 8 例。
