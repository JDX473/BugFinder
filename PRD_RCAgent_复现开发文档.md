# RCAgent 论文复现 — 产品需求文档 (PRD)

| 项目名称 | RCAgent: Cloud Root Cause Analysis by Autonomous Agents with Tool-Augmented LLMs |
| --- | --- |
| 论文出处 | arXiv:2310.16340 (2023-10-25, Alibaba Group / Tsinghua / NJU / Harvard / Anytime.AI) |
| 文档版本 | **v1.2**(v1.2 变更:确认 Embedding 使用 API(模型待用户提供,接口配置化预留);目标服务待用户提供,备选方案为自建 demo 服务;M1~M5 框架开发不依赖目标服务) |
| 创建日期 | 2026-08-19(首次),2026-08-19(v1.1),2026-08-19(v1.2) |
| 文档性质 | 论文复现开发依据文档,以论文原文为准 |

---

## 1. 项目概述

### 1.1 项目目标

复现论文 **RCAgent** —— 一个**工具增强自主智能体(Agent)**框架,用于**云上服务异常根因分析 (RCA)**。

**已确认的开发方向(与用户确认的决策)**:
- **LLM 基座:优先使用云端 API 模型(如 DeepSeek)跑通全流程**;中小规模本地模型(8B~14B 档)作为后续阶段目标,用于复现论文"本地模型 + 稳定化方法"的原始设定。
- **评估器:使用 API 模型(DeepSeek)** 替代论文的 gpt-4-0613。
- **推理硬件:不使用本地 GPU**,全部 LLM 能力来自云端 API。
- **Embedding:使用 API 模型,具体模型由用户后续提供**;代码侧以可配置接口预留(FR-13/FR-15),模型到位前用占位实现(OpenAI 兼容协议)保证开发不阻塞。
- **目标服务:用户后续提供;若用户不提供,则由本项目自建 demo 服务作为示例落地**。demo 方案候选见 §2.11;M1~M5(框架开发)不依赖目标服务,可先行。
- **架构定位:通用服务适配** —— 框架本体与具体服务解耦,可适配任意服务(包括业务服务),不局限于 Flink。

复现范围分为三个层级,按优先级递进:

| 层级 | 内容 | 判定标准 |
| --- | --- | --- |
| **L1 框架复现** | 完整实现 RCAgent 的 Agent 循环、OBSK、Expert Agents、JsonRegen、错误处理、TSC 等全部机制,并验证其对**任意目标服务**的可适配性 | 系统能对目标服务的异常自主完成 thought-action-observation 循环,并输出四项 RCA 结果 |
| **L2 实验复现** | 为目标服务构建可评估的数据集与评估流水线 | 复现出"RCAgent 全面优于 ReAct"的相对结论(数值不必与论文一致) |
| **L3 消融复现** | 复现 RQ2 消融实验与 RQ3 轨迹稳定性实验 | 各组件(LLM experts / JsonRegen / OBSK)移除后性能下降的**相对趋势**与论文一致 |

> 注意(论文 §VI-B2 依据):即使基座换成 API 模型,JsonRegen 等稳定化方法**仍然必要**——论文明确观察到 ChatGPT 在内容复杂度上升时也会产生错误的 JSON 结构。因此 API 模型阶段不能跳过稳定化模块。

### 1.2 成功标准(顶层)

1. **通用性**:框架核心(§2.2 中除工具实现外全部模块)与目标服务零耦合;适配新服务只需实现工具集 + 领域知识 + 知识库(见 §2.11)。
2. 系统在 L1 上可端到端运行:给定目标服务的异常实例,自动完成数据收集 → 分析 → 输出 `{root_cause, solution, evidence, responsibility}`。
3. L2 上,RCAgent 在 METEOR / BERTScore / EmbScore / NUBIA / BLEURT / BARTScore 及 Win Rate 上**整体优于 ReAct 基线**(与论文方向一致)。
4. L3 上,消融实验呈现论文中的性能梯度:**RCAgent > w/o Obs Head > w/o JsonRegen > w/o LLM experts > ReAct**。若 API 模型过强导致梯度被压缩,则该条降级为"移除任一组件后性能不反弹、主要指标单调下降的趋势存在"。
5. 轨迹稳定性:Pass Rate ≥ 90%,Invalid Rate 显著低于 ReAct(论文 7.93% vs 22.82%)。

### 1.3 复现难点总览

| 难点 | 说明 | 对策(详见 §8/FR-12) |
| --- | --- | --- |
| 数据不可得 | 论文使用阿里云 Flink 平台私有数据 + Flink Advisor 私有知识库 | 目标服务适配:按 §2.11 的方法为一个可用的目标服务构建数据集与知识库 |
| 目标服务未定 | 框架通用,但工具/知识注入需要具体落点 | ★ 待与用户确认目标服务;未确认前按"通用框架 + 示例服务(如开源微服务 demo)"推进 |
| 基座模型偏移 | 论文用弱本地模型(13B),API 模型更强 | API 模型先跑通功能;L3 消融若梯度不显,后续阶段换本地中小模型(8B~14B) |
| 私有标注 | 论文由阿里 SRE 团队人工标注 | 目标服务数据集上自行标注 + LLM 辅助标注 |
| API 成本与速率限制 | TSC 采样 K=10、多轮实验会放大 API 调用量 | 轨迹缓存、并发控制、采样数可配置;预算估算(见 FR-10/§5) |

---

## 2. 论文深度解析(复现依据)

### 2.1 论文要解决的问题

云上流式计算作业(如 Apache Flink)出现异常(不可恢复失败、卡死)时,SRE 需要人工排查。传统 AIOps 方法依赖历史数据,存在数据质量差、分布漂移、标注成本高、泛化差等问题。LLM 用于 RCA 的已有工作(RCACopilot、PACE-LM、Oasis 等)存在三个缺陷:

1. **依赖 GPT 系列外部 API**,云生产数据传出有隐私风险(论文核心动机之一,本项目因使用 API 模型,此动机退化为"设计目标",即框架预留本地模型切换能力);
2. **依赖人工设定的工作流**(如人工编写的排障指南),无法自主决策;
3. **未充分发挥 LLM 的决策与交互能力**(ReAct 式 agent 在 AIOps 领域未被采用)。

RCAgent 是**第一个面向真实云 RCA 的工具增强 LLM 自主智能体**。

### 2.2 系统组成(论文 §III)

