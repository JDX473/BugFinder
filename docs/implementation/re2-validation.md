# RE2-SS 真实数据验证 实现细节

> 模块：`app/tools/rcaeval_datasource.py`（适配器扩展）
> 关联 PRD：§5.2（指标接入）、§9（评测）、硬约束 1（日志+指标+traceId）
> 状态：RE2-SS 已下载验证（90 cases，真实 k8s 指标 + 真实日志），Top-3 91.1%

## 解决什么问题

RE1-OB 只有简洁指标（`{service}_{metric}`）。RE2-SS 是**更真实的 k8s 环境数据**：
复杂指标名（`catalogue_container-cpu-usage-seconds-total`、`gke-...-node-...`）、
空值脏数据、真实日志（Go 服务结构日志/access log）。验证管线在"更脏、更真实"的数据上
是否仍然成立，并暴露 mock 假设的失效点。

## 数据特征

| | RE1-OB | RE2-SS |
|---|---|---|
| 指标文件 | `data.csv` | `metrics.csv`（+ `simple_metrics.csv`） |
| 指标名 | `{service}_{metric}` | k8s 前缀 `{service}_container-*` / `istio-*` / `node-*` |
| 日志 | 无 | `logs.csv`（**无 traceId**，`container_name/message/level/req_path/error`） |
| 脏数据 | 无 | 空值（`''`） |
| cases | 125 | 90 |

## 适配器扩展

1. **文件命名探测**：`load_case_csv` 按存在性探测 `data.csv`/`metrics.csv`/`simple_metrics.csv`
   （RE1 vs RE2 差异，优化日记 #21）。
2. **跳空值**：`metric_series`/`anomaly_series` 跳过 `''` 脏数据点（#22）。
3. **日志聚类**：RE2 日志全 info 级，`cluster_logs` 用 `min_level=info` + 噪音黑名单
   （mock 的 `min_level=warn` 假设在真实数据失效，#23）。

## 验证结果

### 指标检测 + 场景路由 + 假设打分（90 cases）

| 指标 | RE2-SS | RE1-OB |
|---|---|---|
| Top-1 命中 | **85.6%** | 83.2% |
| Top-3 命中 | **91.1%** | 95.2% |
| 场景分布 | resource 71 / latency 17 / error 2 | latency 73 / resource 47 / error 4 |

**关键验证**：RE2 更复杂（k8s 指标名 + 脏数据），但表现依旧稳——**幅度优先定位**让根因
服务的强信号压过 node 级弱干扰。示例：`catalogue_socket` 故障下，catalogue cpu 614x
幅度最大 → rank1 正确命中 catalogue（即使 `gke-...-node-...` 被解析错，也被压到 rank2）。

### 日志聚类（真实日志）

RE2 日志（44K 行/case）用 `min_level=info` 聚类工作正常：模板归一化正确（时间戳/耗时/id
被占位）、按服务分组正确。但 socket 故障的异常信号**不在日志内容**（全是正常请求
`result=1`），在指标——日志聚类对这类故障定位帮助有限（诚实结论）。

### 局限：RE2-SS 无 traceId

`logs.csv` 列无 traceId/rpc_direction → **无法验证 trace 重建**。本项目关键路径
（rebuild_trace）仍需带 traceId 的数据（RE2-TT 或自建）。RE3 或 RE2 其他系统版本待后续。

## 已知边界

- **loss 故障 6/8 miss**：与 RE1 一致的已知边界（根因服务指标正常/隐藏）。
- **2 个 delay miss**：RE2 新增的 delay 形态，幅度优先未能区分（后续看指标语义）。
- **k8s 指标名服务提取靠幅度兜底**：`_svc_from_metric` 对 `gke-...-node-...` 解析错，
  但幅度优先让根因服务仍排第一。真实 Prometheus 需语义化解析（优化日记 #24）。

## 本地验证

```bash
# RE2-SS 评估（需先下载并解压）
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0, 'E:/QIUZHAO/RCA')
from scripts.eval_rcaeval import _evaluate_case
from app.tools.rcaeval_datasource import RcaEvalMetricSource
src = RcaEvalMetricSource('E:/QIUZHAO/rca-data/RE2-SS')
for k in src.list_cases()[:5]:
    r = _evaluate_case(src, k); print(k, r['top3'])
"

# 真实日志聚类
.venv/Scripts/python.exe -c "
from app.schema.models import LogRecord
from app.pipeline.log_clustering import cluster_logs, LogNoiseFilter
# ... 加载 RE2 logs.csv，min_level=info 聚类
"

# 数据下载（zenodo，需网络）
# https://zenodo.org/records/14590730 → RE2-SS.zip（245MB）→ 解压
```
