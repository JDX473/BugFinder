"""语义指标(论文 §IV-C)。

- METEOR / BERTScore(deberta-large-mnli) / NUBIA(6-dim) / BLEURT /
  BARTScore(F-Score, CNNDM) / EmbScore(默认 embedding 余弦, (1+cos)/2)
- NormScore: (Score(p,r) − Score(b,r)) / (1 − Score(b,r)),基线 b="Unclear"

权重类指标延迟导入;依赖缺失时 raise MetricUnavailable(带安装指引)。
失败/不完整预测由上层先填 "Unclear" 再参与计算(论文 baseline 填充)。
"""

from __future__ import annotations

import math
import zipfile
from functools import lru_cache

from ..llm.embedding import Embedder

UNCLEAR = "Unclear"


class MetricUnavailable(Exception):
    pass


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def emb_score(preds: list[str], refs: list[str], embedder: Embedder) -> list[float]:
    """EmbScore = (1 + cos(Emb(p), Emb(r))) / 2。"""
    pv = embedder.embed(preds)
    rv = embedder.embed(refs)
    return [(1.0 + _cosine(p, r)) / 2.0 for p, r in zip(pv, rv)]


# ---- 权重类指标(延迟导入) ------------------------------------------------


def _meteor_single(pred: str, ref: str) -> float:
    """轻量 METEOR: 精确 + Porter 词干匹配,无 wordnet 同义词层。

    与标准 METEOR 的差异仅在"同义词匹配"一档,用于相对对比足够;
    wordnet 数据可用时 meteor() 走 nltk 标准实现。
    参数: alpha=0.9, beta=3, gamma=0.5(标准默认)。
    """
    from nltk.stem import PorterStemmer

    ps = PorterStemmer()
    p_toks = [ps.stem(w.lower()) for w in pred.split()]
    r_toks = [ps.stem(w.lower()) for w in ref.split()]
    if not p_toks or not r_toks:
        return 0.0

    # 贪心对齐: 每个 ref token 最多匹配一个 pred token
    matched = [False] * len(p_toks)
    m = 0
    for rt in r_toks:
        for i, pt in enumerate(p_toks):
            if not matched[i] and pt == rt:
                matched[i] = True
                m += 1
                break

    # 惩罚: 相邻匹配的连续段数
    chunks = 0
    prev = False
    for flag in matched:
        if flag and not prev:
            chunks += 1
        prev = flag

    if m == 0:
        return 0.0
    precision = m / len(p_toks)
    recall = m / len(r_toks)
    fmean = (10 * precision * recall) / (recall + 9 * precision)
    penalty = 0.5 * (chunks / m) ** 3
    return fmean * (1 - penalty)


def meteor(preds: list[str], refs: list[str]) -> list[float]:
    try:
        import nltk
        from nltk.translate import meteor_score
    except ImportError:
        raise MetricUnavailable("pip install nltk") from None

    try:
        nltk.data.find("corpora/wordnet")
        return [meteor_score.single_meteor_score(r.split(), p.split(), gamma=0)
                for p, r in zip(preds, refs)]
    except (LookupError, zipfile.BadZipFile, KeyError):
        # wordnet 数据缺失或损坏(受限网络等),退回轻量实现
        return [_meteor_single(p, r) for p, r in zip(preds, refs)]


@lru_cache(maxsize=1)
def _bert_scorer():
    try:
        from bert_score import BERTScorer
    except ImportError:
        raise MetricUnavailable("pip install bert-score") from None
    # 论文: BERTScore (deberta-large-mnli)
    return BERTScorer(
        model_type="microsoft/deberta-large-mnli",
        lang="en",
        rescale_with_baseline=True,
        device="cpu",
    )


def bertscore_f1(preds: list[str], refs: list[str]) -> list[float]:
    scorer = _bert_scorer()
    _, _, f1 = scorer.score(preds, refs)
    return [float(v) for v in f1]


@lru_cache(maxsize=1)
def _nubia():
    try:
        from nubia_score import NubiaScore
    except ImportError:
        raise MetricUnavailable(
            "pip install nubia-score (需先 pip install torch; 模型从 HF 下载)"
        ) from None
    return NubiaScore()


def nubia(preds: list[str], refs: list[str]) -> list[float]:
    scorer = _nubia()
    return [float(scorer.scoring([p], [r])[0]) for p, r in zip(preds, refs)]