```
┌─────────────────────────────────────────────────────────────────┐
│                        Controller Agent (LLM)                     │
│   Prompt 三件套: ① Framework Rules (thought-action-observation   │
│   循环规则)  ② Task Requirements (RCA任务指令 + 领域知识 + 责任   │
│   判定规则,见图6)  ③ Tools Documentation (全部工具文档)          │
│   决策输出格式: JSON ({"function": ..., "kwargs": {...}})        │
│   出口: finalize 工具(自由决定何时收尾,输出四项RCA结果)           │
└──────────────────────────────┬──────────────────────────────────┘
                               │ 解析后的行动 (Parsed Action)
        ┌──────────────────────┼──────────────────────────┐
        │                      │                          │
┌───────▼───────┐     ┌────────▼────────┐        ┌───────▼────────┐
│ 信息收集工具    │     │  Expert Agents   │        │  OBSK 键值库   │
│ (简单参数,     │     │  (LLM 作为工具)   │        │  snapshot key │
│  模糊去重)     │     │  代码分析工具      │        │  → 完整观察    │
│  · 目标服务    │     │  日志分析工具      │        │               │
│    专属工具    │     │  (Algorithm 1)   │        │               │
└───────────────┘     └────────┬────────┘        └───────────────┘
                      ┌────────▼────────┐
                      │ JsonRegen (Alg 2)│── 所有结构化输出的修复层
                      └────────┬────────┘
                      ┌────────▼────────┐
                      │  错误处理反馈     │── 重复调用/trivial输入/过早finalize
                      └─────────────────┘
   环境: 日志系统 / 数据库 / 代码仓库 / 知识库 — 仅可访问异常检测时刻之前的数据
└──────────────────────────────────────────────────────────────────┘
   解码策略: 默认 greedy;超长(>4096 token)重启并 +0.5 repetition/frequency penalty
   Self-Consistency: 文本级投票/LLM聚合 + 轨迹级 TSC(从倒数第二步起采样, K=10)
```

### 2.3 核心创新点与论文依据对照表

| # | 创新点 | 论文出处 | 机制概述 |
| --- | --- | --- | --- |
| 1 | **OBSK** (Observation Snapshot Key) | §III-A, 图2/图3 | 观察内容只展示 head 给 controller,长文本映射为 hash ID 存入键值库;snapshot key 可作为工具参数传递,由外部方法(如 log agent)处理长数据 |
| 2 | **工具设计范式** | §III-B | 信息收集工具语义极简(只收实体 ID),避免 LLM 无效探索;返回数据模糊匹配去重,防重复退化 |
| 3 | **Expert Agents** | §III-B2 | LLM 作为分析工具:代码分析工具(递归读代码+任务队列+LLM总结)、日志分析工具(in-context RAG + Louvain 语义分区 + 证据幻觉过滤) |
| 4 | **JsonRegen** | §III-C1, Algorithm 2 | 推理前敏感字符替换 → 括号匹配提取 JSON → 转 YAML 再恢复,多轮重试直到可解析 |
| 5 | **错误处理** | §III-C2 | 三类预定义错误(重复调用无状态工具 / expert 的 trivial 输入 / 过早 finalize)给出错误消息与建议 |
| 6 | **解码稳定化** | §IV-A | greedy 默认 + 自适应惩罚(超 4096 token 重启,+0.5 repetition/frequency penalty,可迭代) |
| 7 | **TSC** | §III-D2, 图5 | 轨迹级 Self-Consistency:仅在进入 finalize 时从倒数第二步开始采样,共享前期步骤 |
| 8 | **文本 SC 聚合** | §III-D1 | Embedding 投票(距均值最近者胜)或 LLM 聚合(K=10) |
| 9 | 隐私优先(设计目标) | 全文 | 论文禁止 GPT 外部 API;本项目第一阶段使用 API 模型,框架保留本地模型切换能力,隐私条款见 §5 |

### 2.4 关键算法原文复述

#### Algorithm 1 — Log Expert Agent(日志分析专家,论文 §III-B2)

**输入**:日志 `L`,最大 prompt 长度 `N`
**输出**:解释集合 `R̃`(interpretations),证据集合 `Ē`(evidences)

```
1:  S ← 按分隔符(如换行)切分 L 得到行集合
2:  v_s ← EMBEDDINGMODEL(s),对每个 s ∈ S            # 行级 embedding
3:  W = {w_ij} 空权重矩阵
4:  for (s_i, s_j) ∈ S×S,其中 j − i ∈ (0, 200]:    # 窗口内配对
5:      d_ij ← s_i 与 s_j 在 L 中的位置距离
6:      sim_ij ← v_si 与 v_sj 的余弦相似度
7:      w_ij ← sim_ij × exp(−d_ij)                  # 距离指数衰减加权
8:  end for
9:  G ← (S, W)                                       # 加权无向稠密图
10: C ← LOUVAINCLUSTERING(G)                         # Louvain 社区检测
11: C' ← GREEDYOVERLAPREMOVAL(C)                     # 贪心去重叠,保证簇内连续
12: P ← 由 C' 指示的分区(日志 chunk)
13: R', E' ← 空(过滤后结果)
14: for 每个分区 p ∈ P:
15:     E, A ← 检索到的排序示例与答案               # 从知识库检索相似日志分析示例
16:     ICP ← 空 in-context prompt
17:     for (e, a) ∈ (E, A):
18:         if ICP 长度未超过 N: ICP ← ICP ∪ (e, a)
19:     R, E ← LLMANALYSIS(ICP, p)                  # 零样本CoT + 答案提取指令
20:     for 每个 (r, e) ∈ (R, E):
21:         if LEVENSHTEIN(e, p) < L(p) − L(e) × 0.9:   # 幻觉过滤:证据必须可在chunk中模糊匹配
22:             R' ← R' ∪ r; E' ← E' ∪ e
23:     end for
24: end for
25: R̃, Ē ← LLMSUMMARY(R', E')                        # LLM 总结全部结果
26: return R̃, Ē
```

**实现要点**:
- 权重公式 `w_ij = sim_ij × exp(−d_ij)`,`exp(−d_ij)` 使远距离行权重指数衰减(只保留 `j − i ∈ (0, 200]` 配对);
- Louvain 聚类要求输出**内部连续**的分区(chunk 必须是原文中连续片段),通过贪心切换最小量聚类标签实现;
- 证据过滤 `LEVENSHTEIN(e, p) < len(p) − len(e) × 0.9`:证据 e 的大部分内容能在 chunk 中逐字找到,否则丢弃(防 LLM 幻觉及对 in-context 示例的抄袭式分析);
- 论文特别指出:要求专家直接**复制日志原文**作为证据,因为长 prompt 会淹没示例与目标数据之间的分隔符。

#### Algorithm 2 — JsonRegen(结构化输出修复,论文 §III-C1)

```
输入: 模型 LLM, prompt P;输出: 结构化输出 O
1:  Sensitive, Clean ← 控制符号及其替代字符对
2:  Escaped, Original ← 错误转义模式及其正确形式
3:  P_clean ← REPLACE(P, Sensitive, Clean)   # 替换P中敏感字符;不影响P中真实JSON对象
4:  J ← LLM(P_clean)
5:  for 重试次数 < 上限:
6:      J ← REPLACE(J, Escaped, Original)    # 修复错误转义模式
7:      J ← FINDJSON(J)                      # 花括号匹配提取JSON
8:      if J 可解析: break
9:      Y ← LLM("Extract structure into YAML", J)      # 理解JSON结构
10:     J ← LLM("Restore to correct JSON", Y)           # 恢复为正确JSON
11: end for
12: if J 可解析: O ← JSONPARSE(J) else: O ← EmptyObject
```

