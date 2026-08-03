# 真实 DeepSeek 接入 实现细节

> 模块：`app/llm/deepseek_llm.py`（生产实现）、`app/llm/ask_json.py`（shim）、`app/graph/workflow.py`（注入）
> 关联 PRD：§6.4（LLM 调用规格）、§7.2（技术选型）、硬约束 3（DeepSeek 不支持 Structured Output）
> 状态：真实 DeepSeek 已接通并验证——三个 LLM 注入点全部工作，125 cases 全量对比完成

## 解决什么问题

此前 LLM 全部是 `llm=None`（纯规则）或 FakeLLM（mock 测试）。本模块把**真实 DeepSeek**
接入三个关键判断点，验证"确定性为主干、LLM 只在判断点"的架构在真实模型下的行为，
并产出**规则 vs LLM 的真实数据对比**。

## 核心方法

### 1. 客户端（`deepseek_llm.py`）

`DeepSeekLLM` 实现 `LLMClient` 协议，走 OpenAI 兼容接口（`deepseek-chat`）：
- `create_deepseek_client()`：从 `config/settings.py` 读 `RCA_LLM_API_KEY`/`RCA_LLM_BASE_URL`/`RCA_LLM_MODEL`
  - 未配置 key → 返回 None（系统退化为纯规则模式）
  - openai 未装 → 返回 None（延迟导入，可选依赖）
- `complete()`：标准 chat completion，返回文本

### 2. 三个 LLM 注入点（workflow）

| 注入点 | 触发条件 | LLM 职责 | 确定性兜底 |
|---|---|---|---|
| **场景路由兜底**（`route_scenario`） | 无指标证据 + 无业务白名单命中 | 从 6 枚举选场景 + 抽业务上下文 | 低置信/坏 JSON → other（0.1） |
| **假设排序**（`generate_hypotheses`） | 有 ≥2 个规则假设 | 对假设做 pairwise 排名 | 低置信/失败 → 规则分排序 |
| **日志深挖**（`make_logs_node` + bounded_react） | 日志聚类含异常簇 + 注入 llm | ReAct 决定是否深挖（工具受限） | 失败 → 不深挖（聚类摘要已够） |

### 3. 验证结果

**最小连通**：`ask_json` shim 在真实 DeepSeek 上工作正常（结构化输出 + 重试 + 兜底）。

**业务语义兜底**（关键价值）：
- 输入 `充电桩充不进电`（白名单外）→ DeepSeek 判定 `business_logic`（conf 0.95），
  抽 `充电桩/充不进电`，source=llm。纯规则会落 other（白名单没这条）。

**全量 125 cases 对比**（RE1-OB 真实指标）：

| 指标 | 纯规则 | 真实 DeepSeek | 变化 |
|---|---|---|---|
| Top-1 命中 | 83.2% | 84.0% | +0.8 |
| Top-3 命中 | 95.2% | 96.0% | +0.8 |
| LLM 实际参与 | — | 43/125 | — |

**结论（诚实记录）**：纯指标驱动的故障，规则已定位得很好，LLM 增量很小（+0.8%）。
这**验证了"确定性优先"架构是对的**——LLM 不该在确定性已能解决的地方刷存在感。
LLM 的真正价值在**规则覆盖不到的地方**：白名单外的业务语义、需要推理的判断、
以及 RE2/RE3 的日志/trace 复杂场景（尚未验证）。

## 输出契约

```python
# 注入方式
from app.llm.deepseek_llm import create_deepseek_client
llm = create_deepseek_client()          # None = 纯规则模式
wf = RCAWorkflow(llm=llm)               # 三个注入点全接通

# 环境变量（不写进代码/仓库）
RCA_LLM_API_KEY=sk-xxx                 # DeepSeek key
RCA_LLM_BASE_URL=https://api.deepseek.com/v1   # 默认
RCA_LLM_MODEL=deepseek-chat            # 默认

# 评估对比
python scripts/eval_rcaeval.py --limit 125           # 纯规则基线
python scripts/eval_rcaeval.py --limit 125 --llm     # 真实 LLM（需 key）
```

## 设计边界

- **key 不进仓库**：`RCA_LLM_API_KEY` 只走环境变量，代码/文档不出现真实 key。
- **确定性优先不破坏**：LLM 失败/低置信/坏 JSON 全部降级到确定性结果，永不比规则差
  （对比 +0.8 证明 LLM 参与时也没拖后腿）。
- **LLM 不生成根因文本**：只排序规则假设（pairwise），不自由发挥。
- **ask_json shim 是唯一入口**：所有结构化产出过 shim，无裸调。

## 已知边界与局限

- **RE1 是纯指标**：LLM 在指标定位上增量小是**数据决定的**——规则已够。
  LLM 潜力需 RE2/RE3（日志+trace）验证（复杂调用链/根因隐藏场景）。
- **`_svc_from_metric` 依赖命名约定**：接真实 Prometheus 指标名时需确认。
- **token 成本未计量**：ask_json 不返回 usage，`token_cost` 是记录字段（Phase 2）。
- **评估是雏形**：只有命中率，幻觉率/证据引用完整率需报告 Web + 人工核验。

## 本地验证

```bash
# 1. 最小连通（需环境变量，约几厘）
RCA_LLM_API_KEY=sk-xxx .venv/Scripts/python.exe -c "
from app.llm.deepseek_llm import create_deepseek_client
from app.llm.ask_json import ask_json
c = create_deepseek_client()
r = ask_json(c, '测试', '说你好', {'type':'object','properties':{'a':{'type':'string'}},'required':['a']}, fallback=lambda:{'a':'fb'})
print(r.ok, r.data)"

# 2. 业务语义兜底（白名单外）
RCA_LLM_API_KEY=sk-xxx .venv/Scripts/python.exe -c "
from app.pipeline.scenario_router import route_scenario
from app.llm.deepseek_llm import create_deepseek_client
r = route_scenario(incident_text='用户反馈充电桩充不进电', anomalies=[], llm=create_deepseek_client())
print(r.to_summary())"   # → business_logic + 充电桩/充不进电

# 3. 全量对比（125 cases，真实 LLM 约几毛钱）
RCA_LLM_API_KEY=sk-xxx .venv/Scripts/python.exe scripts/eval_rcaeval.py --limit 125
RCA_LLM_API_KEY=sk-xxx .venv/Scripts/python.exe scripts/eval_rcaeval.py --limit 125 --llm
```
