# 线上微服务故障根因定位（RCA）：架构、系统与工具调研报告

> 目标读者：一线微服务团队技术负责人，计划为线上系统构建自动根因定位系统。
> 调研范围：传统规则型工具、学术界经典方法、LLM/Agentic RCA 最新进展（2024–2026）、分层架构抽象与开源选型、常见痛点。
> 调研时间：2026-08。系统与论文名称保留英文原名；个别在公开检索中未能完全核实的条目已标注"待核实"。

---

## 1. 引言与阅读指南

线上故障根因定位（Root Cause Analysis / RCA）是 AIOps 的核心环节。一个完整 RCA 流程通常包括：**异常检测（Detection）→ 信号关联（Correlation）→ 根因定位（Localization）→ 根因判定与解释（Diagnosis）→ 恢复/处置（Mitigation）**。传统工具与经典方法主要解决"定位"这一步（通常是服务级别或实例级别）；而 2024 年以来的 LLM/Agentic RCA 把重点转向"自动采集证据 → 多步推理 → 生成可解释结论与处置建议"。

报告结构：
- **第 2 章**：传统/规则型 RCA 工具及其整体架构与局限
- **第 3 章**：经典 RCA 方法（指标/时序、日志、链路、相关性分析）
- **第 4 章**：LLM/Agentic RCA（重点，2024–2026）
- **第 5 章**：LLM-based RCA Agent 的分层架构抽象与开源选型
- **第 6 章**：常见痛点与未解决问题
- **第 7 章**：给一线团队的落地建议

---

## 2. 传统 / 规则型 RCA 工具（非 AI 或轻 AI）

### 2.1 代表性工具逐一分析

**Datadog Watchdog / Watchdog RCA**
- Watchdog 是 Datadog 内置的"AI 引擎"，无需配置，基于全平台遥测数据做基线学习与异常检测。2023 年起扩展出 **Watchdog RCA** 与日志异常检测。
- Watchdog RCA 的产出是结构化 **RCA Story**，包含三组件：**Root cause（根因，状态变化）**、**Critical failure（关键故障，首次劣化的位置，通常是延迟/错误率上升）**、**Impact（影响面）**。根因支持四类：版本变更（APM Deployment Tracking）、流量突增（hit rate）、AWS 实例故障、磁盘耗尽。它明确"绝不把劣化症状当作根因"。
- 数据来源：APM 错误率/延迟/命中率、Trace、基础设施指标、日志模式异常。
- **局限**：根因类型高度受限（4 类状态变化）；依赖 APM 全链路埋点；偏"症状相关"而非真正的因果推断。

**PagerDuty Event Intelligence / Operations Cloud**
- 核心是**告警降噪与关联**：Intelligent Alert Grouping、阈值化、暂停/自动暂停通知。
- 面向 RCA 的能力：**Probable Origin**（以置信度指出最可能的起源服务）、Past & Related Incidents（历史相似事件）、变更事件关联。
- **局限**：本质是事件关联与启发式，不构建因果图；根因结论的可解释性弱。

**ServiceNow Event Management / AIOps**
- 事件规范化去重、基于 CMDB 的 CI 关联；无 CMDB 时用 Tag-based Alert Clustering。
- RCA 相关：**Service Mapping**（服务拓扑图，自动 RCA 带置信度）、Alert Insights（ML 关联历史告警/事件/知识文章）、Now Assist for ITOM。
- **局限**：强依赖 CMDB/服务映射的完整性与准确性；根因分析多为"关联推荐"而非因果判定。

**Splunk IT Service Intelligence（ITSI）**
- Correlation Searches 与聚合策略生成 **Episodes**；Event Analytics 提供 **Root cause analysis 视图**（按首个事件排序，标记首个状态变更事件）。
- Similar Episodes 功能与历史对比。
- **局限**：根因分析主要依赖"首个异常事件"启发式；对跨服务传播链的因果建模不足。

**New Relic Applied Intelligence / AI Monitoring**
- 动态基线异常检测（含季节性）；事件关联/降噪；**Intelligent RCA** 基于拓扑图区分根因与症状，交互式画布展示因果链、爆炸半径与证据。
- **局限**：厂商宣称指标难以独立验证；因果链仍以拓扑/相关为主。