**实现要点**:
- **字符替换表**(论文示例):`"` → `'`;`[` → `<:`;`{` → `<%`。替换只对 prompt 中的**非 JSON 对象文本**进行;
- FINDJSON 用花括号匹配提取 JSON-like 子串;重试上限建议 3 轮(★次要参数);
- 修复层同时应用于 controller 与 expert 的所有结构化生成;
- **API 模型阶段此模块仍然保留**(论文 §VI-B2 明确:ChatGPT 在复杂内容下同样产生错误结构);
- 论文经验:JSONFormer/TypeChat 不适用,复现时不要引入结构化生成库替代。

### 2.5 三类错误处理(论文 §III-C2)

| 错误类型 | 触发条件 | 处理 |
| --- | --- | --- |
| (i) 重复调用 | 无状态工具以相同参数被重复调用 | 向 controller 返回错误消息 + 建议 |
| (ii) Trivial 输入 | 传给 expert agent 的输入过于简单/无信息量 | 同上 |
| (iii) 过早 Finalize | 未充分调查就调用 finalize | 同上,提示继续调查 |

设计动机:LLM 在工具调用中会**传播错误**、限制探索行为;错误反馈可降低无意义动作频率。

### 2.6 Self-Consistency 聚合(论文 §III-D)

**文本级 SC**(两种,LLM 聚合普遍优于 embedding 投票):
- **Embedding 投票**:`argmax_i similarity(a_i, (1/K)·Σ_j a_j)`,a 为语义 embedding,选最接近 K 个样本均值者;
- **LLM 聚合**:提示 LLM 聚合候选,输出"相似形式与长度"的文本。

**TSC(轨迹级 SC,核心创新)**:
- 完整 SC 直接采样多条 thought-action-observation 轨迹**计算开销过大**(expert agent 激活尤其昂贵;API 模型下成本直接翻倍,此动机更强),且从第一步随机采样缺少历史示例会导致大量错误动作;
- TSC 策略:**只有当 controller 进入 finalize 阶段时,才从倒数第二步开始采样**;
- 采样轨迹共享大部分前期步骤;greedy 解码的稳定 action history 起到 few-shot 示例作用(不额外消耗上下文);
- 不约束后续动作步数(0 或多步),直到 finalize 或全局上限;
- 默认采样数 K=10。

**对照配置**:步进 SC(`SC`)——只接受与 greedy 轨迹**同步 finalize** 的样本,仅采样 finalize 前的 CoT 式思考,不允许额外动作。

### 2.7 数据集构建(论文 §IV-B)——原版与通用化对照

**论文原版(阿里私有)**:
```
15,616 异常作业(不可恢复失败 / 6分钟内无法启动)
  → 过滤非平凡(有实质日志内容)≈ 5,000
  → Flink Advisor 知识库(SRE 专家规则集)成功分析 2,154
  → 类平衡约束:相同根因的作业 ≤ 2 个 → 161 个
```
标注四项:`root cause`、`solution`、`evidence`、`responsibility`(Platform / User,规则见附录 A)。标注流程:LLM 总结 Advisor 分析 → SRE 人工校对。标注质量约束:避免无信息模板句。**在线数据集**:36 个超出规则能力的 OoD 案例,语义聚类选取,人工标注两项(responsibility + root cause)。

**数据源**:三级日志(platform/runtime/infrastructure,阿里 SLS)、advisor 服务数据库、advisor 代码仓库、日志分析知识库(Flink Advisor 历史子集,严格排除标注规则)。**时间约束:只能检索异常检测时刻之前的数据**。

**通用化要点**(见 §2.11):上述流程的结构(异常实例收集 → 过滤 → 规则库分析 → 类平衡 → 标注 → 知识库)完全通用,只是"Flink Advisor"换成"目标服务的诊断知识源"。

### 2.8 模型与推理配置(论文 §IV-A)——本项目对照

| 项 | 论文配置 | 本项目(第一阶段) |
| --- | --- | --- |
| 基座模型 | Vicuna-13B-V1.5-16K(本地) | **API 模型(DeepSeek 等)** ★已确认 |
| 推理后端 | vLLM + A100 80GB | 无本地推理,全部走 API |
| 默认解码 | Greedy | 与论文一致(API 参数中关闭采样/设 temperature=0) |
| 自适应惩罚 | 超 4096 token 重启 +0.5/+0.5,可迭代 | 需实现:检测超长输出 → 以新增惩罚参数重发请求 |
| SC 采样解码 | temperature 0.9, top_p 0.6 | 与论文一致 |
| SC 采样数 | K=10 默认;另做规模实验 | K 可配置;API 成本控制见 FR-10 |
| Embedding | GTE-LARGE | **API embedding(模型由用户后续提供)**;论文证明 embedding 对结果影响边际(§VI-A),到位前占位实现不阻塞开发 |

### 2.9 评估体系(论文 §IV-C)

**语义指标**(离线):METEOR、BERTScore(deberta-large-mnli)、NUBIA(6-dim)、BLEURT、BARTScore(F-Score, CNNDM)、EmbScore(`(1+cos)/2`)。
**归一化指标**:`NormScore(p,r) = (Score(p,r) − Score(b,r)) / (1 − Score(b,r))`,基线 `b = "Unclear"`;失败/不完整轨迹**自动填 "Unclear"**。
**轨迹稳定性**:Pass Rate(15 步内成功 finalize 的轨迹占比)、Invalid Rate(无效动作占比)。
**LLM 评估**:原 gpt-4-0613 → **本项目用 DeepSeek API** ★已确认;G-Correctness/G-Helpfulness(0~10)、Win Rate(vs ReAct)。
**人工评估**:论文 7 名 SRE;本项目建议 2~3 名熟悉目标服务者(降权为参考指标)。

### 2.10 论文主要结果(复现的基准参照)

| 结论 | 数值(论文) |
| --- | --- |
| RCAgent vs ReAct 全面胜出 | Root cause Win Rate 72.67%,Solution 69.25%;evidence METEOR 领先 +16.28 |
| TSC(LLM)进一步增益 | Solution: METEOR +3.51, BLEURT +4.50, NUBIA +7.18 |
| 消融梯度(重要性排序) | **LLM experts > JsonRegen > OBSK(观察头)**;移除 experts 时 METEOR 15.15 → 9.60,几近退回 ReAct(6.44) |
| 轨迹稳定性 | RCAgent Pass Rate 99.38% / Invalid Rate 7.93%;ReAct 86.33% / 22.82%;采样解码崩溃至 70.19% / 44.80% |
| SC 规模 | 收益在样本数=20 时趋稳;LLM 聚合 > embedding 投票;单样本时与 greedy 差异微小 |
| Embedding 敏感性 | 与 embedding 模型能力(MTEB)边际相关 |

