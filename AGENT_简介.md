# RCAgent:一个会"自己排查故障"的 LLM Agent

> 基于论文 RCAgent(arXiv:2310.16340)复现,已接入真实系统 QuantumLink IM 验证。

## 一句话

**给 Agent 一句异常描述(如"im-connect 消息上抛失败"),它会自主决定查什么日志、调什么工具、何时下结论,最终输出四项 RCA 结果:根因 / 解决方案 / 证据 / 责任方。**

## 核心组成(五个部分)

```
┌──────────────────────────────────────────────────────────────┐
│ ① 决策循环(Controller Agent)                                  │
│    Thought → Action(JSON)→ Observation,循环直到 finalize      │
│    最多 15 步;任一环节失败都带错误反馈回到 LLM 重新生成         │
├──────────────────────────────────────────────────────────────┤
│ ② 稳定性三件套                                                │
│    JsonRegen: 修复 LLM 的坏 JSON(转义/YAML 重试)              │
│    错误处理:  拦截重复调用/trivial 输入/过早 finalize           │
│    OBSK:      长观察只给 head + 快照键,完整内容按需取           │
├──────────────────────────────────────────────────────────────┤
│ ③ 专家工具(LLM 当工具用)                                      │
│    日志分析专家: 长日志 Louvain 分区 + 知识库检索 + 幻觉过滤    │
│    代码分析专家: 从类名出发递归读代码,仓库即插件               │
├──────────────────────────────────────────────────────────────┤
│ ④ 信息收集工具(服务专属件)                                     │
│    IM 环境: chat_log / connect_log / error_summary / outbox   │
│    —— 语义极简参数,只收关键词与时间窗                          │
├──────────────────────────────────────────────────────────────┤
│ ⑤ 增强与观测                                                  │
│    TSC: 多轨迹采样聚合(更稳的结论)                             │
│    评估: 语义指标 + LLM 评估器 + 消融变体                      │
│    可视化: Web 页面实时展示 Loop 节点推进 + 流式输出            │
└──────────────────────────────────────────────────────────────┘
```

## 工作机制(一个真实例子)

**输入**:"im-connect 消息上抛失败,发送消息报错"(实时排查模式,检测时刻=当前)

```
Step 1  Thought: 异常在 connect 层,先查 connect 日志的 "send failed"
        Action:  connect_log(query="send failed")
        Observation: ERROR UpstreamProducer - send failed topic=client2server
                     RemotingConnectException: connect to 192.168.40.1:10911 failed
Step 2  Thought: 堆栈指向 broker 不可达,证据充分
        Action:  finalize
```

**输出**(3 步完成):
> **根因**:RocketMQ Broker 宕机/不可达,UpstreamProducer 无法上抛消息到 client2server
> **方案**:恢复 broker 并检查 namesrv 路由与磁盘;建议配置主从 HA
> **证据**:RemotingConnectException: connect to 192.168.40.1:10911 failed
> **责任**:platform

## 关键设计

| 设计 | 为什么 |
| --- | --- |
| 不预置排障流程 | LLM 靠常识 + 工具文档 + 反馈现场推理,能处理没见过的新故障 |
| 检测时刻截止 | 只能查故障发生前的数据,符合真实排障时序 |
| 自研框架(非 LangChain) | OBSK/JsonRegen/TSC 需要逐 token 级控制,通用框架掣肘 |
| 服务适配只改三处 | 工具集 + 领域知识 prompt + 知识库,框架零改动 |

## 现状

- **8/8 论文机制**全部实现,111 个单元测试通过;
- **3 个真实故障案例**(Redis ZPOPMIN 不兼容 / 客户端非法帧 / RocketMQ 宕机)全部 3~10 步精准定位;
- 运行方式:`python -m rcagent.web` → 浏览器打开 http://127.0.0.1:8080 → 填异常描述 → 开始排查。