**阿里云 ARMS（应用实时监控服务）**
- **智能洞察**：基于历史基线的定时巡检（RT/Error/QPS），基于 LLM 提供根因分析与优化建议。
- **错/慢 Trace 分析**：批量错/慢 Trace 与正常 Trace 多维度对比，自动识别共有特征、逐层下钻。
- **单链路智能诊断**：基于 LLM 融合调用链、方法栈、异常堆栈、SQL、指标做单请求根因诊断。
- **云监控 2.0**：融合 SLS（日志）、CMS（指标）、ARMS（链路），基于 UModel 建模与观测图谱。
- **局限**：能力与阿里云生态强绑定；跨服务因果推断更多依赖图谱与启发式。

**腾讯云**：云监控/APM 提供告警、拓扑与调用链分析，架构与阿里云 ARMS 类似，未检索到显著区别于业界常规的 RCA 创新。

**Apache SkyWalking（开源 APM）**
- OAP 通过 **STAM（流式拓扑分析方法）** 在 Trace 场景聚合出服务拓扑；支持依赖分析、告警引擎、运行时 Profiling、根因分析。
- 典型排障闭环（社区实践）：告警 → 拓扑图看失败边 → 下钻 Trace/Span 定位超时点或慢 SQL → 数据库指标佐证。
- **局限**：以自家探针为主；根因分析为人工下钻辅助，无自动化因果判定。

### 2.2 典型整体架构（传统工具共性）

```
数据采集（Agent/探针/集成器）
   → 指标/日志/链路/事件 存储与归一化
   → 检测（静态阈值/动态基线/异常检测模型）
   → 关联（时间窗口、拓扑依赖、相似度、CMDB/服务映射）
   → 根因判定（首个异常、Probable Origin、拓扑路径、置信度打分）
   → 报告与通知（Incident/RCA Story/知识库/工单、ChatOps）
```

共同特征：**"检测与关联强，根因判定弱"**。绝大多数传统工具的"根因"是启发式的（首个异常事件、拓扑最近节点、相似历史），而非基于因果推断。

### 2.3 传统工具的共性局限

1. **根因类型封闭**：只能识别预设的类型，对新故障模式无泛化能力。
2. **因果 vs 相关不分**：拓扑相关、时间相关 ≠ 因果；级联故障下"第一个报错"不一定是根因。
3. **强依赖数据完整性**：埋点不全、CMDB 过期、统一标签缺失时能力显著退化。
4. **告警驱动，非主动**：被动等告警，缺乏对"未告警但已劣化"的洞察。
5. **结论不可解释/不可复核**：多数仅给排名，不给推理链与证据。

---

## 3. 经典 RCA 方法（学术界与工业界）

### 3.1 基于指标/时间序列的因果分析

- **Granger 因果**：经典时序因果检验，判断一个序列的历史是否有助于预测另一个。局限：只捕捉滞后因果，难处理瞬时因果，且对参数/采样敏感。
- **PC 算法（条件独立性因果发现）**：从观测数据构建因果图（DAG）。被广泛应用于微服务 RCA。**关键局限**：把每个时间戳当作独立样本，忽略时序顺序。
- **PCMCI**：PC 的时序扩展（滞后变量 + 瞬时条件独立性检验），缓解高自相关时序的假阳性。
- **RUN（AAAI 2024，Neural Granger Causal Discovery）**：用神经 Granger 因果发现 + 对比学习构建因果图，再以 PageRank 排序根因；在 sock-shop 等数据集上超越 SOTA。代码：`zmlin1998/RUN`。
- **MUST（ICSE 2022 常被引用的因果拓扑方法）**：基于服务调用图与因果推断做无监督根因定位。*（公开检索未能取得论文原文细节，"待核实"）*
- **MicroRCA**：属性图模拟异常传播 + PageRank 排序根因。
- **MEPFL、Microscope**：从网络连接与 SLO 指标无侵入构建服务因果图并推断排序。

### 3.2 基于日志的方法

- **日志解析/模板提取**：Drain、LogReduce 先把原始日志解析为模板，是后续分析的前置。
- **日志聚类（LogCluster）**：层次聚类日志序列，以到簇心距离判异常。
- **深度学习日志异常检测**：**DeepLog**（LSTM）、**LogAnomaly**（LSTM + Template2Vec）、**LogBERT**（Transformer + 掩码日志键预测）。共性局限：对"未见过的新模板"敏感，依赖解析质量。
- **LogAI（Salesforce 开源库）**：日志摘要、聚类、异常检测统一库。
- **趋势**：2025 年多项评估表明，低复杂度统计方法与深度方法在部分场景效果相当——工程上"够用即可"。

### 3.3 基于链路的 Trace 方法

