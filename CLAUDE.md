# CLAUDE.md — 项目工作约定

本文件是给在仓库里工作的 AI 助手（Claude）看的项目记忆，随仓库提交、团队共享。

## 开发工作流规矩（必须遵守）

**每次向远程推送代码（`git push`）之前，必须先沉淀文档。** 具体指：

1. **本仓库 `docs/` 下的实现文档**：新模块、新能力上线后，在 `docs/` 下新增或更新一份"实现细节"文档，记录：
   - 该模块解决什么问题、对应 PRD 哪一节
   - 核心实现思路 / 算法 / 关键设计决策
   - 数据模型 / 接口约定（字段、含义）
   - 已知边界与局限（如 trace 重建依赖埋点完整性）
   - 本地如何运行验证（命令）
2. **文档必须随代码同一提交进仓库**：不允许"代码推上去了、文档之后补"。两者进同一个 commit，保证历史可追溯。
3. **代码提交信息（commit message）要能自解释**：写清"为什么/做了什么"，不写空泛的 "update"。
4. **踩坑必须沉淀进优化日记**：每次开发中踩坑、解决完后，把 **坑 + 解决办法 + 避坑方法**
   追加到 `docs/optimization-diary.md`（编号递增，按模块归类）。**不要混入设计文档**——
   设计文档回答"为什么这么设计"，优化日记回答"这个坑怎么踩的、以后怎么避免"。

## 项目背景与硬约束

- 线上微服务故障根因定位 Agent（RCA Agent），定位 L2→L3（LLM 辅助 + 人在环上）
- 架构：**确定性工作流 + 有界 ReAct**（harness）——确定性为主干，LLM 只在关键判断点
- 硬约束：
  1. 数据信号只有日志 + 机器指标 + traceId（无完整链路存储，靠日志按 traceId 重建）
  2. 拿不到变更/发布事件，不依赖变更关联
  3. 大模型选型 DeepSeek（不支持 Structured Output，须走 `ask_json` shim）
- 权威规格：PRD 见 `docs/PRD.md`（§8 输入输出 schema 由 `app/schema/models.py` 唯一实现）

## 代码结构速览

- `app/schema/models.py` —— IncidentEvent / Evidence / RCAReport（Pydantic，PRD §8 权威实现）
- `app/llm/ask_json.py` —— DeepSeek 结构化输出 shim（提示词约束→json→jsonschema→≤3 重试→确定性兜底）
- `app/llm/protocol.py` —— LLM 访问协议（LLMClient，可注入 mock）
- `app/llm/deepseek_llm.py` —— DeepSeek 生产实现（OpenAI 兼容接口）
- `app/tools/base.py` —— 数据源适配器协议 + 查询护栏
- `app/tools/mock_datasource.py` —— mock 数据源（离线开发/测试）
- `app/tools/trace_reconstruction.py` —— traceId 重建调用链（关键路径）
- `scripts/run_trace_rebuild.py` —— CLI 原型
- `config/settings.py` —— 全环境变量配置，骨架默认 mock

## 常用命令

```bash
# 测试
.venv/Scripts/python.exe -m pytest

# trace 重建 CLI（mock 数据）
.venv/Scripts/python.exe scripts/run_trace_rebuild.py tr-mock-0001
.venv/Scripts/python.exe scripts/run_trace_rebuild.py tr-mock-0002

# 环境变量配置见 config/settings.py（RCA_DATA_SOURCE / RCA_LLM_API_KEY 等）
```

## 分支与提交流程

- 当前主开发分支：`dev`（跟踪 `origin/dev`）；`main` 保留文档基线
- 功能开发在 `dev` 上进行，**每次 push 前完成本文档沉淀**
