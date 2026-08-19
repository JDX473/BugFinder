# 环境适配层与 Demo 数据

> 相关: [[工具注册与finalize]] | [[知识库与检索]] | [[代码分析专家]]
> 代码: `rcagent/env/adapter.py` + `local.py`
> 论文: §III-B1(工具设计), §IV-B2(数据源)

## 是什么

把论文的"SLS 日志 / advisor 数据库 / 代码仓库"抽象为**可插拔接口**。
框架本体不感知任何具体系统;目标服务通过实现一个 `Environment` 接入。

## 适配协议(Environment)

```python
class Environment(Protocol):
    def register_tools(self, registry, *, include_experts=True) -> None: ...
    def expert_tool_names(self) -> list[str]: ...
```

**服务适配三件套**(PRD §2.11, 框架唯一需要动的地方):

| # | 专属件 | 位置 | 适配动作 |
| --- | --- | --- | --- |
| 1 | 信息收集工具集 | `Environment.register_tools` | 按服务数据面设计工具(语义极简参数) |
| 2 | 领域知识 | `config/prompts/task_requirements.txt` | 服务架构/故障模式/责任规则 |
| 3 | 知识库 | `knowledge.build_demo_kb` → 自定义 | 历史工单/故障记录提炼示例 |

框架本体(OBSK/JsonRegen/专家/TSC/评估)**零改动**。

## 工具契约(与环境无关的统一约定)

- 输入: kwargs 简单参数(实体 ID + 时间窗);
- 输出: 原始文本 → 注册表统一做去重/截断/OBSK 包装(见 [[工具注册与finalize]]);
- **时间截止约束**: 工具只能访问异常检测时刻(`JobDesc.detect_time`)之前的数据
  ——防止分析"未来信息",对齐论文 §IV-B2 的真实排障语义。

## Demo 环境(LocalEnvironment)

基于 `data/demo_jobs/{job_id}/` 目录文件的本地实现,覆盖论文三级数据源:

```
data/demo_jobs/demo_es_conn_timeout/
├── job.json        # job_id / anomaly / detect_time / ground_truth(四项标注)
├── runtime.log     # 运行时日志(3000 行合成,含错误块)
├── platform.log    # 平台层日志
├── infra.log       # 基础设施日志
└── advisor.txt     # advisor 服务历史记录
```

### 合成日志生成(SCENARIOS)

4 个故障场景,每个由"错误行模板 + 正常噪声行"合成:

| 场景 | 根因 | 责任 | 关键错误行 |
| --- | --- | --- | --- |
| es_conn_timeout | ES 客户端连接超时 | platform | SocketTimeoutException |
| oss_lifecycle | bucket 生命周期规则缺失 | user | RequestTimeTooSkewed |
| checkpoint_timeout | checkpoint 超时/状态后端慢 | user | Checkpoint expired |
| task_evicted | 平台资源过度售卖驱逐 | platform | Eviction notice |

生成器特性:
- 行格式对齐 Flink 日志(`时间 LEVEL logger - message`);
- 错误块集中在日志尾部(~10%),夹杂 WARN/INFO;
- 纯数字 token 归一化(mock embedding 用,真实 API embedding 不依赖);
- `--generate` 一键重建(可复现)。

### 时间截止的实现

```python
_read_log(job_id, level, detect_time):
    kept = [line for line in lines if line <= detect_time]
```
按时间戳字符串过滤(合成数据时间单调递增,天然成立)。

## 代码仓库(Demo)

`data/code_repo/` — 模拟 advisor 诊断服务的 8 个 Java 类,
复刻论文图4 的类结构(见 [[代码分析专家]])。
`LocalEnvironment(code_repo=...)` 可指向任意目录(仓库即插件)。

## 扩展路径(目标服务接入)

1. 实现 `Environment`(或直接复用 `LocalEnvironment` 改数据目录);
2. 数据落盘格式对齐:`job.json`(含 ground_truth)+ 日志文件 + 知识库;
3. 替换领域知识模板与责任规则;
4. 验证: Pass Rate ≥ 90% 且 full > react 后视为适配完成。