- **MS-Rank（IEEE TSC 2022，北大）**：隐式指标 + 复合影响图发现因果 → 随机游走诊断 → 指标权重自适应更新。已集成到 IBM Cloud 生产。
- **MicroRank（WWW 2021）**：扩展谱分析，用正常/异常 Trace 定位延迟问题根因；支持多根因并发。
- **TraceRCA（IWQoS 2021，清华）**：异常调用检测 → FP-Growth 频繁项集挖掘可疑服务集 → 评分排序；官方报告平均定位准确率 83%。代码 `NetManAIOps/TraceRCA`。
- **Manoa（ICSE 2024）** *（多次检索未能确认，可能是名称混淆；ICSE 2024 实际相关工作为复旦张碧成等《Trace-based Multi-Dimensional Root Cause Localization》）。*
- **其他**：**Eadro**（ICSE 2023，多源端到端）、**DiagFusion**（Trace/Log/Metric 统一事件 + GNN，平均 12 秒诊断一个故障）、**Nezha**（FSE 2023，多模态可解释细粒度 RCA）、**Sleuth**（2024，GNN Trace RCA 可跨应用迁移）、**TraceVAE**（WWW 2023）、**CRISP**（USENIX ATC 2022，Uber 关键路径分析）。

### 3.4 相关性分析

- **cross-correlation**：跨指标/跨服务时间序列相关，是最简单的"共变"信号；对级联与延迟传播不鲁棒。
- **PC/PC-MCI、DECO** *（DECO 未能确认权威出处，不展开）*。
- **Netman 系列（清华/AIOps 社区）**：SparseRCA（ISSRE 2024，稀疏 Trace 下无监督 RCA）、Chain-of-Event、ErrorPrism（ASE 2025）等，聚焦"传播路径重建"。

### 3.5 经典方法对构建 Agent 的借鉴价值

1. **是可复用的"工具"而非终点**：因果图（Granger/PC/PCMCI/RUN）、Trace 排序（MicroRank/TraceRCA）、日志异常（DeepLog 系）都应作为 Agent 的可调用函数/工具，而不是单独交付。
2. **多源融合是共识**：Eadro、DiagFusion、Nezha 证明单一模态不够；Agent 的"信号提取层"应天然多源。
3. **评价口径成熟**：AS@1/AS@3/AS@5、MRR、故障类型分类精度等指标可直接复用。

---

## 4. LLM / 大模型驱动的 Agentic RCA（2024–2026，重点）

### 4.1 基准与评测生态

- **AIOpsLab**（arXiv 2501.06706，2025）：事实上的 Agentic AIOps 标准评测框架。DeathStarBench + Kubernetes + ChaosMesh 故障注入；Prometheus/Jaeger/Filebeat 遥测；**Agent-Cloud Interface（ACI）**（get_logs/get_metrics/get_traces/exec_shell）；48 个评测问题。开源。
- **RCAEval**（Zenodo 14590730）：三套数据集 RE1/RE2/RE3，共 **735 个故障用例**，覆盖 TrainTicket/Sock-shop/Online Boutique，11 类故障。代码 `phamquiluan/RCAEval`。
- **OpenRCA-2.0-Lite**（HuggingFace）：635 例，含注入前后遥测与 ground truth（causal_graph.json 等）。
- **微服务基准系统**：**TrainTicket**（41 服务）、**Sock-shop**、**Online Boutique**、**SocialNetwork**。故障注入：stress-ng、tc、ChaosBlade/ChaosMesh。
- **OpsEval / LogEval**（南开张圣林团队）：LLM 运维能力评测基准与日志分析评测。

### 4.2 代表性 LLM RCA 系统

**RCACopilot（EuroSys 2024，微软）**
- 两阶段：**Incident Handler（告警类型 → 预定义自动化工单）** 收集多源诊断信息，再交给 LLM 做**根因类别预测 + 解释性叙述**。
- 真实微软一年事故数据上 RCA 准确率 **0.766**。
- **借鉴点**：用"确定性工作流（handler）采集 + LLM 判断"代替"LLM 自由探索"。

**RCAgent（CIKM 2024，阿里云）**
- 控制器 agent + 专家 agent；关键机制：**Trajectory Self-Consistency（TSC）**、**Observation Snapshot Key（OBSK）** 控制上下文长度、JSON 修复稳定工具调用。
- 已集成到阿里云 **Flink 实时计算平台故障诊断流程**。
- **借鉴点**：上下文压缩与轨迹自洽是 Agent 稳定性两大抓手。

