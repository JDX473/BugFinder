# TSC 与文本聚合

> 相关: [[Controller-Agent-循环]] | [[错误处理与解码策略]]
> 代码: `rcagent/sc/tsc.py` + `aggregate.py`
> 论文: §III-D, 图5

## 是什么

**轨迹级 Self-Consistency(自一致性)**:让 agent 以多种"收尾思路"给出多个
候选答案,再聚合成一个——多路径投票提升结论稳健性。
是论文在"把 SC 用到自由文本 agent 轨迹"上的创新点。

## 为什么不能直接照搬经典 SC

经典 SC(CoT 多路径投票)用在 agent 上有两个致命问题(论文实测):
1. **太贵**:从第一步采样 K 条完整轨迹,信息收集、专家分析全部重跑 K 遍
   (API 模式下成本直接翻倍);
2. **会崩**:从第一步随机采样缺少历史示范,模型乱调工具——
   纯采样解码 Pass Rate 从 99% 崩到 70%,Invalid Rate 升到 44.8%。

## TSC 的核心洞察

**信息收集阶段是确定性的工作,不需要多样性;只有"下结论"需要多种视角。**

```
主轨迹(greedy):  查日志 → 分析 → 综合 → finalize
                                      ↑ 只有这里需要采样
子轨迹(采样×K):            综合' → finalize \
                           综合'' → finalize  } 自由 0~N 步
                           综合''' → finalize /
聚合: K+1 个候选 → LLM 聚合 / embedding 投票 → 最终答案
```

- 采样点 = **主轨迹 finalize 的倒数第二步**;
- 前期步骤(1..t-2)共享主轨迹的——不重跑,省成本;
- greedy 留下的稳定动作历史充当 few-shot 示范,抑制采样期乱来;
- 不限制子轨迹后续步数(0 或多步),直到自身 finalize 或全局上限。

## 实现(TSCRunner)

```python
run(job, samples=10, method="tsc", aggregate="llm") -> TSCResult:
    1. main = agent.run(job, greedy)          # 主轨迹
       if not main.passed: return 失败        # 主轨迹失败则不采样
    2. keep = len(main.records) - 2           # 保留倒数第二步之前的历史
       base = agent.replay_messages(job, main, keep)   # 精确重建消息
    3. for k in range(samples):
           sampler = agent.fork_sampler()     # 独立错误检测器,继承调查状态
           sub = 新 Trajectory
           result = sampler._loop(base, job, sub,
                                  decode_mode="sampling", max_steps=15)
    4. candidates = [main.result] + [sub.result ...]   # 失败者填 None
    5. aggregate: llm | embedding
```

### replay_messages — 消息精确重建

主轨迹的 `StepRecord` 保存了每步原始输出(`raw_action`)与注入反馈
(`observation_head`),重建的消息与运行时逐字一致——这是重放正确性的根基。

### fork_sampler — 子轨迹工厂

- 共享 `llm / SnapshotStore / ToolRegistry`(快照键在子轨迹里仍可解析);
- **独立 ErrorDetector**,但 `inherit_detector=True` 继承主轨迹的
  "已调查工具"状态——否则子轨迹直接 finalize 会被"过早 finalize"误拦截
  (真实运行中发现并修复的 bug);
- 子轨迹的变体与主轨迹一致。

## 文本聚合(aggregate.py)

### Embedding 投票(论文 §III-D1)

```python
argmax_i similarity(a_i, 1/K Σ_j a_j)
```
选与"候选均值向量"最接近的文本胜出(直接推广无权重多数投票);
- 文本字段(root_cause/solution/evidence)各自投票;
- responsibility 用多数投票;
- 失败轨迹填 "Unclear" 参与(论文 §IV-C 的 baseline 填充)。

### LLM 聚合(论文实测更优)

```
You are aggregating N candidate root cause analyses...
Candidate 1: root_cause: ... solution: ... evidence: ... responsibility: ...
...
Output JSON: {"root_cause": ..., "solution": ..., "evidence": ..., "responsibility": ...}
```
- 只聚合成功候选(失败填 Unclear 会干扰综合);
- 输出经 JsonRegen;
- 聚合失败退化为主轨迹结果。

### 步进 SC 对照(论文 §IV-A)

`method="sc"`:只采样"思考 + finalize"(不允许额外动作步骤);
生成结果非 finalize 或字段不全 → 该样本丢弃。
对齐论文 "only accepts samples that finalize synchronously with the greedy
trajectory"。

## 真实运行验证(DeepSeek, demo_task_evicted, K=3)

```
main trajectory: PASSED, 8 步
samples: pass_rate=100%  steps=[2, 2, 1]     # 收尾很短(共享调查阶段)
result: 聚合后根因整合了驱逐通知 + 心跳超时 + 级联失败 + 重启循环
         + error rate 97% 多线索,解决方案 6 条
cost: $0.41(主轨迹 ~$0.3 + 3 条短收尾)
```

与论文观察一致: **LLM 聚合随候选池增大给出更全面的结果**。

## CLI

```bash
python -m rcagent --job demo_x --sc tsc --samples 10     # TSC
python -m rcagent --job demo_x --sc sc --samples 10      # 步进 SC 对照
python -m rcagent --job demo_x --sc none                 # 关闭(默认)
```
