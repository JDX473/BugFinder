"""RCA Agent 全局配置。

全部从环境变量读取，骨架阶段默认走 mock 数据源；将来接入真实环境时
通过环境变量注入连接信息与预算，不修改任何代码。

约定：所有配置项都有合理默认值，便于本地零配置开发与测试。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class DataSourceConfig:
    """数据源接入配置。骨架阶段默认 mock，接真环境时设 RCA_DATA_SOURCE=real。"""

    # mock | real（real 时需配 ES/Prometheus 连接）
    source: str = field(default_factory=lambda: os.environ.get("RCA_DATA_SOURCE", "mock"))

    # 日志查询（ES/OpenSearch 方言，Q1 确认后填）
    log_es_url: str = field(default_factory=lambda: os.environ.get("RCA_LOG_ES_URL", ""))
    log_es_index: str = field(default_factory=lambda: os.environ.get("RCA_LOG_ES_INDEX", ""))

    # 指标查询（Prometheus）
    prometheus_url: str = field(default_factory=lambda: os.environ.get("RCA_PROMETHEUS_URL", ""))

    # 查询护栏
    max_log_hits: int = field(default_factory=lambda: _env_int("RCA_MAX_LOG_HITS", 1000))
    max_query_calls: int = field(default_factory=lambda: _env_int("RCA_MAX_QUERY_CALLS", 20))


@dataclass
class LLMConfig:
    """LLM 接入配置。DeepSeek 走 OpenAI 兼容接口；未配置 key 时启用 mock 模式。"""

    model: str = field(default_factory=lambda: os.environ.get("RCA_LLM_MODEL", "deepseek-chat"))
    base_url: str = field(default_factory=lambda: os.environ.get("RCA_LLM_BASE_URL", "https://api.deepseek.com/v1"))
    api_key: str = field(default_factory=lambda: os.environ.get("RCA_LLM_API_KEY", ""))
    temperature: float = field(default_factory=lambda: _env_float("RCA_LLM_TEMPERATURE", 0.0))

    # 结构化输出重试与兜底
    max_json_retries: int = field(default_factory=lambda: _env_int("RCA_LLM_MAX_JSON_RETRIES", 3))

    @property
    def enabled(self) -> bool:
        """未配置 key 时视为禁用，调用方应走 mock/兜底。"""
        return bool(self.api_key)


@dataclass
class BudgetConfig:
    """单事件调查预算（PRD §7.3）。"""

    token_budget: int = field(default_factory=lambda: _env_int("RCA_BUDGET_TOKENS", 200_000))
    time_budget_sec: int = field(default_factory=lambda: _env_int("RCA_BUDGET_TIME_SEC", 600))
    query_budget: int = field(default_factory=lambda: _env_int("RCA_BUDGET_QUERIES", 20))
    max_concurrency: int = field(default_factory=lambda: _env_int("RCA_MAX_CONCURRENCY", 3))


@dataclass
class Settings:
    data_source: DataSourceConfig = field(default_factory=DataSourceConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)


# 进程级单例，各模块共享
settings = Settings()