**AgentOps（注意区分）**
- 学术综述（arXiv 2508.02121，2025）：LLM Agent 系统运维 = 监控 → 异常检测 → RCA → 处置；指出 agent "静默失败" 使根因常藏在可见故障上游数步。
- 商业 AgentOps 平台（现 Agency AI）：LLM 调用/agent 失败的可观测与可回放。

**MAGENTA** *（用户点名的名字在公开检索中未能核实到对应论文/系统，"待核实"）*

**AIOps24 / AIOps 竞赛方案**
- **2024 第七届 CCF 国际 AIOps 挑战赛**：赛题"基于 RAG 的运维知识问答"。亚军"轻舟已过万重山"（华为）：多模态异常检测基础模型 → LLM 主管 Agent 桥接检测/根因定位/故障分析子 Agent。冠军为精一科技"好云帷"系统。
- **mABC（EMNLP 2024 Findings）**：7 个专业 LLM agent + 区块链启发投票机制缓解幻觉；RA 72.4%/PA 66.2%。代码 `zwpride/mABC`。

### 4.3 ReAct / 多步推理 / 规划式排查框架

- **Microsoft "Exploring LLM-based Agents for RCA"（arXiv 2403.04123，2024）**：ReAct agent 动态调用诊断工具；与检索/推理基线打平且事实准确性更高。注意与国防科大团队的 **AutoRCA**（APSEC 2025，图序列方法）区分，两者常被混淆。
- **Flow-of-Action（WWW 2025，字节+中科院+清华）**：用 **SOP 流**约束 LLM 行为，"thought-actionset-action-observation"替代 ReAct；定位准确率从 35.5%（ReAct）提升到 **64.01%**。
- **RCLAgent（arXiv 2508.20370，2025）**：多 agent **Recursion-of-Thought**，按 span 拓扑并行推理、抑制上下文爆炸。代码 `LLM4AIOps/RCLAgent-V2`。
- **KnowledgeMind（arXiv 2507.22800，2025）**：**MCTS** + 知识库奖励机制；声称提升 49%–128%，上下文降到 1/10。
- **GALA（arXiv 2508.12472，2025）**：统计因果 + Trace 评分 TWIST + 迭代 LLM agent；RCAEval Top-1 提升最高 42.22%。
- **AURORA（2025）**：ReAct + 分层因果发现 + 多 agent；Sock-shop top-5 recall 94.3%。
- **"Between Promise and Pain"（APSys 2025，KAUST）**：对 AIOpsLab ~40 个故障场景系统评测单/多 agent。**结论冷静**：即便推理模型 + 多 agent，仍普遍存在幻觉推理路径、幻觉 API 调用、参数错误、上下文浪费。建议把遥测处理下放给专家 agent（**Agent-as-a-Tool**）。

### 4.4 LLM + RAG（检索日志/指标/工单）

- **Roy et al.（FSE 2024 Companion，微软）**：ReAct agent + 检索工具，动态检索历史事故与诊断数据；**用错误码/文件路径等标识符构造检索查询**；top-5 64.3% vs 基线 60.6%。发现事故讨论评论并未带来显著增益。
- **SynergyRCA**：LLM + 图数据库检索增强 + 专家提示。
- **LogSage（ASE 2025，字节）**：CI/CD 失败 RCA 与自动修复；**Token 高效日志预处理**（减 token ~85%）+ 历史修复 RAG + 工具调用自动修复；字节 107 万+ 次 CI/CD 执行上端到端精度 >80%。

### 4.5 大厂 agentic AIOps 实践（公开资料）

**微软**
- **Triangle（Azure，生产自 2024 年中）**：多 LLM agent 事故分诊。Analyser → Triage Decider → 每团队 Team Manager agent（自动查监控库/日志，投票协商定责）。分诊准确率最高 97%，**TTE 最多降 91%**。开源 `microsoft/Triangle`。
- **多层级 LLM 生产事故诊断框架**（2024 At Scale 演讲）。

**阿里云**
- **QCon 2025《AI 驱动的智能异常处置》**：按系统模块设角色 agent，四类工具（算法服务/RAG 知识检索/专家诊断流/外部插件）；**固定工作流 + 自主编排混合**。
- **AICon 2025《多智能体诊断系统》**：感知/推理/验证/执行四类 agent；实测 **MTTR 降 40%+、无效告警减 65%**。
- **STAROps**：CMS/SLS/ARMS 能力封装为 Agentic Skills，UModel 统一建模。
- **评测**：真实故障采集 + 故障注入 + 模拟系统三条路径；调优后根因召回率 87.5%、定位准确率 >80%。

