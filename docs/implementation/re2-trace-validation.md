# RE2-TT 真实 trace 验证 实现细节

> 模块：`scripts/verify_re2_trace.py`（trace 能力验证脚本）
> 关联 PRD：§5.3（traceId 重建调用链，关键路径）、硬约束 1
> 状态：RE2-TT 已下载验证（90 cases，Train Ticket 64 服务，真实 Jaeger span）

## 解决什么问题

RE1/RE2-SS 只有指标/日志，**验证不了 trace 能力**。RE2-TT 是唯一含 `traces.csv`
的套件——Train Ticket（64 服务）的 Jaeger 采集 span 表。本项目核心路径是
"traceId 重建调用链 + 定位慢/错节点"，这里用真实 span 数据验证它。

**诚实的边界**：RE2-TT 的 traces.csv 是**完整链路存储**（span 表，含 parentSpanID），
而项目硬约束是"只有日志按 traceId 重建"（无完整存储）。所以 RE2-TT 验证的是
"给定 traceId 还原调用链 + 定位慢/错节点"这一**核心能力**（数据形态更接近理想
链路存储，比日志重建更乐观）。

## 数据格式

```
traces.csv（每 case 16 万行）:
  time, traceID, spanID, serviceName, methodName, operationName,
  startTimeMillis, startTime, duration, statusCode, parentSpanID
```
- `traceID` + `parentSpanID` → 调用树结构（Jaeger 语义）
- `duration`（µs）→ 耗时，根 span duration 已含子树耗时
- `statusCode` → 本数据集全空（Train Ticket HTTP 调用未填错误码）

## 验证方法（`verify_re2_trace.py`）

1. 按 `traceID` 聚合 span，`parentSpanID` 为空者为根 trace。
2. 慢节点定位：根 span duration 超全局中位数 `threshold_factor=5` 倍的服务，累计求和。
3. 判定：根因服务（case 目录名 `{service}_{fault}`）是否在慢节点 Top-5 内。

**性能修复（优化日记 #25）**：最初用递归算子树耗时（O(span×深度)），真实数据
16 万 span/case 下 10 case 超 120s。改为直接取**根 span duration**（Jaeger 已含
子树耗时），O(span) 单遍，全量 90 cases 可跑。

## 验证结果

| | RE2-TT trace 慢节点定位 |
|---|---|
| 根因服务命中慢节点 Top-5 | **47/90 = 52.2%** |
| 平均每 case 根 traces | 6263 |

按故障类型：mem 命中率 50%，socket 命中率 53%——**两类都约等于抛硬币**。

**结论（诚实）**：单靠 trace 慢节点定位在真实分布式数据上命中率只有 ~52%，
**显著弱于指标信号**（RE2-SS Top-3 91.1%）。根因深挖：

- **耗时高 ≠ 根因**：socket 故障下 `ts-auth-service` 耗时 376844ms 排第一，但根因
  是 `ts-travel-service`（21605ms）。高耗时服务可能是**受害者**（请求堆积/排队）
  而非加害者。
- 高吞吐服务的累计耗时天然高，不是故障信号。

**架构启示**：这验证了项目"多信号交叉验证"设计的必要性——trace 慢节点只能作为
**疑似候选**，必须与指标（幅度优先）、日志（异常簇）交叉打分，不能单靠 trace。
RE2-SS 指标 Top-3 91.1% 证明了指标主导的假设打分是主线，trace 作为辅助。

## 已知边界与局限

- **RE2-TT 无日志重建路径**：span 表 ≠ 日志，不能验证 `rebuild_trace`（从日志
  聚合重建）本身，只验证了"给定 traceId 还原调用链"的能力。日志重建仍需真实
  带 traceId 的日志数据（如 RE3 或自建）。
- **statusCode 全空**：无法验证"错误节点"定位，只验证了"慢节点"。
- **慢节点定位是启发式**：`threshold_factor=5` + 累计求和是经验参数，需按真实
  数据调参；"耗时高≠根因"的根本局限需靠多信号交叉解决。

## 本地验证

```bash
# 单 case 验证
.venv/Scripts/python.exe scripts/verify_re2_trace.py --case ts-travel-service_socket/1 --verbose
# 全量 90 cases（需 RE2-TT.zip 已下载）
.venv/Scripts/python.exe scripts/verify_re2_trace.py --limit 90

# RE2-TT 数据（2.8GB，zenodo，块式续传下载）
# https://zenodo.org/records/14590730 → RE2-TT.zip
```