### 2.11 ★ 架构通用性分析(框架与 Flink 的耦合点)

**结论:框架本体(约 80% 代码)与具体服务零耦合;适配新服务只需替换三处"服务专属件"。**

| 模块 | 与具体服务耦合? | 适配新服务时的操作 |
| --- | --- | --- |
| Controller 循环、OBSK、JsonRegen、错误处理、TSC/SC、解码管理、轨迹记录 | ❌ 无 | 零改动 |
| 日志分析专家(Algorithm 1) | ❌ 无 | 零改动(语义分区+RAG 与领域无关) |
| 代码分析专家 | ❌ 无 | 零改动(输入为类名/文件路径) |
| **耦合点 1:信息收集工具集** | ⚠️ 强 | 按目标服务的数据源重写工具实现;**设计原则不变**:语义极简参数、模糊去重、时间截止约束 |
| **耦合点 2:领域知识注入** | ⚠️ 强 | 重写 Task Requirements 中的领域知识段与责任判定规则(结构沿用 Fig.6 的 Platform/User 两分法,条目换成目标服务的) |
| **耦合点 3:知识库(检索示例)** | ⚠️ 强 | 构建目标服务的"历史诊断记录/故障模式库",替代 Flink Advisor;严格排除标注规则 |
| 工具文档 | ⚠️ 中 | 随工具集重写;文档打磨是 Pass Rate 的关键(论文 §VI-B2 经验) |

**对业务服务的适配路径**(模板):
1. 枚举服务的可观测面:应用日志(级别/来源)、数据库/存储、依赖中间件、代码仓库、配置中心、调用链;
2. 按"语义极简"原则为每个数据面设计 1~2 个工具(参数 = 实体 ID + 时间窗);
3. 编写 Task Requirements 领域段:服务的架构一句话描述、关键组件名、常见故障模式、责任规则;
4. 从历史故障/工单/issue 提炼 20~50 条诊断规则形成知识库(示例-答案对);
5. 收集历史异常实例(失败/超时/启动失败),过滤非平凡,类平衡抽样,LLM 辅助 + 人工标注四项;
6. 验证:Pass Rate ≥ 90% 且 RCAgent > ReAct 后,视为适配完成。

**示例服务候选**(目标服务由用户后续提供;若未提供,本项目自建 demo 服务):
- **方案 A(推荐,自研轻量 demo)**:一个单体业务服务(如订单/交易处理,Java 或 Python)+ 依赖中间件(Redis/MySQL/ES 中 1~2 个),脚本化注入典型故障(依赖连接超时、队列积压、资源不足、序列化错误、配置错误),输出结构化日志 + 代码仓库 + 迷你诊断规则库。完全可控、可复现,覆盖三处耦合点的全部适配动作,且与用户真实服务形态无关。
- 方案 B:开源微服务 demo(Train Ticket / Sock Shop)——日志真实但故障注入与标注工作量大;
- 方案 C:中间件(Elasticsearch/Kafka)——文档生态成熟,但可观测面较窄。

---

## 3. 总体架构设计

### 3.1 复现系统的部署形态

```
┌───────────────────────────────────────────────────────────────────┐
│                        RCAgent 复现系统                            │
│                                                                   │
│  ┌─────────────┐     ┌──────────────────┐     ┌───────────────┐  │
│  │  Controller  │◄───►│  Agent Runtime    │◄───►│  JsonRegen    │  │
│  │  Agent (LLM) │     │  (循环/解析/执行) │     │  (结构化修复) │  │
│  └─────────────┘     └────────┬─────────┘     └───────────────┘  │
│                               │                                   │
│               ┌───────────────┼───────────────┐                   │
│               ▼               ▼               ▼                   │
│       ┌────────────┐  ┌────────────┐  ┌────────────┐              │
│       │ 信息收集工具 │  │ Expert Agents│  │ OBSK 键值库 │             │
│       │ (服务专属件) │  │ (代码/日志)  │  │ (快照存储)  │             │
│       └─────┬──────┘  └─────┬──────┘  └────────────┘              │
│             │               │                                     │
│       ┌─────▼───────────────▼─────────────────────┐               │
│       │          环境适配层 (Environment Adapter)   │               │
│       │  日志源 │ 数据库 │ 代码仓库 │ 知识库(检索示例) │               │
│       └─────┬─────────────────────────────────────┘               │
│             │                                                     │
│  ┌──────────▼───────────┐    ┌───────────────────────┐            │
│  │ 目标服务数据集        │    │ 评估流水线              │            │
│  │ (异常实例+四项标注)   │    │ 语义指标/LLM评估/轨迹统计│            │
│  └──────────────────────┘    └───────────────────────┘            │
│                                                                   │
│  LLM 调用层: DeepSeek API (controller/expert/评估器/聚合) +       │
│  Embedding 服务(本地轻量或 API) + 可选本地模型插槽(后续阶段)      │
└───────────────────────────────────────────────────────────────────┘
```

### 3.2 模块清单与依赖

| 模块 | 名称 | 依赖 | 服务耦合 |
| --- | --- | --- | --- |
| M1 | Controller Agent 循环 | M3/M4/M5/M6 | ❌ |
| M2 | Prompt 管理(三件套组装) | — | 部分(领域段可注入) |
| M3 | 工具层:信息收集 + finalize | 环境适配层 | **⚠️ 服务专属** |
| M4 | OBSK 快照键值存储 | — | ❌ |
| M5 | JsonRegen 结构化修复 | LLM 调用层 | ❌ |
| M6 | 错误处理与反馈 | 轨迹状态跟踪 | ❌ |
| M7 | Expert Agent:日志分析 | 知识库、Louvain、M5 | ❌ |
| M8 | Expert Agent:代码分析 | 代码仓库、任务队列、M5 | ❌ |
| M9 | 解码策略管理 | LLM 调用层 | ❌ |
| M10 | Self-Consistency:TSC + 文本聚合 | M1/M7/M9 | ❌ |
| M11 | 环境适配层(数据源抽象) | 数据集 | 部分 |
| M12 | 数据集构建工具链(服务适配) | — | **⚠️ 服务专属** |
| M13 | 评估流水线 | 指标库 | ❌ |
| M14 | LLM 调用层(API 封装/本地插槽) | — | ❌ |

---

## 4. 功能需求

> 每个 FR 给出:需求描述、输入/输出、论文依据、实现要点、验收标准。

---

### FR-01 Controller Agent 决策循环