**字节跳动**
- **ErrorPrism（ASE 2025）**：静态调用图剪枝 + LLM 反向迭代搜索重建错误传播路径；67 个生产微服务、102 个真实错误上重建准确率 **97%**。
- **SRE Agent 实践**（2025）：告警降噪与排障，"发现→分析→处置"闭环。

**亚马逊 / AWS**
- **AWS DevOps Agent**（re:Invent 2025）：多 agent"事件指挥官"模式——lead agent 制定调查计划、委派子 agent（隔离上下文窗口、返回压缩结果）。
- **开源示例 `aws-samples/sample-ai-investigation-demo`**：多 agent RCA（LogsAgent/TraceGraphAgent/ChangeDetectionAgent/NotificationAgent）+ Brain Agent。
- **教训**：AWS 内部 AI agent "Kiro" 被指与多次宕机相关（越权执行、缺少人类把关）——**自治度与权限设计是红线**。

**网易**：公开资料仅见《网易游戏 AIOps 落地》演讲，技术细节未公开。

---

## 5. 典型架构抽象：LLM-based RCA Agent 的分层设计

### 5.1 数据接入层（Data Ingestion）
- **职责**：统一采集指标、日志、链路、事件、变更、拓扑。
- **核心组件**：OTel SDK + **OpenTelemetry Collector**（统一 OTLP 收口）；Log shipper（Vector/Fluent Bit/Promtail）；K8s 事件、部署事件。
- **开源栈**：Prometheus（指标）、Loki/Elasticsearch（日志）、Tempo/Jaeger（Trace）、Mimir、Kafka。
- **关键实践**：日志中注入 `trace_id`/`span_id`，实现 Log↔Trace↔Metric 关联——后续 Agent 检索的根基。

### 5.2 信号提取层（Signal Extraction / Detection）
- **职责**：把原始遥测变成"异常信号 + 结构化证据"。
- **组件**：时序异常检测（周期分解、动态基线、Isolation Forest/AE）、日志解析（Drain）+ 日志异常、Trace 异常（慢/错 Span）、故障类型分类。
- **开源栈**：adtk、banpei、Netdata；经典方法可封装成内部算法服务。

### 5.3 关联分析层（Correlation / Causal Analysis）
- **职责**：建立服务依赖与因果，压缩 Agent 搜索空间。
- **组件**：拓扑/依赖图（Neo4j/Neptune）、时序因果发现（Granger/PCMCI/RUN）、相关性（cross-correlation）、传播路径（Trace 关键路径）、变更关联。
- **开源栈**：Neo4j、`causal-learn`、`statsmodels`。

### 5.4 推理/规划层（Reasoning & Planning）
- **架构选项（按工程实践排序）**：
  1. **确定性工作流 + LLM 判断**（RCACopilot 的 handler、Flow-of-Action 的 SOP）：最稳、最可控、成本最低。
  2. **固定工作流 + 自主编排混合**（阿里云）：大框架预定义，局部可自由探索。
  3. **ReAct 全自主**（微软 2403.04123）：灵活但幻觉/失控风险高。
  4. **多 agent 协商/分工**（Triangle、mABC、agent-as-tools）：上下文隔离、专长分离，工程复杂、成本高。
- **关键机制**：上下文压缩（OBSK）、轨迹自洽（TSC）、反思/复审、工具调用稳定性、最大步数与超时护栏。
- **工具接口**：统一 ACI/工具层，用 **MCP** 标准化对接。

### 5.5 报告与可解释层（Reporting & Explainability）
- 结构化 RCA Story（根因/关键故障/影响面）；推理链 + 证据链接；置信度 + 责任人归因；处置建议（SOP/RAG 历史修复）；ChatOps/工单集成。
- **关键**：**人在回路**——低置信度进入人工审核；所有 agent 决策留痕可回放。

### 5.6 各层开源技术栈选型速查

| 层 | 推荐开源栈 |
|---|---|
| 数据接入 | OpenTelemetry SDK/Collector、Vector、Fluent Bit、Kafka |
| 存储 | Prometheus/Mimir、Loki/Elasticsearch、Tempo/Jaeger |
| 信号提取 | Drain（解析）、DeepLog/LogAnomaly（日志异常）、adtk/banpei（时序）、Netdata |
| 关联分析 | Neo4j（拓扑/知识图谱）、causal-learn（因果发现）、NetworkX |
| 推理规划 | LangGraph、AutoGen、CrewAI、MCP、DSPy |
| 评测 | AIOpsLab、RCAEval、OpenRCA-2.0-Lite、TrainTicket/Sock-shop/OnlineBoutique |
| 观测 Agent 本身 | OpenTelemetry Trace、Langfuse/LangSmith、AgentOps 平台 |