@lru_cache(maxsize=1)
def _bleurt():
    try:
        from bleurt.score import BleurtScorer
    except ImportError:
        raise MetricUnavailable(
            "pip install bleurt (需 TensorFlow; 若不可用可跳过该指标, "
            "论文中 BLEURT 与其他指标趋势一致)"
        ) from None
    # BLEURT-20 checkpoint 需自行下载: https://github.com/google-research/bleurt
    return BleurtScorer("BLEURT-20")


def bleurt(preds: list[str], refs: list[str]) -> list[float]:
    scorer = _bleurt()
    return [float(s) for s in scorer.score(references=refs, candidates=preds)]


@lru_cache(maxsize=1)
def _bartscore():
    try:
        from transformers import AutoTokenizer, BartForConditionalGeneration
    except ImportError:
        raise MetricUnavailable("pip install transformers torch") from None
    tok = AutoTokenizer.from_pretrained("facebook/bart-large-cnn")
    model = BartForConditionalGeneration.from_pretrained("facebook/bart-large-cnn")
    return tok, model


def _bartscore_single(pred: str, ref: str) -> float:
    """BARTScore F-Score (CNNDM): 论文表注 "F-Score, CNNDM"。"""
    import torch

    tok, model = _bartscore()
    model.eval()
    with torch.no_grad():
        # 前向: 以 ref 生成 pred 的概率(直接参考官方脚本的精度/召回分解)
        enc_ref = tok([ref], return_tensors="pt", truncation=True, max_length=1024)
        dec_pred = tok([pred], return_tensors="pt", truncation=True, max_length=1024)
        logits = model(**enc_ref, labels=dec_pred["input_ids"]).logits
        loss = torch.nn.functional.cross_entropy(
            logits.view(-1, logits.size(-1)), dec_pred["input_ids"].view(-1),
            reduction="sum")
        # 归一化到每 token 负对数似然
        nll = loss.item() / max(dec_pred["input_ids"].numel(), 1)
        score_fwd = -nll
        enc_pred = tok([pred], return_tensors="pt", truncation=True, max_length=1024)
        dec_ref = tok([ref], return_tensors="pt", truncation=True, max_length=1024)
        logits2 = model(**enc_pred, labels=dec_ref["input_ids"]).logits
        loss2 = torch.nn.functional.cross_entropy(
            logits2.view(-1, logits2.size(-1)), dec_ref["input_ids"].view(-1),
            reduction="sum")
        nll2 = loss2.item() / max(dec_ref["input_ids"].numel(), 1)
        score_bwd = -nll2
    return float(2 * score_fwd * score_bwd / (score_fwd + score_bwd + 1e-9))


def bartscore_f1(preds: list[str], refs: list[str]) -> list[float]:
    return [_bartscore_single(p, r) for p, r in zip(preds, refs)]


# ---- 归一化与汇总 ------------------------------------------------------------


def norm_scores(scores: list[float], baseline_scores: list[float]) -> list[float]:
    """NormScore(p,r) = (Score(p,r) − Score(b,r)) / (1 − Score(b,r)), b="Unclear"。"""
    out = []
    for s, b in zip(scores, baseline_scores):
        denom = 1.0 - b
        out.append((s - b) / denom if abs(denom) > 1e-9 else 0.0)
    return out


def mean_std(values: list[float]) -> tuple[float, float]:
    """报表用 mean±std(SC 多次运行场景,论文 Table I-V 风格)。"""
    import statistics

    if not values:
        return float("nan"), float("nan")
    return statistics.mean(values), statistics.pstdev(values)


METRIC_NAMES = ["METEOR", "BERTScore", "EmbScore", "NUBIA", "BLEURT", "BARTScore"]


def compute(
    metric: str,
    preds: list[str],
    refs: list[str],
    embedder: Embedder | None = None,
) -> list[float]:
    """按名计算单个指标;未知指标抛 ValueError。"""
    m = metric.lower()
    if m == "meteor":
        return meteor(preds, refs)
    if m == "bertscore":
        return bertscore_f1(preds, refs)
    if m == "embscore":
        if embedder is None:
            raise ValueError("EmbScore 需要 embedder")
        return emb_score(preds, refs, embedder)
    if m == "nubia":
        return nubia(preds, refs)
    if m == "bleurt":
        return bleurt(preds, refs)
    if m == "bartscore":
        return bartscore_f1(preds, refs)
    raise ValueError(f"unknown metric: {metric}")
