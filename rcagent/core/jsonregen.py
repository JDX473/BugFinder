"""JsonRegen(论文 Algorithm 2):LLM 结构化输出的修复层。

流程: 推理前替换 prompt 中非 JSON 文本的敏感字符 → LLM 生成 →
错误转义修复 → 括号匹配提取 JSON → 解析失败则 LLM 转 YAML 再恢复为
JSON,多轮重试直到可解析或超限。

字符替换借用 C 语言 digraph 写法(论文注 3):
    " -> '    [ -> <:    { -> <%
"""

from __future__ import annotations

import json
import re

from ..llm.client import LLMClient

SENSITIVE_TO_CLEAN = {'"': "'", "[": "<:", "{": "<%"}

# 简单格式修复(尾随逗号 / 多余 \' 转义)
_ESCAPE_FIXES: list[tuple[re.Pattern, str]] = [
    # 多余的 \' 转义(JSON 中单引号无需转义)
    (re.compile(r"\\'"), "'"),
    # 尾随逗号
    (re.compile(r",\s*}"), "}"),
    (re.compile(r",\s*\]"), "]"),
]


def _fix_unescaped_controls(text: str) -> str:
    """把 JSON 字符串内部的裸换行/制表符替换为转义形式。

    用状态机区分字符串内外,避免误伤 JSON 结构层的换行
    (结构层换行是合法的;此前用正则实现曾误伤导致整体解析失败)。
    """
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                in_str = False
                out.append(ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    return "".join(out)

_JSON_REPLACE_HINT = (
    "Now, fix the JSON syntax errors and output ONLY the corrected JSON object, "
    "no explanation."
)


def protect_json_objects(text: str) -> tuple[str, list[str]]:
    """找出文本中可被 json.loads 解析的 {...} 对象并替换为占位符。

    论文要求字符替换"忽略真实 JSON 对象"(如历史 action 记录)。
    """
    blocks: list[str] = []
    out = list(text)
    placeholders: list[tuple[int, int, int]] = []  # (start, end, idx)
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 0
            j = i
            in_str = False
            esc = False
            while j < len(text):
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[i : j + 1]
                        try:
                            json.loads(candidate)
                        except json.JSONDecodeError:
                            break  # 不是合法 JSON,按普通文本处理
                        blocks.append(candidate)
                        placeholder = f"@@JSONBLOCK{len(blocks)-1}@@"
                        for k in range(i, j + 1):
                            out[k] = ""
                        out[i] = placeholder
                        i = j
                        break
                j += 1
        i += 1
    return "".join(out), blocks


def sanitize_prompt(prompt: str) -> str:
    """推理前净化:保护 JSON 对象后替换非 JSON 文本的敏感字符。"""
    protected, blocks = protect_json_objects(prompt)
    for sensitive, clean in SENSITIVE_TO_CLEAN.items():
        protected = protected.replace(sensitive, clean)
    for idx, block in enumerate(blocks):
        protected = protected.replace(f"@@JSONBLOCK{idx}@@", block)
    return protected


def find_json(text: str) -> str | None:
    """花括号匹配提取最外层 {...} 子串(Algorithm 2 的 FINDJSON)。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def fix_escapes(text: str) -> str:
    text = _fix_unescaped_controls(text)
    for pattern, repl in _ESCAPE_FIXES:
        text = pattern.sub(repl, text)
    return text


def _try_parse(text: str) -> tuple[dict | None, str | None]:
    """尝试解析:标准解析 → 单引号转双引号后再解析。"""
    try:
        return json.loads(text), text
    except json.JSONDecodeError:
        pass
    # LLM 可能把字符串值内的引号输出为单引号(受净化影响)。
    # 无条件尝试转换:只替换与单词字符相邻的单引号,JSON 结构引号不受影响。
    if "'" in text:
        converted = re.sub(r"(?<=\W)'|'(?=\W)", '"', text)
        try:
            return json.loads(converted), converted
        except json.JSONDecodeError:
            pass
    return None, text


def parse_json_or_none(text: str) -> dict | None:
    """完整解析管线(不依赖 LLM 重试):返回 dict 或 None。"""
    fixed = fix_escapes(text)
    block = find_json(fixed)
    if block is None:
        return None
    parsed, _ = _try_parse(block)
    return parsed


def json_regen(
    llm: LLMClient,
    prompt: str,
    *,
    retries: int = 3,
    temperature: float = 0.0,
) -> dict | None:
    """Algorithm 2 完整流程:净化 prompt → 生成 → 修复/重试 → 返回 dict 或 None。"""
    messages = [
        {"role": "system", "content": "You are a JSON format repair assistant."},
        {"role": "user", "content": prompt},
    ]
    clean_prompt = sanitize_prompt(prompt)
    gen = llm.chat([{"role": "system", "content": "You are a JSON format repair assistant."},
                    {"role": "user", "content": clean_prompt}], temperature=temperature)
    j = gen.text
    for _ in range(retries):
        j = fix_escapes(j)
        block = find_json(j)
        if block is not None:
            parsed, _ = _try_parse(block)
            if parsed is not None:
                return parsed
        # 转 YAML -> 恢复 JSON
        y = llm.chat([{"role": "system", "content": "Extract the structure of the following "
                       "content into YAML. Output only YAML, no explanation."},
                      {"role": "user", "content": j}], temperature=temperature)
        j = llm.chat([{"role": "system", "content": "Restore the following YAML to a correct "
                       "JSON object with identical structure and content. Output only the JSON."},
                      {"role": "user", "content": y.text}], temperature=temperature).text
    return None
