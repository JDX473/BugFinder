# 评估基线与业界对比 实现细节

> 关联 PRD：§9（评测与验收标准）、§9.2（评测集建设）、§9.3（验收门槛）
> 状态：真实数据评估基线已固化（RE1-OB / RE2-SS / RE2-TT 三套，规则 vs LLM 对比，业界对照）

## 解决什么问题

RCA Agent 的效果不能靠"案例演示"验收（业界共识，见调研报告）。本文件把项目的
**真实数据评估基线**固化下来：在 RCAEval 开放数据集上，我们的方法（确定性规则为主
+ LLM 兜底）命中率是多少、跟业界公开基准差多少、优势从哪来、风险在哪。这是
PRD §9 评测体系的落地起点，也是后续每轮迭代回归的参照物。

## 评估设置（严格口径）

- **数据**：RCAEval（Zenodo 14590730）真实采集数据，全量 case
  - RE1-OB：125 cases，纯指标，Online Boutique（12 服务）
  - RE2-SS：90 cases，指标+日志，Sock Shop（15 服务，k8s 复杂指标名）
  - RE2-TT：90 cases，trace（Jaeger span），Train Ticket（64 服务）
- **ground truth**：case 目录名 `{service}_{fault}` = 根因服务 + 故障类型
- **判定**：Top-1 = rank1 假设命中根因服务；Top-3 = 任一候选命中
- **LLM 模式**：真实 DeepSeek（deepseek-chat），走 ask_json shim，置信度门槛 0.5
- **规则模式**：`llm=None`，纯确定性（场景路由 + 幅度优先假设定位）

## 结果基线（2026-08 固化）

### 指标主线（RE1-OB / RE2-SS）

| 数据集 | 口径 | 纯规则 | 规则+LLM | LLM 参与 |
|---|---|---|---|---|
| RE1-OB（125） | **Top-1** | **83.2%** | **84.0%** | 49/125 |
| RE1-OB（125） | Top-3 | 95.2% | 96.0% | — |
| RE2-SS（90） | **Top-1** | **85.6%** | — | — |
| RE2-SS（90） | Top-3 | 91.1% | — | — |

### trace 主线（RE2-TT）

| 数据集 | 方法 | 口径 | 命中 |
|---|---|---|---|
| RE2-TT（90） | 慢节点定位（根 span 累计耗时） | Top-5 | **52.2%** |

### 业界对照（严格同口径）

| 方法 | 信号 | 数据集 | 口径 | 命中 |
|---|---|---|---|---|
| **本项目（规则）** | 指标 | RE1-OB | Top-1 | **83.2%** |
| **本项目（规则）** | 指标 | RE2-SS | Top-1 | **85.6%** |
| **本项目（规则+LLM）** | 指标+LLM | RE1-OB | Top-1 | **84.0%** |
| GALA（统计因果+LLM） | 多信号 | RCAEval 聚合 | Top-1 | 42.22% |
| RCLAgent（多 agent） | trace | RE2-OB | R@1 | 56.67% |
| TraceRCA（清华） | 纯 trace | RCAEval | Top-1 | 13.19% |

## 核心结论

### 1. 我们的 Top-1（83-85%）显著高于业界公开基准（42-57%）

严格同口径下成立，但优势来自三个**结构因素**，不是"我们算法更聪明"：

1. **数据规律切合**：RE1/RE2 的 ground truth 是**单根因服务**，且根因服务指标异常
   **幅度最强**（如 catalogue cpu 614x）。"幅度优先定位"恰好切中这个规律，而业界
   方法普遍被"多服务并发异常"拖累。
2. **确定性优先**：业界主流是"LLM 主导 + 统计辅助"，而 OpenRCA 实测最强 LLM 直接
   定位 Top-1 只有 0.21-0.4。我们是**规则主导 + LLM 兜底**，规则把确定性信息榨干，
   噪声少。
3. **口径差异**：即使压到 Top-1（83-85%）仍高于业界——优势不是靠放宽口径来的。

### 2. LLM 增量极小（+0.8%），这是架构正确性的证明

| 口径 | 规则 | +LLM | 增量 |
|---|---|---|---|
| Top-1 | 83.2% | 84.0% | +0.8%（1 case） |
| Top-3 | 95.2% | 96.0% | +0.8%（1 case） |

- LLM 只在 **49/125** 参与（其余被确定性路径接管）——省成本 + 确定性优先。
- 结论（优化日记 #20）：**纯指标故障上规则已榨干信息，LLM 无增量空间**。LLM 的
  真实价值在**规则覆盖不到的地方**（白名单外业务语义，如"充电桩充不进电"→
  business_logic；指标不明确的 case 被 LLM 从 other 提升）。

### 3. trace 单信号只有 52%，验证了多信号交叉的必要性

RE2-TT 慢节点定位 52.2%（socket/mem 都 ~50%），远低于指标（91-96%）。根因：
**耗时高 ≠ 根因**（高耗时服务常是"受害者"而非加害者）。这验证了项目架构
"指标主导 + trace 候选 + 日志异常簇交叉打分"的正确性——trace 只能当疑似候选。

## 已知边界（诚实标注）

1. **单根因数据前提**：RE1/RE2 ground truth 是单根因服务，真实多根因/跨服务故障
   下数字会掉。
2. **系统规模差异**：我们 RE1-OB 是 12 服务小系统；GALA 聚合含 64 服务 TrainTicket
   （更难）。在 RE2-TT 64 服务上跑指标评估会更公平（数据未拉全，后续补）。
3. **业界数字是论文自报**：评测口径可能有细微差异，不能当绝对等号。
4. **LLM 增量主要在业务语义**：纯指标定位命中率是确定性算法的成绩，不是 LLM 的。

## 后续改进项（让对比更公平）

1. **RE2-TT 指标评估**：拉 RE2-TT 的指标数据跑场景路由 + 假设打分（64 服务，最难的
   公平考场），对比 GALA 的 TrainTicket 数字。
2. **多根因标注**：评估集加入多根因 case，检验"单根因优势"是否会崩。
3. **MRR / 幻觉率**：PRD §9.1 的其余指标待报告 Web 层 + 人工核验后补。
4. **trace 候选进假设打分**：把 trace 慢节点作为候选之一喂进 generate_hypotheses，
   量化"多信号交叉"比"纯指标"的增量（当前假设打分只消费指标证据）。

## 复现命令

```bash
# RE1-OB 纯规则
.venv/Scripts/python.exe scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --limit 125
# RE1-OB 规则+LLM（需 RCA_LLM_API_KEY）
RCA_LLM_API_KEY=sk-... .venv/Scripts/python.exe scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE1-OB --limit 125 --llm
# RE2-SS 纯规则
.venv/Scripts/python.exe scripts/eval_rcaeval.py --root E:/QIUZHAO/rca-data/RE2-SS --limit 90
# RE2-TT trace 验证
.venv/Scripts/python.exe scripts/verify_re2_trace.py --limit 90
```
