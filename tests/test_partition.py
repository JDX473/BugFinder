"""语义分区(Algorithm 1 步骤 1-12)单元测试: 连续性 / 错误块聚类 / 去重叠。"""

from rcagent.experts.partition import greedy_overlap_removal, semantic_partition
from rcagent.llm.embedding import Embedder


class TestGreedyOverlapRemoval:
    def test_makes_labels_contiguous(self):
        labels = [1, 1, 2, 1, 2, 2, 3, 3, 1, 1]
        out = greedy_overlap_removal(labels)
        # 每个标签只出现一个连续段
        for label in set(out):
            segments = []
            for i, v in enumerate(out):
                if v == label and (i == 0 or out[i - 1] != label):
                    segments.append(i)
            assert len(segments) == 1, f"label {label} not contiguous: {out}"

    def test_single_label_unchanged(self):
        assert greedy_overlap_removal([1] * 5) == [1] * 5


class TestSemanticPartition:
    def _log(self):
        # 正常行 + 末尾错误块(共享 ERROR/异常关键词,词袋 embedding 下相似)
        normal = [f"2024-01-01 09:00:{i:02d} INFO org.apache.flink.runtime - "
                  f"heartbeat ok {i}" for i in range(80)]
        err = [f"2024-01-01 09:05:{i:02d} ERROR org.apache.flink.connector.elasticsearch - "
               f"SocketTimeoutException connect timed out {i}" for i in range(20)]
        return normal + err

    def test_chunks_reassemble_to_original(self):
        lines = self._log()
        chunks = semantic_partition(lines, Embedder(cfg={"provider": "mock"}))
        assert [ln for c in chunks for ln in c] == lines  # 连续无丢失

    def test_error_lines_cluster_together(self):
        lines = self._log()
        chunks = semantic_partition(lines, Embedder(cfg={"provider": "mock"}))
        # 错误行全部保留,且聚集在少数相邻 chunk 中(不被正常行打散)
        err_chunks = [i for i, c in enumerate(chunks) if any("ERROR" in l for l in c)]
        total_err = sum("ERROR" in l for c in chunks for l in c)
        assert total_err == 20
        assert len(err_chunks) <= 2, f"error lines scattered across {len(err_chunks)} chunks"
        assert max(err_chunks) - min(err_chunks) + 1 <= 2  # 相邻
