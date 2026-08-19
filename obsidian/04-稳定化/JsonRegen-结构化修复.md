# JsonRegen 结构化修复

> 相关: [[Controller-Agent-循环]] | [[错误处理与解码策略]]
> 代码: `rcagent/core/jsonregen.py`
> 论文: §III-C1, Algorithm 2

## 是什么

保证 LLM 结构化输出可解析的**修复层**。controller 的动作、expert 的返回、
聚合的中间产物全部是 JSON——而 LLM(尤其弱模型、复杂内容下)经常输出
坏 JSON:未转义引号、错转义、截断、被自然语言包裹。JsonRegen 用
"预防 + 修复 + 重试"三阶段让输出尽量可解析。

论文为什么不用现成工具:JSONFormer/TypeChat 要么无法处理带大量转义字符的
自由 JSON,要么只依赖 LLM 逐 token 纠错——对噪声云数据中的复杂输出不敏感。
论文还明确: **即使 ChatGPT 在内容复杂时也会产生错误结构**——所以
API 模型阶段此模块依然必要。

## 三阶段流程

### 阶段 1: 推理前净化(sanitize_prompt)

LLM 生成前,把 prompt 中**非 JSON 文本**的敏感字符替换掉,降低它"引用内容时
不转义"的概率:

| 敏感字符 | 替换为 |
| --- | --- |
| `"`(双引号) | `'` |
| `[` | `<:` |
| `{` | `<%` |

关键:**真实 JSON 对象(如历史动作记录)受保护**——`protect_json_objects`
先扫描出能被 `json.loads` 解析的完整对象替换为占位符,净化后再恢复。

### 阶段 2: 输出修复(fix_escapes + find_json)

LLM 输出 `J` 后依次:
1. **状态机修复字符串内的裸换行/制表符** → 转义形式
   (只处理字符串内部,结构层换行合法——这是真实运行中修复过的重要 bug,
   此前用正则实现会误伤 `{` 后的结构换行,导致整个 JSON 变非法);
2. **简单格式修复**:多余 `\'` 转义、尾随逗号;
3. **FINDJSON**:花括号匹配提取最外层 JSON 块(容忍被自然语言包裹);
4. `_try_parse` 两级解析:
   - 标准 `json.loads`;
   - 失败且存在单引号 → 将**与单词字符相邻的单引号**转成双引号再试
     (LLM 受净化影响可能把字符串值写成 `'SINK_CONN_ERROR'`)。

### 阶段 3: LLM 重试(YAML 中转)

仍不可解析时(论文 Algorithm 2 步骤 9-10):
1. 让 LLM "Extract structure into YAML"(理解 JSON 结构);
2. 再让 LLM "Restore to correct JSON"(恢复为合法 JSON);
3. 回到阶段 2,重试直到可解析或超过上限(默认 3 轮);
4. 超限返回 None(EmptyObject),上层按无效动作处理。

## 调用链全景

```
Controller 动作       → parse_action → jsonregen.parse_json_or_none(阶段2)
专家工具输出         → json_regen(阶段1+2+3,带重试)
聚合中间产物         → json_regen
```

```python
# 无 LLM 重试的轻量路径(controller 动作解析用,快)
parse_json_or_none(text) -> dict | None

# 完整路径(专家/聚合用,带净化+YAML重试)
json_regen(llm, prompt, retries=3) -> dict | None
```

## 真实运行中的表现

- controller 动作多为单行 JSON,轻量路径足够;
- 专家输出(多行 JSON + 从日志复制的证据行)复杂得多,完整路径兜底;
- 真实轨迹中观察到净化痕迹(`<:Elasticsearch:9200]`)——证明证据确实是
  从被净化的 chunk 里逐字复制的(见 [[日志分析专家-Algorithm1]])。

## 消融

`variant="no_jsonregen"` 时 `use_regen=False`,解析走简化路径
(直接 `json.loads` 最外层花括号块,无任何修复)——对应论文消融实验
"w/o JsonRegen"(Pass Rate 从 99% 降到 85%,主要因错误决策)。

## 关键代码索引

| 函数 | 职责 |
| --- | --- |
| `sanitize_prompt` | 阶段1: 保护 JSON 对象 + 敏感字符替换 |
| `protect_json_objects` | 扫描可解析 JSON 对象并占位 |
| `fix_escapes` / `_fix_unescaped_controls` | 阶段2: 状态机修复 + 格式修复 |
| `find_json` | 花括号匹配提取 |
| `_try_parse` | 标准解析 → 单引号转换解析 |
| `parse_json_or_none` | 轻量路径(无 LLM 重试) |
| `json_regen` | 完整路径(净化 + 重试 + YAML 中转) |