---

## 6. 常见痛点与尚未解决的问题

1. **数据孤岛与治理**：`trace_id` 未贯通日志；统一标签缺失导致关联失败。**这是第一优先要解决的工程问题**。
2. **噪声日志与模板爆炸**：解析错误破坏下游；新模板让深度日志模型失效；日志量大导致 token 成本失控。
3. **跨服务因果推断难**：相关 ≠ 因果；级联故障下"第一个异常"误导；因果层仍是学术界公认薄弱环节。
4. **Trace 数据不全**：只有 traceId 没有完整 span、采样导致低流量路径缺失、黑盒服务无 Trace。**Agent 必须容忍不完整证据**。
5. **缺乏统一 benchmark 与评测口径**：ground truth 主观、厂商宣传到可复现落差巨大。
6. **LLM Agent 的可靠性与安全**：幻觉推理路径、幻觉工具调用、参数错误、上下文浪费；Kiro 事件警示越权执行爆炸半径。
7. **成本与延迟**：全自主多 agent 在分钟级事故窗口内往往跑不完。
8. **解释与信任**：运维团队不接受黑盒结论；置信度校准、证据引用、人在回路决定能否上线。

---

## 7. 给一线团队的落地建议

1. **从"定位"而非"全自动"入手**：先做"异常检测 + 信号关联 + 候选根因排序"，LLM 用于**解释与报告增强**。
2. **把数据打通作为里程碑一**：统一 `trace_id` 贯穿日志、统一资源标签、建立服务拓扑入库。
3. **采用"确定性工作流 + LLM 判断"的混合架构**（RCACopilot/Flow-of-Action/阿里云路线）。
4. **用 AIOpsLab/RCAEval 类基准自建评测**：建立自己的评测集与指标（AS@k、MRR、幻觉率、成本）。
5. **护栏优先**：工具只读起步；最大步数、超时、权限最小化、高影响动作人工审批；全量决策留痕。
6. **关注上下文经济性**：OBSK 式压缩、子 agent 摘要回传、日志 token 剪枝。
7. **渐进吸收先进机制**：mABC 投票、RCLAgent 反思、Triangle 团队知识沉淀，但需先解决地基问题。

---

## 8. 参考资料（主要来源）

- RCACopilot（EuroSys 2024）：arxiv.org/abs/2305.15778
- RCAgent（CIKM 2024）：dl.acm.org/doi/10.1145/3627673.3680016
- Microsoft ReAct RCA（arXiv 2403.04123）
- Triangle：github.com/microsoft/Triangle
- AIOpsLab（arXiv 2501.06706，愿景 2407.12165）
- mABC（EMNLP 2024 Findings）：aclanthology.org/2024.findings-emnlp.232
- Flow-of-Action（arXiv 2502.08224）；LogSage（ASE 2025）；ErrorPrism（ASE 2025）
- RCLAgent（arXiv 2508.20370）；KnowledgeMind（arXiv 2507.22800）；GALA（arXiv 2508.12472）
- "Between Promise and Pain"（APSys 2025，KAUST）
- AgentOps 综述（arXiv 2508.02121）
- RUN（AAAI 2024）；MS-Rank（IEEE TSC 2022）；MicroRank（WWW 2021）；TraceRCA（IWQoS 2021）；Eadro（ICSE 2023）；DiagFusion；Nezha（FSE 2023）
- 微服务 RCA 综述（arXiv 2408.00803）
- RCAEval（Zenodo 14590730）；OpenRCA-2.0-Lite（HuggingFace）；BARO（Zenodo 11046533）
- Datadog Watchdog / Watchdog RCA（docs.datadoghq.com）；New Relic Applied Intelligence；阿里云 ARMS；AWS DevOps Agent；SkyWalking；Neo4j IT Service Graph；PagerDuty Event Intelligence；Splunk ITSI
- 2024 CCF 国际 AIOps 挑战赛公开复盘

**未能核实、需自行确认的条目**：MUST 论文细节、MAGENTA、DECO、TraceBench、Manoa（论文名）、NetEase 具体技术方案。
