# 开放数据集接入与真实数据评估 实现细节

> 模块：`app/tools/rcaeval_datasource.py`、`scripts/eval_rcaeval.py`
> 关联 PRD：§5.2（指标接入）、§9（评测与验收标准）、§9.2（评测集建设）、§10（Phase 3 评估迭代）
> 状态：真实数据源已接入（RCAEval RE1-OB，125 cases），首份真实评估基准已产出

## 解决什么问题

此前所有数据都是 mock（构造的日志/指标）。本模块把**公开开放数据集**（RCAEval benchmark）
接入现有 `MetricQuery` 协议，让 Agent 第一次跑在**真实采集的指标数据**上，产出可对比的
评估基准（PRD §9.1 的 Top-1/Top-3 命中率）。

**为什么选 RCAEval RE1-OB**：
- 公开可下载（zenodo，31MB——RE2/RE3 是 245MB~2.8GB，本环境网络不稳拉不动）
- 真实采集（Online Boutique 微服务 + Prometheus 采集，非合成数据）
- **带标注**：case 目录名 `{service}_{fault}` 即 ground truth（根因服务 + 故障类型）
- 纯指标（375→实际 125 cases），正好验证指标驱动的场景路由 + 假设打分主线

**数据格式**（RE1 系列）：
```
RE1-OB/{service}_{fault}/{instance}/
    inject_time.txt   # 故障注入时刻（epoch 秒）= 标注锚点
    data.csv          # 宽表时序：time=epoch 秒，其余列 = {service}_{metric}
```

## 核心方法

### 1. 适配器（`rcaeval_datasource.py`）

`RcaEvalMetricSource` 实现 `MetricQuery` 协议，Agent 编排层不感知底层差异：

- **索引**：`rglob("inject_time.txt")` 递归找 case 叶子（zip 解压常多套一层目录），
  轻量扫描（只读目录 + inject_time，指标序列惰性加载，375 cases 不全量驻留内存）。
- **查询**：`query_metric(metric, time_range, labels={"case": "..."})`——用 labels 的
  case 上下文定位数据（协议兼容）。
- **事件构造**：`incident_for(key)` 把 case 转成 `IncidentEvent`（告警自动触发，
  RCA-001），事件文本用 case 标注，时间窗 = 注入前后 30 分钟。
- **ground truth**：`case.ground_truth` = `{service}_{fault}`（评估判定用）。

### 2. 评估脚本（`eval_rcaeval.py`）

对每个 case：构造 incident → 场景路由（纯规则）→ 假设打分（纯规则）→ 判定
Top-1/Top-3 是否命中 ground truth 根因服务。输出汇总（命中率/场景分布/平均候选数）。

### 3. 真实数据暴露的问题（→ 修复）

**第一版评估 Top-3 命中 58.3%，平均候选数 1.0**。诊断发现假设生成的缺陷：

- **`_build_metric_hypotheses` 用"列表第一个异常"定位服务**——`scenario.raw_anomalies`
  不是时间序也不是幅度序，是输入序。真实数据下 `checkoutservice_latency` 先异常
  ≠ 根因是 checkoutservice（真实案例：adservice 故障 ratio 46x 但列表第一个是
  checkoutservice 的弱异常）。
- **修复**：改为按服务分组，**每个异常服务生成一个假设**（Top-3 多样性），
  **幅度最大（|ratio| 最大）的服务优先**（幅度 = 信号强度，比列表顺序可靠）。

**修复后全量 125 cases：Top-1 83.2%、Top-3 95.2%、平均候选 2.8**——达到
PRD §9.3 的 MVP 验收门槛（Top-3 ≥ 60%）。

### 4. 6 个 miss 的根因（已知边界，如实记录）

全部是 **loss（网络丢包）** 故障：
- `adservice_loss/4`：检测出 **0 个异常指标**（技术信号干净，落 other）——丢包
  故障在该 case 的指标数据里无明显异常形态，检测器抓不到。
- `productcatalogservice_loss/*`：异常在**下游**（adservice/cartservice 延迟上升），
  根因服务自身指标正常——真实分布式故障的"根因隐藏在下游"特征，当前假设模板
  定位不到根因服务。

这是真实数据评估的价值：**暴露 mock 数据永远发现不了的能力边界**。loss 类故障
目前检测不到 → 落 other 强制人工介入（PRD §12 的兜底设计正确承接了它）。

## 输出契约

```python
# rcaeval_datasource.py
class RcaEvalMetricSource(MetricQuery):
    def __init__(self, data_root: str)
    def list_cases(self) -> list[str]          # ["adservice_cpu/1", ...]
    def case(self, key: str) -> RcaEvalCase    # service / fault / instance / inject_time / ground_truth
    def incident_for(self, key: str) -> IncidentEvent
    def metric_series(self, key: str, metric: str) -> MetricSeries
    def anomaly_series(self, key: str) -> list[MetricSeries]  # 全列指标序列

# eval_rcaeval.py（CLI）
python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB [--limit N] [--case key] [--verbose]
```

## 设计边界

- **只读数据**：适配器不写任何东西（失败抛 `RcaEvalDataError`，工作流节点已降级）。
- **RE1 只有指标**：无日志/trace，所以跳过 trace/日志步骤，只验证指标主线。
- **数据集不入库**：数据在 `E:/QIUZHAO/rca-data/`（项目外），适配器按路径引用，
  不污染仓库。下载脚本不提交（数据 31MB 太大）。
- **评估是雏形**：只有 Top-1/Top-3 命中率 + 场景分布，未接入幻觉率/证据引用完整率
  （PRD §9.1 其余指标需报告 Web 层 + 人工核验后补）。

## 已知边界与局限

- **loss 类故障检测不到**（见上）：根因是异常检测器对丢包形态的指标特征不敏感
  + 根因服务隐藏。建议后续：接 RE2（含日志/trace）后，用 trace 传播方向辅助定位；
  或为 loss 场景补指标模式识别。
- **场景判定偏宽**：`latency_spike` 占 73/125——RE1 的 latency 列（P50/P90）在很多
  故障里都异常，场景区分度有限（但假设打分按服务分组弥补了定位）。
- **`_svc_from_metric` 依赖指标命名约定**：`{service}_{metric}` 是 RE1 的约定，
  真实 Prometheus 指标名可能不同（如 `{metric}_total`），接真实数据源时需确认。
- **RE2/RE3（日志+trace）未接入**：本环境网络拉不动大文件；换网络/机器后补充，
  可验证 trace 重建在真实 trace 上的表现（本项目关键路径）。

## 本地验证

```bash
# 适配器测试（9 例）
.venv/Scripts/python.exe -m pytest tests/test_rcaeval_datasource.py -v

# 真实数据评估（全量 125 cases，需先下载并解压 RE1-OB）
.venv/Scripts/python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --limit 125
.venv/Scripts/python scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --case adservice_cpu/1 --verbose

# 数据下载（首次，31MB，网络需能访问 zenodo）
# https://zenodo.org/records/14590730 → RE1-OB.zip → 解压到 rca-data/
```
