# 日志聚类 / 降噪实现细节

> 模块：`app/pipeline/log_clustering.py`
> 关联 PRD：§5.1（日志接入——降采样与聚合）、§6.2（步骤 4 日志分析）
> 状态：Phase 0 已落地

## 解决什么问题

把一段故障窗口的**原始日志大海**压成「异常模板簇」摘要，喂给 LLM 精读，
而不是把原文直接塞进上下文。这是控制 token 成本与 LLM 幻觉（读原始大数据）的关键一步。

**为什么需要它**：
- 一条告警窗口内可能上万行日志，直接进 LLM 上下文既贵又容易 lost-in-the-middle；
- 绝大多数日志是心跳 / 健康检查 / info 噪音，与根因无关，先确定性砍掉；
- 真正相关的错误日志高度重复（同一异常出现千百次），只需要"一个模板 + 计数"就能传达。

**定位**：确定性管线（规则预过滤 → 模板聚类 → 簇摘要），**不调 LLM**。
LLM 只在第 4 步之后对**簇摘要**精读，提取"发生了什么、哪个组件、什么异常类型"。

## 核心方法

三段式：

### 1. 规则预过滤（`LogNoiseFilter`）
- **噪音模板黑名单**：子串匹配（大小写不敏感）heartbeat / health check / keepalive /
  ping / lease renewal / metrics flushed / worker poll loop / connection pool acquired / cache refreshed。
  命中的日志**不进异常簇**（只进统计字段 `noise_count`）。
- **level 门槛**：默认 `min_level=warn`，debug/info 只统计不分析。
  level 排名：`debug/trace < info < warn/warning < error < fatal/critical`。

### 2. Drain 简化版模板聚类（`cluster_logs`）
按"归一化后的模板字符串"把日志归桶：
- **变量归一化**（`normalize_template`，顺序为长优先）：
  1. `key=value` / `key:value` 形态（id/uuid/trace_id/request_id/ip/port/cost/…）→ `{key}`（保留键名，便于阅读）；
  2. `IP[:port]` → `{ip}`（**必须先于通用数字**，否则 IP 段被 `\d[\d.,:]*` 吃掉）；
  3. 数字 token（含 0x 十六进制、千分位、小数、冒号时间）→ `{num}`；
  4. 空白压缩为单空格。
- 例：`ERROR payment timeout after 15000 ms, retry 3 times` → `ERROR payment timeout after {num} ms, retry {num} times`。
- 这就是 Drain 的"模板树"最简形态：不做深度前缀树，直接用模板串做 dict key——够用版。

### 3. 簇摘要（`LogCluster.to_summary` / `ClusterResult.to_summary`）
每个簇输出：模板、计数、最高级别、服务（去重）、时间范围、最常见异常类型、
错误占比（level≥error 的占比）、代表样本（默认 3 条，可截断）。
LLM 只读这个摘要。

### 工程参数
- `max_representatives`（默认 3）：每簇保留样本上限。
- `max_clusters`（默认 50）：簇数上限，超出的低频簇合并进 `other（低频簇聚合）` 簇，防止"一日志一簇"失控。

## 输出契约

```python
@dataclass
class ClusterResult:
    total_logs: int        # 输入日志总数
    noise_count: int       # 规则过滤掉的噪音条数
    clustered_count: int   # 进入聚类的有效条数
    clusters: list[LogCluster]  # 按 count 降序

@dataclass
class LogCluster:
    template: str          # 归一化模板（簇的身份）
    count: int
    level: str             # 簇内最高级别
    services: list[str]
    first_timestamp / last_timestamp
    representatives: list[str]    # 原始样本（截断）
    exception_type: str | None    # 簇内最常见异常类型
    error_ratio: float            # level>=error 占比
```

`ClusterResult.to_summary(max_clusters=10)` 产出供 `Evidence.summary` / LLM 精读的整体摘要：
`日志 N 条，噪音过滤 M 条，有效 K 条，聚成 X 个模板簇` + 各簇摘要。

## 设计边界

- **纯确定性，不调 LLM**。语义层面的"这簇日志说了什么"留给第 4 步 LLM 精读，
  这里只做结构归一，保证喂给 LLM 的是"少而准"。
- **模板聚类只按消息体**，服务/主机作为簇内统计字段；跨服务同名错误会被聚到同一簇
  （错误发生在哪个服务由 `services` 字段表达，而不是拆成多簇）。
- 归一化是**宽容的**：宁可多归并（占位符把不同值归为一类），不可错分。
  如果出现"一个簇里混了两种异常"，靠 `exception_type`（最常见）与代表样本兜底。

## 已知边界与局限

- **默认黑名单是经验值**：真实环境的噪音模板需要按线上日志调整（可传 `noise_markers` 覆盖）。
- **变量归一化不做语义**：`12345` 是订单号还是耗时，归一化后都是 `{num}`——需要区分时靠 key=value 保留键名。
- **不做时间滑动窗口聚类**：整个输入窗口一次性聚类；超长窗口（数小时）需按分钟级滑动分段后再聚合。
- 未做 Drain 的**在线增量**与深度前缀树，最大簇树深度 / token 前缀学习是后续增强（Phase 2）。

## 本地验证

```bash
# 模块测试
.venv/Scripts/python.exe -m pytest tests/test_log_clustering.py -v

# 端到端（mock 数据，CLI 加 --cluster 看噪音过滤 + 簇摘要）
.venv/Scripts/python.exe scripts/run_trace_rebuild.py tr-mock-0001 --cluster  # 故障 trace：8 条噪音被滤，3 个错误簇
.venv/Scripts/python.exe scripts/run_trace_rebuild.py tr-mock-0002 --cluster  # 正常 trace：8 条全被滤，0 个异常簇
```
覆盖：噪音过滤（黑名单 + level）、变量归一化（数字/键值/IP）、同模板归簇、跨模板分簇、
噪音统计、簇元数据（异常类型/错误占比/时间范围）、按计数排序、代表样本截断、
max_clusters 合并（计数守恒）、空输入、异常类型提取、摘要文本，共 17 例。
