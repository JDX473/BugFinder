"""Embedding 封装:配置化 API(用户提供模型后填入)+ mock 占位。

论文 §IV-A 使用 GTE-LARGE;本项目使用用户后续提供的 API embedding 模型。
mock 模式返回确定性 hash 向量,保证测试可复现。
"""

from __future__ import annotations

import hashlib
import math
import os

from ..config import Config


class Embedder:
    def __init__(self, cfg: Config):
        self.provider = cfg.get("provider", "mock")
        self.model = cfg.get("model") or ""
        self.dim = cfg.get("dim", 1024)
        self._client = None
        if self.provider == "openai_compat":
            import openai

            api_key = os.environ.get(cfg.get("api_key_env") or "EMBEDDING_API_KEY")
            if not api_key:
                raise ValueError("embedding provider=openai_compat 需要 API key")
            base_url = cfg.get("base_url") or None
            self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=60)
        if self.provider == "mock" and not self.model:
            self.model = "mock-embedding"

    def embed(self, texts: list[str], batch_size: int = 20) -> list[list[float]]:
        """批量 embedding;batch_size 默认 20(对齐 qwen embedding API 上限)。"""
        if self.provider == "mock":
            return [self._mock_vec(t) for t in texts]
        assert self._client is not None
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            resp = self._client.embeddings.create(
                model=self.model, input=texts[i : i + batch_size])
            out.extend(d.embedding for d in resp.data)
        return out

    def _mock_vec(self, text: str) -> list[float]:
        """确定性伪向量: 词袋特征 hash 到 dim 维单位球面。

        纯数字 token 归一化为 <num>(时间戳/序号差异不影响相似度,
        接近真实 embedding 的行为)。
        """
        vec = [0.0] * self.dim
        for tok in text.lower().replace("_", " ").split():
            if tok.isdigit():
                tok = "<num>"
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
