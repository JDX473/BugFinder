# Prompt 三件套

> 相关: [[Controller-Agent-循环]] | [[环境适配层与Demo数据]]
> 代码: `rcagent/core/prompts.py` | 模板: `config/prompts/`

## 是什么

论文 §III 定义的 controller 系统提示词三部分,由 `build_system_prompt` 组装:

```
┌──────────────────────────────────────────────┐
│ ① Framework Rules   (framework_rules.txt)    │
│    thought-action-observation 循环协议        │
│    JSON 动作格式 + OBSK 快照使用规则          │
│    finalize 出口说明                          │
├──────────────────────────────────────────────┤
│ ② Task Requirements (task_requirements.txt)  │
│    RCA 任务定义 + 四项输出要求                │
│    责任判定规则(Platform/User 两分法)         │
│    工作原则(证据优先等)                      │
├──────────────────────────────────────────────┤
│ ③ Tools Documentation (动态生成)             │
│    每个工具的 名称/参数/描述/示例             │
│    由 ToolRegistry.docs() 实时生成            │
└──────────────────────────────────────────────┘
```

## 各部分要点

### ① 框架规则(与语言模型无关的"操作系统手册")

- **循环协议**:每步输出 `Thought:` + `Function:`(单个 JSON 对象),观察由环境返回;
- **规则**:每步只调一个工具、只用文档中的工具、不重复调用无状态工具、不提前 finalize;
- **OBSK 规则**(关键):长观察以 `...N lines omitted. [snapshot: <key>]` 结尾时,
  需要完整内容就把 **snapshot key 直接作为参数**传给长内容工具,不要自己粘贴;
- 这些规则在真实运行中确实被模型遵循——DeepSeek 自主用快照键调用 log_agent。

### ② 任务要求(领域知识的注入点)

- 任务定义 + 四项输出(root_cause / solution / evidence / responsibility);
- 责任判定规则:结构沿用论文 Fig.6 的 Platform/User 两分法
  (IaaS/PaaS/未明确问题 vs 用户操作/配置/代码/最佳实践);
- 工作原则:证据逐字引用、不臆造、finalize 不省略字段;
- **服务适配的耦合点 2**:目标服务确定后替换本模板的领域段,框架代码零改动。

### ③ 工具文档(动态生成)

- 从工具注册表实时生成(注册即入文档,FR-03 的"注册式工具框架");
- 论文经验(§VI-B2):文档必须**极尽清晰**,否则模型会调用不存在的函数或错误传参
  ——这是 Pass Rate 的关键,需要随实验迭代打磨。

## 组装与注入

```python
build_system_prompt(registry, task_requirements, framework_rules) -> str
    = framework_rules + "=== TASK REQUIREMENTS ===" + task_requirements
      + "=== TOOLS DOCUMENTATION ===" + registry.docs() + finalize 说明

build_user_prompt(job_desc) -> str
    = "An anomaly has been detected on job '<id>'. Anomaly: <desc>..."
```

- 模板文件支持 `{placeholder}` 参数化(领域段预留);
- Prompt 版本随实验配置落盘(轨迹记录),保证可复现。

## 观察与反馈的注入格式

| 类型 | 格式 | 触发 |
| --- | --- | --- |
| 正常观察 | `Observation:\n<head 文本>` | 工具执行成功 |
| 错误反馈 | `System: Error: ...` | 解析失败/工具不存在/参数错误/错误检测命中 |

错误反馈与观察走同一条消息通道——模型"看到"错误后自行调整策略,
这是错误处理不终止循环的设计基础(见 [[错误处理与解码策略]])。

## 为什么这样设计

1. **三件套分离**让"通用协议"(①)、"领域知识"(②)、"环境能力"(③)各自独立演进:
   换服务只动 ②,加工具只动 ③,框架规则保持稳定;
2. **无 few-shot**(论文):示例让位给上下文预算;greedy 轨迹历史充当示范;
3. **文档动态生成**保证工具文档与注册的工具永远一致,不会出现"文档与实现漂移"。