**需求**:实现 ReAct 式 thought-action-observation 循环。controller 每步:
1. 基于完整对话历史(初始 memory + 全部历史)生成 Thought + Action;
2. Action 为 JSON 工具调用 `{"function": <工具名>, "kwargs": {<参数>}}`;
3. 系统解析 → 校验 → 执行 → 返回 Observation 或错误反馈;
4. 直到调用 `finalize` 退出。

**论文依据**:§III、图2、§IV-B1。**与 ReAct 的关键差异**:无 few-shot 示例,trajectory-level zero-shot 工具调用;JSON 为数据交换格式。

**输入**:异常实例描述(实体 ID、异常类型、检测时刻)。
**输出**:
```json
{"root_cause": "<文本>", "solution": "<文本>", "evidence": "<文本>", "responsibility": "platform" | "user"}
```

**实现要点**:
1. 循环上限 15 步(与 Pass Rate 定义一致),超限按失败处理(填 "Unclear");
2. 允许纯思考步(CoT 式,无工具调用,供步进 SC 对照);
3. `finalize` kwargs 缺项/解析失败 → 该轨迹标记失败;
4. 对话历史完整保留,超上下文时以 OBSK 为主要压缩手段;
5. 轨迹事件全部落盘(JSONL),供 TSC 与评估;
6. **API 模型适配**:LLM 调用层统一封装,支持 deepseek-chat 等 API;temperature/penalty 参数映射到 API 对应字段(见 FR-09)。

**验收**:对 10 个种子异常实例,15 步内 finalize 成功率 ≥ 90%。

---

### FR-02 Prompt 管理(三件套)

| 部分 | 内容 | 论文依据 |
| --- | --- | --- |
| Framework Rules | thought-action-observation 循环、JSON 输出格式、OBSK 使用规则(快照用于传参而非直接处理长文本)、finalize 说明 | §III-A、§III |
| Task Requirements | RCA 任务、四项输出要求、**目标服务领域知识**(服务架构/组件/故障模式)、责任判定规则(结构沿用附录 A) | §IV-B1、Fig.6 |
| Tools Documentation | 全部工具名/参数/返回/使用场景;语义极简 | §III-B1、§VI-B2 |

**实现要点**:
1. 模板参数化(实体 ID、时间戳、工具文档版本);领域段作为独立模板文件注入(服务适配的耦合点 2);
2. 工具文档打磨迭代流程:统计"调用不存在函数/错误参数化"错误 → 修订文档 → 回归 Pass Rate(论文 §VI-B2 经验);
3. **API 模型下此模块重要性不降**:即使强模型能理解复杂文档,论文仍建议文档精简(保留上下文预算)。

**验收**:Prompt 版本化;领域段可热切换(换服务不改框架代码);文档修订日志。

---

### FR-03 工具层(信息收集工具 + finalize)

**需求**:工具集遵循"语义极简"原则(只收简单参数如实体 ID,隐藏访问细节);返回数据模糊匹配去重。**工具集是服务专属件(耦合点 1)**,框架只定义注册与契约。

**论文依据**:§III-B1;数据源清单 §IV-B2。

**通用工具契约**(每个工具):
- 输入:实体 ID + 可选时间窗(JSON kwargs);
- 返回:`{head: <前N字符>, snapshot: <hash>, truncated: bool, meta: {数据范围/条数}}`;
- 时间截止约束:只访问检测时刻之前的数据;
- 无状态工具调用记录(参数哈希)供 FR-06 使用。

**示例工具清单**(论文 Flink 版,作为模板):
`runtime log` / `platform log` / `infrastructure log` / `advisor db` / `finalize`。
**目标服务适配时**:为服务的数据面(应用日志、数据库、中间件、代码仓库、配置等)各设计 1~2 个工具。

**实现要点**:
1. 注册式工具框架:声明 schema + 实现即自动进入 Tools Documentation(支持 M11 环境适配层切换实现);
2. 去重:对工具返回条目做模糊匹配(编辑距离或 embedding 相似度),消除重复;
3. 工具执行记录物理数据读取范围(审计与复现)。

**验收**:契约测试——同一工具在不同数据源实现下返回结构一致;去重对含 50% 重复行的测试日志有效。

---

### FR-04 OBSK 观察快照键机制

**需求**:
1. 长 observation 只向 controller 展示 head + 快照键(hash ID),完整内容存键值库;
2. controller 以 snapshot key 作为工具参数(如传给 log agent)由外部方法处理长数据;
3. system prompt 明确 OBSK 规则。

**论文依据**:§III-A、图2、图3(53 行省略,`[snapshot: 2975241420]`,`log agent` 以 snapshot 为参)。

**实现要点**:
1. 快照键:内容 hash(可复现),格式对齐论文风格(`[snapshot: <key>]`);
2. head 长度默认 2000~4000 字符(★次要参数);截断时附 `...N lines omitted. [snapshot: <key>]`;
3. 解析层:发现工具参数为 snapshot key 时,从键值库解析真实观察再执行工具;
4. 快照随轨迹生命周期,落盘供 TSC 复跑;
5. 消融开关:`w/o Obs Head`(只给 snapshot)与 `w/o OBSK`(直接截断,无快照)。

**验收**:200k 字符日志的工具返回,controller prompt 观察部分受控;snapshot 传参调用 log agent 成功。

---

### FR-05 JsonRegen 结构化输出修复

**需求**:实现 Algorithm 2 作为所有 LLM 结构化输出的修复层(controller action、expert 返回、SC 聚合中间产物)。

**论文依据**:§III-C1、Algorithm 2。**API 模型阶段仍必需**(论文 §VI-B2)。

**实现要点**:
1. 前置净化:替换 prompt 中非 JSON 文本的敏感字符(`"`→`'`,`[`→`<:`,`{`→`<%`),跳过真实 JSON 对象;
2. 后置流水线:错误转义修复表 → FINDJSON(花括号匹配)→ 容错解析 → 失败则 LLM 转 YAML → LLM 恢复 JSON → 重试(上限 3 轮)→ 超限返回 EmptyObject;
3. 修复率统计与日志(用于文档打磨);
4. 不引入 JSONFormer/TypeChat 等结构化生成库。

**验收**:破坏性测试集(漏引号/错转义/多余逗号/截断/被自然语言包裹)修复率 ≥ 90% 且语义保持。

---

### FR-06 错误处理与反馈

**需求**:三类错误检测与反馈:(i) 无状态工具相同参数重复调用;(ii) expert 的 trivial 输入;(iii) 未充分调查就 finalize。错误消息 + 建议注入 controller,不终止循环。

**论文依据**:§III-C2、图2("Error?" 分支)。

**实现要点**:
1. 错误消息走 JSON 结构化通道(经 JsonRegen 解析后注入);
2. 规则可插拔;错误计入 Invalid Rate 分子。

