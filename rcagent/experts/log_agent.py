"""Log Expert Agent(论文 Algorithm 1):长日志的 in-context RAG 分析。

流程: 语义分区 → 分块分析(ICP + 零样本 CoT + 证据逐字复制)→
Levenshtein 幻觉过滤(证据必须可模糊匹配到 chunk)→ LLM 总结。

论文关键点: 长 prompt 会淹没示例与目标数据的分隔符,导致 LLM 分析
in-context 示例而非目标 chunk;因此强制 evidence 逐字复制自日志原文,
无法模糊匹配的分析结果被丢弃。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein

from ..config import Config
from ..core.jsonregen import json_regen
from ..llm.client import LLMClient
from ..llm.embedding import Embedder
from .knowledge import KnowledgeBase
from .partition import DEFAULT_WINDOW, semantic_partition

logger = logging.getLogger(__name__)

_ANALYZE_TEMPLATE = """\
Analyze the following log chunk from an anomalous job. Think step by step, then
report the root-cause-relevant interpretation and the supporting evidence.

{icp}

Target log chunk:
{chunk}

Respond with a JSON object:
{{"interpretation": "<concise analysis of what failed and why>",
 "evidence": "<verbatim log lines copied EXACTLY from the target log chunk>"}}
The evidence must be copied character-for-character from the target log chunk. \
Never copy from the examples."""

_SUMMARIZE_TEMPLATE = """\
Below are the analyses of several chunks of one long log. Merge them into a
single final interpretation, keeping the strongest evidence.

{results}

Respond with a JSON object:
{{"interpretation": "<final merged interpretation>",
 "evidence": "<the single strongest verbatim evidence line>"}}"""


@dataclass
class ChunkAnalysis:
    interpretation: str
    evidence: str


class LogExpertAgent:
    def __init__(
        self,
        llm: LLMClient,
        embedder: Embedder,
        kb: KnowledgeBase,
        cfg: Config | None = None,
    ):
        self.llm = llm
        self.embedder = embedder
        self.kb = kb
        self.cfg = cfg.get("log_agent") if cfg is not None else None
        self._cache: dict[str, str] = {}  # 同一日志只分析一次(快照内容不变)

    # -- 配置 -------------------------------------------------------------

    def _opt(self, key: str, default):
        return self.cfg.get(key, default) if self.cfg else default

    # -- 主入口 ------------------------------------------------------------

    def run(self, log_text: str) -> str:
        """Algorithm 1 主流程;返回给 controller 的 observation 文本。"""
        if not log_text or not log_text.strip():
            return "{interpretation: insufficient log content provided, evidence: []}"
        key = hashlib.md5(log_text.encode()).hexdigest()
        if key in self._cache:
            return self._cache[key]
        result = self._run_uncached(log_text)
        self._cache[key] = result
        return result

    def _run_uncached(self, log_text: str) -> str:
        lines = log_text.splitlines()
        if not lines or not any(l.strip() for l in lines):
            return "{interpretation: insufficient log content provided, evidence: []}"

        chunks = semantic_partition(
            lines, self.embedder,
            window=self._opt("window", DEFAULT_WINDOW),
        )
        chunks = self._merge_tiny_chunks(chunks)
        # 按可疑度(ERROR/Exception 密度)降序:错误块必然进入分析,
        # 超限时优先裁掉纯正常区域(工程化裁剪,论文未公开该细节)
        chunks.sort(key=self._suspicion, reverse=True)
        chunks = chunks[: self._opt("max_chunks", 30)]

        # 分块分析并发执行(chunk 间无依赖;论文每 chunk 一轮,串行/并行不影响语义)
        from concurrent.futures import ThreadPoolExecutor

        accepted: list[ChunkAnalysis] = []
        with ThreadPoolExecutor(max_workers=self._opt("concurrency", 4)) as pool:
            futures = {pool.submit(self._analyze_chunk, "\n".join(c)): c for c in chunks}
            for fut, chunk in futures.items():
                analysis = fut.result()
                if analysis is None:
                    continue
                chunk_text = "\n".join(chunk)
                if self._evidence_ok(analysis.evidence, chunk_text):
                    accepted.append(analysis)
                else:
                    logger.debug("hallucinated evidence discarded: %r",
                                 analysis.evidence[:80])

        if not accepted:
            return "{interpretation: no reliable analysis after evidence filtering, evidence: []}"

        return self._summarize(accepted)

    @staticmethod
    def _suspicion(chunk: list[str]) -> float:
        """可疑度: ERROR/Exception 行占比(0~1)。"""
        hits = sum(1 for l in chunk if "ERROR" in l or "Exception" in l)
        return hits / max(len(chunk), 1)

    # -- 分块分析 ------------------------------------------------------------

    def _analyze_chunk(self, chunk_text: str) -> ChunkAnalysis | None:
        icp = self.kb.build_icp(chunk_text, top_k=self._opt("top_k", 3))
        prompt = _ANALYZE_TEMPLATE.format(icp=icp, chunk=chunk_text)
        out = json_regen(
            self.llm, prompt,
            retries=self._opt("jsonregen_retries", 2),
            temperature=self._opt("temperature", 0.0),
        )
        if out is None:
            logger.debug("chunk analysis unparsable, dropped")
            return None
        interp = out.get("interpretation")
        ev = out.get("evidence")
        if not isinstance(interp, str) or not isinstance(ev, str) or not ev.strip():
            return None
        return ChunkAnalysis(interpretation=interp.strip(), evidence=ev.strip())

    # -- 幻觉过滤(论文步骤 24-27) --------------------------------------------

    def _evidence_ok(self, evidence: str, chunk_text: str) -> bool:
        """LEVENSHTEIN(e, p) < L(p) − L(e) × 0.9 才接受(Algorithm 1 步骤 25)。"""
        if not evidence or not chunk_text:
            return False
        dist = Levenshtein.distance(evidence, chunk_text)
        return dist < len(chunk_text) - len(evidence) * 0.9

    # -- 总结 ----------------------------------------------------------------

    def _summarize(self, analyses: list[ChunkAnalysis]) -> str:
        results = "\n\n".join(
            f"Chunk {i + 1}:\ninterpretation: {a.interpretation}\nevidence: {a.evidence}"
            for i, a in enumerate(analyses)
        )
        prompt = _SUMMARIZE_TEMPLATE.format(results=results)
        out = json_regen(self.llm, prompt, retries=self._opt("jsonregen_retries", 2))
        if out is not None and isinstance(out.get("interpretation"), str):
            interp = out["interpretation"].strip()
            ev = out.get("evidence")
            ev = ev.strip() if isinstance(ev, str) else analyses[0].evidence
            return f"interpretation: {interp}\nevidence: {ev}"
        # LLM 总结失败时退化为拼接最强分析
        best = max(analyses, key=lambda a: len(a.evidence))
        return f"interpretation: {best.interpretation}\nevidence: {best.evidence}"

    # -- 小 chunk 合并 ---------------------------------------------------------

    def _merge_tiny_chunks(self, chunks: list[list[str]]) -> list[list[str]]:
        """过小的 chunk(分区噪声)并入相邻块,减少碎片化分析。"""
        min_lines = self._opt("min_chunk_lines", 2)
        merged: list[list[str]] = []
        for chunk in chunks:
            if merged and len(chunk) < min_lines:
                merged[-1].extend(chunk)
            else:
                merged.append(chunk)
        return merged