**验收**:三个场景各构造触发轨迹,错误反馈出现且 controller 能收到;Invalid Rate 统计正确。

---

### FR-07 Expert Agent:日志分析工具(log agent)

**需求**:Algorithm 1 全流程,作为工具 `log agent`,接受长日志(或 OBSK snapshot),返回解释与证据。

**论文依据**:§III-B2、Algorithm 1、图3。

**实现要点**:
1. **语义分区**:行切分 → 行级 embedding(经 FR-15 配置化 embedding 调用,模型由用户提供;到位前用占位 API)→ 加权图(窗口 j−i∈(0,200],`w_ij=sim_ij×exp(−d_ij)`)→ Louvain 聚类 → 贪心去重叠保证连续;
2. **分块分析**:每块一轮;ICP 由知识库检索示例填充至 ≤ N(建议 4096~8192 token,★次要参数);零样本 CoT + 答案提取指令,要求 evidence 逐字复制自日志;
3. **幻觉过滤**:`levenshtein(e,p) ≥ len(p) − 0.9·len(e)` 则丢弃;
4. **总结**:LLM 汇总 → 返回 `{interpretation, evidence}`。

**验收**:已知根因的合成异常日志,解释与人工标注一致率 ≥ 70%(LLM 评估);证据 100% 可模糊匹配原日志。

---

### FR-08 Expert Agent:代码分析工具(code agent)

**需求**:递归代码分析(图4):输入类名 → 检索文件 → LLM 分析并建议相关类 → 任务队列 → 直到无推荐或均为外部依赖 → LLM 总结全部文件返回 controller。

**论文依据**:§III-B2、图4。

**实现要点**:
1. 任务队列去重;循环上限(建议 20 文件,★次要参数);
2. 外部依赖判定:import 解析 + 仓库外路径;
3. 每轮输出 `{summary, suggested_classes[], stop_reason}`;输出经 JsonRegen。

**验收**:迷你代码仓库上输入根类名可遍历全部相关类;外部依赖不进入;无死循环。

---

### FR-09 解码策略管理

**需求**:
1. 默认 greedy(API 映射:temperature=0 或关闭采样);
2. 自适应惩罚:生成超 4096 token 时,以新增 +0.5 repetition/frequency penalty 重发请求(可迭代);API 模型需验证 penalty 参数支持情况,不支持则降级为"检测到重复循环 → 以更强的禁止重复指令重发"(★次要决策);
3. SC 采样:temperature 0.9, top_p 0.6;
4. 全部运行参数落盘。

**论文依据**:§IV-A。

**验收**:重复性生成场景惩罚生效;实验记录含完整解码参数。

---

### FR-10 Self-Consistency 聚合(TSC + 文本聚合)

**需求**:
**A. TSC**:greedy 主轨迹至 finalize → 从倒数第二步(t−1)开始 K=10 条采样子轨迹(继承 1..t−2 历史,采样解码,自由 0~N 步,各自 finalize 或 15 步上限);
**B. 文本聚合**(可配置):Embedding 投票(选与均值 embedding 余弦最高者)/ LLM 聚合(推荐默认);root cause 与 solution 聚合;失败轨迹填 "Unclear" 后参与;
**C. 对照**:步进 SC(仅采样 finalize 前 CoT 思考,须与 greedy 同步 finalize)。

**论文依据**:§III-D、图5、§IV-A、Fig.8。

**实现要点**:
1. TSC 采样起点 = 主轨迹动作历史倒数第二步;子轨迹独立执行(不缓存工具结果);
2. **API 成本控制**(★重要):K 可配置(默认 10);子轨迹间并发;运行前按 K×轨迹长度×单价估算预算;结果缓存(相同主轨迹前缀不重复生成);
3. 实验配置参数化:K(1/5/10/20/30)、聚合方式、方法(无SC/SC/TSC);
4. sanity check:K=1 时与 greedy 差异微小(论文发现)。

**验收**:K=10 时 RCAgent+TSC(LLM) 主要指标 ≥ RCAgent(greedy)(趋势对齐);K=1 差异微小;预算估算模块工作。

---

### FR-11 评估流水线

**需求**:论文 §IV-C 完整评估体系,输出对齐 Table I~VI 的报表。

| 指标 | 实现 | 备注 |
| --- | --- | --- |
| METEOR / BERTScore(deberta-large-mnli) / NUBIA(6-dim) / BLEURT / BARTScore(CNNDM) / EmbScore | 标准库实现 | 全部离线可用;NUBIA/BLEURT/BARTScore 需下载权重 |
| NormScore | 按公式,基线 "Unclear" | 失败/不完整轨迹自动填 |
| G-Correctness / G-Helpfulness(0~10)、Win Rate | **DeepSeek API 评估器** ★已确认 | 评估 prompt 照录论文原文("Judge the correctness..., 0 is completely wrong and 10 is well-matched" / "Judge the helpfulness..., 0 is completely misleading and 10 is very helpful") |
| Pass Rate / Invalid Rate | 轨迹统计 | 15 步上限 |
| H-Helpfulness(0~5) | 人工评估界面(可简化) | 2~3 名评估者,★次要 |

**实现要点**:评估脚本与实验配置解耦,一键重跑;输出 mean±std(SC 跑 10 次);指标自检(已知正确预测应得高分)。

**验收**:固定标注集上输出 Table I~V 结构报表;LLM 评估器评分稳定性(同一结果两次评分方差小)。

---

### FR-12 ★ 目标服务适配与数据集构建

**需求**:按 §2.11 适配路径,为一个目标服务构建:工具集(耦合点1)、领域知识(耦合点2)、知识库(耦合点3)、评估数据集(对齐论文 §IV-B 结构)。**目标服务待确认;未确认前以示例服务(开源微服务 demo 或中间件)推进。**

**数据集规格(对齐论文)**:
- 离线:异常实例 ≥ 100(目标:161;类平衡:相同根因 ≤ 2);
- 在线 OoD 案例 ≥ 20(目标:36;语义聚类选取,超出规则库能力,人工标注 responsibility + root cause);
- 标注四项:`root_cause / solution / evidence / responsibility`;LLM 辅助生成初稿 → 人工校对;避免模板句(如 "The root cause of this anomaly is ...");
- 知识库:目标服务的"诊断规则/历史记录"提炼 20~50 条示例-答案对;**严格排除标注规则(防泄露)**;
- 时间约束:数据带时间戳,工具只访问检测时刻之前的数据;
- 评估豁免:识别超出 agent 环境可达性的案例,人工评估时豁免(对齐论文 §IV-C)。

**实现要点**:
1. 数据采集脚本 + 标注工具(LLM 初稿 + 人工校对界面);
2. 类平衡抽样器;OoD 语义聚类脚本;
3. 数据加载一键可复现(脚本化,非手工拷贝)。

**验收**:数据集完整(异常实例+四项标注+知识库+OoD 案例);泄露检查(知识库中无标注规则文本);加载脚本一键重建。

---

### FR-13 环境适配层

**需求**:将论文的"SLS 日志 / advisor 数据库 / 代码仓库"抽象为可插拔接口。

**实现要点**:
1. 接口:`LogSource.query(instance_id, level, time_range, limit)`、`DbSource.query(...)`、`RepoSource.read_file(path)`、`KB.search(text, top_k)`;
2. 统一返回结构 + 时间截止校验;
3. 实现:本地文件版(采集脚本转储)、SQLite/Postgres 版、预留真实系统版;
4. 提供日志长度分布统计(对齐论文图1 的 context-length 动机)。

**验收**:切换数据源实现,agent 行为不变(契约测试)。

---

### FR-14 轨迹记录与可视化(辅助)

**需求**:JSONL 完整记录每条轨迹(每步 thought/action/parsed/observation-head/snapshot/错误/**送入 LLM 的确切 prompt 文本**/API 元数据),支持回放、Invalid/Pass 统计、TSC 采样树展示。

**验收**:任意轨迹可从记录完整重建(含确切 prompt——API 模型阶段排错的关键)。

---

### FR-15 LLM 调用层(API 封装)

**需求**:统一封装 LLM 推理与 **Embedding** 两类调用:
1. 供应商适配(DeepSeek 优先,预留 OpenAI 兼容接口 —— 两者协议一致,天然支持;**Embedding 同样按 OpenAI 兼容协议预留,模型由用户后续提供,配置化加载,到位前占位实现**);
2. 参数映射:greedy(temperature=0)/采样(0.9, top_p 0.6)/penalty(若 API 不支持则降级策略,见 FR-09);
3. 超时、重试、限速(API 速率限制)、并发控制;
4. **成本计量**:每次调用的 token 数与费用记账(实验预算管理);
5. 本地模型插槽:后续阶段接入 vLLM 时,实现同接口即可切换(对齐论文"隐私优先"设计目标)。

**验收**:DeepSeek 与 OpenAI 兼容协议均可调用;embedding 配置热切换(换模型不改代码);调用失败自动重试;成本统计落盘。

---

## 5. 非功能需求

| 类别 | 需求 |
| --- | --- |
| 隐私 | 第一阶段使用 API 模型,发送数据前需**脱敏配置**(可配置字段白名单/黑名单,如隐藏 job/实例 ID 细节);框架保留本地模型切换能力(FR-15) |
| 成本 | 单条轨迹 API 调用预算估算器;TSC 采样并发;支持 K 降档;实验总预算报告 |
| 性能 | 单条轨迹(15 步)端到端完成时间受 API 延迟约束;日志 agent 分区+分析 ≤ 5 分钟;TSC 子轨迹并发度可配置 |
| 可复现 | 全部实验配置(模型、解码、采样、prompt 版本)落盘;评估随机种子管理 |
| 可观测 | 轨迹级日志、JsonRegen 修复率、工具调用统计、错误分布、API 成本看板 |
| 可扩展 | 工具注册式(FR-03);数据源可插拔(FR-13);服务适配只动三处耦合点(§2.11) |

---

## 6. 技术选型(已确认 + 待确认)

> 更新自 v1.0;★ 项为已确认决策;☆ 项为次要待确认。

| 项 | 论文基线 | 本项目选型 | 状态 |
| --- | --- | --- | --- |
| LLM 基座 | Vicuna-13B-V1.5-16K(本地) | **云端 API 模型(DeepSeek)先跑通全流程;本地中小模型(8B~14B)为后续阶段** | ★ 已确认 |
| LLM 评估器 | gpt-4-0613 | **DeepSeek API** | ★ 已确认 |
| 推理硬件 | vLLM + A100 80GB | **无本地 GPU,全部云端 API** | ★ 已确认 |
| 架构定位 | Flink 场景 | **通用服务适配(含业务服务)** | ★ 已确认 |
| Embedding | GTE-LARGE | **API embedding(模型由用户后续提供)**;代码侧以 OpenAI 兼容协议配置化预留(FR-13/FR-15),模型到位前用占位实现;论文证明 embedding 影响边际,选型压力小 | ★ 已确认 |
| Louvain 聚类 | — | python-louvain / networkx | 默认 |
| Agent 框架 | 自研 | **自研**(不依赖 LangChain 等;OBSK/JsonRegen/TSC 需底层控制) | 默认 |
| 语义指标 | 6 种 | 全部复现(meteor/BERTScore/NUBIA/BLEURT/BARTScore/EmbScore) | 默认 |
| 人工评估 | 7 名 SRE | 2~3 名熟悉目标服务者 | ☆ 次要 |
| 语言 | Python | Python 3.10+ | 默认 |
| 目标服务 | — | **用户后续提供**;不提供则由本项目自建 demo 服务(候选方案见 §2.11) | ★ 已确认(策略) |
| 日志语言 | 英文 | 取决于目标服务;英文优先(与论文对齐) | ☆ 次要 |

---

## 7. 开发里程碑

| 阶段 | 周期(估) | 交付物 | 完成标准 |
| --- | --- | --- | --- |
| M0 收尾确认 | 0.5 周 | Embedding 占位实现就绪;demo 服务候选定稿 | 已完成(选型决议:API 模型/DeepSeek 评估器/无本地 GPU/embedding API 待提供/目标服务待定) |
| M1 骨架 | 1~2 周 | Agent 循环、Prompt 三件套、工具注册框架、finalize、轨迹记录、**LLM 调用层(DeepSeek + embedding 占位)** | FR-01/02/03/14/15 验收;Pass Rate ≥ 90% |
| M2 稳定化 | 1 周 | JsonRegen、错误处理、解码管理 | FR-05/06/09 验收;Invalid Rate < 15% |
| M3 专家工具 | 2~3 周 | 日志 agent(Algorithm 1)、代码 agent | FR-07/08 验收;幻觉过滤生效 |
| M4 OBSK | 1 周 | 快照键值库 + prompt 规则 + 消融开关 | FR-04 验收 |
| M5 SC/TSC | 1~2 周 | TSC 采样、文本聚合、步进 SC、**成本控制** | FR-10 验收;K=1 与 greedy 差异微小 |
| M6 数据与服务适配 | 2~4 周 | 目标服务工具集、知识库、数据集(≥100 实例四项标注)、OoD 案例、环境适配层 | FR-12/13 验收 |
| M7 评估 | 1~2 周 | 指标流水线、DeepSeek 评估器、报表 | FR-11 验收;Table I~V 输出 |
| M8 实验与调优 | 2~3 周 | RQ1~RQ5、文档打磨迭代 | 成功标准 §1.2 |
| M9 本地模型阶段(后续) | 待定 | 本地中小模型接入、隐私版部署 | 复现论文"本地弱模型+稳定化"原始设定 |
| 合计(API 阶段) | ~10~16 周 | — | — |

**关键路径**:M1 → M3(日志 agent 最复杂)→ M6(数据,可与 M1~M5 并行准备)→ M8。

---

## 8. 风险与开放问题

| # | 风险 | 等级 | 缓解 |
| --- | --- | --- | --- |
| R1 | API 模型过强,消融梯度被压缩(JsonRegen/OBSK 的收益变小) | 中 | 论文 §VI-B2 表明 ChatGPT 也会出错,机制仍有效;若梯度不显,降低任务复杂度或 M9 阶段换本地中小模型验证 |
| R2 | API 成本与速率限制(TSC K=10 × 多轮实验) | 中 | FR-10 预算估算 + 并发 + 缓存;采样数按需降档 |
| R3 | 目标服务未定,数据构建停滞 | 高 | 未确认前按示例服务推进;确认后替换三处耦合点即可 |
| R4 | 替代数据集与论文不可比 | 中 | 目标为相对趋势一致(§1.2);论文结论(RCAgent > ReAct)应在任意服务数据上成立 |
| R5 | DeepSeek 不支持 repetition/frequency penalty 参数 | 低 | 降级策略:循环检测 → 强化禁止重复指令重发(FR-09) |
| R6 | NUBIA/BLEURT/BARTScore 权重下载环境问题 | 低 | 全部离线可获取;必要时降级指标先行 |
| R7 | API 评估器(DeepSeek)与论文 gpt-4-0613 评分尺度差异 | 低 | 评估只用于相对比较(Win Rate 等),不追求绝对一致 |
| R8 | 上下文预算:16K~32K 内 Agent 历史+工具文档+head 观察分配 | 中 | OBSK head 长度与文档精简迭代(论文 §VI-B2 经验) |
| R9 | 数据脱敏(发送 API 前的敏感信息) | 中 | FR-15 脱敏配置;评估数据先行人工审查 |
| R10 | Louvain 分区窗口(200)在长日志上的计算量 | 中 | 行数 >5 万时抽样窗口或分块;可合理工程化 |
| R11 | Embedding API 模型未到位,阻塞日志分区与 EmbScore 评估 | 低 | 占位实现(OpenAI 兼容协议)先行,模型到位后配置切换;分区质量对最终指标影响边际(论文 §VI-A) |
| R12 | 自建 demo 服务与用户后续提供的真实服务重叠浪费 | 低 | demo 只验证适配方法(§2.11 五步),真实服务到位后仅重做三处耦合点;框架代码零浪费 |

**开放问题清单**:
1. **目标服务**:用户后续提供,或由本项目自建 demo 服务(候选方案见 §2.11);M1~M5 框架开发可先行,不依赖此决策;
2. **Embedding 模型**:用户后续提供 API embedding 模型;代码侧已配置化预留;
3. 人工评估参与人数与期望;
4. 是否介意发送**脱敏后**数据到 DeepSeek API(评估器与基座均为 API,这是既定方向,仅需确认脱敏边界)。

---

## 9. 验收标准汇总

1. **功能**:L1 端到端 —— 输入异常实例 ID,输出四项 RCA 结果,Pass Rate ≥ 90%,Invalid Rate < 15%。
2. **通用性**:换目标服务时,只改工具集 + 领域知识 + 知识库,框架代码零改动(§2.11)。
3. **对齐**:RCAgent 相对 ReAct 的整体胜出(Win Rate 与主要语义指标方向一致);消融趋势(experts > JsonRegen > OBSK)存在。
4. **机制**:OBSK 快照传参可用;JsonRegen 修复率 ≥ 90%;日志 agent 证据零幻觉;TSC 采样树正确;K=1 与 greedy 差异微小。
5. **可复现**:任一实验可从配置+种子完整重跑;轨迹完整可重建(FR-14);API 成本有账。
6. **工程**:工具注册式;数据源可插拔;脱敏配置生效。

---

## 附录 A:责任判定规则(论文 Fig.6,结构通用,条目按目标服务替换)

**Platform responsibility(平台责任)** —— 只能由平台维护者修复的问题,包括但不限于:
1. **IaaS 层**:硬件故障、网络连接失败、OS 系统升级等;
2. **PaaS 层**:为高优作业驱逐作业、过度售卖计算资源导致资源释放、管理服务(API server、SQL server 等)异常、运行时(VVR)及其他关联云系统组件的 bug 或不兼容;
3. **未明确问题**:需要对云系统做更多调查与诊断才能缓解的问题。

**User responsibility(用户责任)** —— 用户对平台的错误或故意滥用,以及任何可由用户自助修复的问题,包括但不限于:
1. **用户刻意操作**:通过 SDK/gRPC 请求取消作业,或通过控制台操作;
2. **配置错误**:资源不足(内存泄漏、配置错误、资源配额不足)、缺少正确 HA(重启或 checkpoint 等高可用设置);
3. **代码问题**:语法错误、可通过修改代码解决的问题(含进程或上下游服务抛出的异常);
4. **最佳实践违反**:任何有明显缓解建议可供用户自助修复的问题。

> 适配说明:两分法结构与 4+3 条分类框架保留;具体条目(IaaS/PaaS 语义、VVR、checkpoint 等)按目标服务改写。

## 附录 B:标注示例(论文 Fig.7,标注风格对齐)

- **例1**:Root cause: "High pressure or anomalies in the Elasticsearch client, resulting in connection timeouts";Solution: "If there are multiple timeouts, it is recommended to seek help from Elasticsearch's product ticket service or manual support.";Evidence: `"SocketTimeoutException"` problem in `"org.apache.flink.elasticsearch"`;Responsibility: **Platform**。
- **例2**:Root cause: "Bucket lacks lifecycle rules for version control";Solution: "Configure lifecycle rules on OSS to periodically clean up and delete unnecessary tagging and historical versions...";Evidence: `"RequestTimeTooSkewed"` + `"The difference between the request time and the current time is too large"` + exception stack `"...oshadoop.shaded.com.alibaba.oss.OSSException"`;Responsibility: **User**。

## 附录 C:论文可复现关键配置速查

- 默认 K=10;SC 采样 temperature=0.9, top_p=0.6;greedy 默认;惩罚重启阈值 4096 token、+0.5/+0.5 可迭代;
- Pass Rate 定义:15 步内成功 finalize;失败/不完整填 "Unclear";
- NormScore 基线 b="Unclear";EmbScore=(1+cos)/2;
- 日志配对窗口 j−i∈(0,200];权重 w=sim×exp(−d);证据过滤 len(e,p) 规则见 §2.4;
- 知识库检索示例与标注规则严格分离;
- 评估器 prompt(论文原文):"Judge the correctness of the prediction, 0 is completely wrong and 10 is well-matched" / "Judge the helpfulness of the prediction, 0 is completely misleading and 10 is very helpful"。
