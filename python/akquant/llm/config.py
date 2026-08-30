"""Configuration loading for the review-center AI subsystem."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class StrategyConfig(_ConfigModel):
    style: str = "a_share_short_term_trend_swing"
    prediction_horizons: list[str] = Field(
        default_factory=lambda: ["next_trading_day", "next_5_trading_days"]
    )
    analysis_schedule: str = "after_daily_close"
    selection_mode: str = "user_selected_pool"


class IntradayKConfig(_ConfigModel):
    enabled: bool = True
    interval_policy: str = "adaptive"
    max_coverage_trading_days: int = Field(default=3, ge=1, le=3)
    session_start: str = "09:30"
    session_end: str = "refresh_time"


class MarketDataConfig(_ConfigModel):
    quote_cache_ttl_seconds: int = Field(default=180, ge=1)
    daily_k_lookback_calendar_days: int = Field(default=360, ge=90)
    daily_k_prompt_bars: int = Field(default=10, ge=1, le=60)
    intraday_k: IntradayKConfig = Field(default_factory=IntradayKConfig)


class ProviderConfig(_ConfigModel):
    enabled: bool = True
    provider_type: str = "openai_compatible"
    api_style: Literal["responses", "chat_completions"] = "responses"
    url: str
    sk: str = ""
    model: str

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled and self.url.strip() and self.sk.strip() and self.model.strip()
        )


class RequestConfig(_ConfigModel):
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    # The structured trade-analysis schema contains several evidence and
    # scenario arrays.  A 2.5k-token cap can truncate otherwise valid JSON
    # from compatible providers (notably DeepSeek), resulting in an EOF parse
    # error and an ``LLM unavailable`` fallback.  Keep enough headroom for the
    # complete schema while still bounding provider usage.
    max_output_tokens: int = Field(default=5000, ge=256)
    stream: bool = False


class FusionConfig(_ConfigModel):
    mode: str = "rule_adjustment"
    initial_traditional_weight: float = Field(default=0.70, ge=0.0, le=1.0)
    initial_llm_max_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    llm_score_max_delta: float = Field(default=10.0, ge=0.0)
    probability_max_delta: float = Field(default=0.08, ge=0.0, le=0.5)
    max_action_step_change: int = Field(default=1, ge=0, le=3)


class KnowledgeConfig(_ConfigModel):
    path: str = "docs/zh/advanced/llm_trade_personal_knowledge.md"
    max_retrieved_rules: int = Field(default=12, ge=1, le=30)


class StorageConfig(_ConfigModel):
    database_path: str = "review_center_ai.sqlite3"
    raw_directory: str = "review_center_ai_raw"
    raw_normal_days: int = Field(default=30, ge=1)
    raw_oversized_days: int = Field(default=7, ge=1)
    oversized_token_threshold: int | None = Field(default=None, ge=1)


class MiaoxiangConfig(_ConfigModel):
    enabled: bool = False
    url: str = "https://mxapi.eastmoney.com/mxds/mcp"
    em_api_key: str = ""
    timeout_seconds: float = Field(default=120.0, gt=0)
    allow_read_tools: bool = True
    read_tool_allowlist: list[str] = Field(default_factory=list)
    allow_self_select_management: bool = True
    allow_simulated_trade_writes: bool = False
    # 个股事实压缩上限：避免妙想原始表格线性放大 LLM 输入。
    stock_max_tables: int = Field(default=6, ge=1, le=20)
    stock_max_rows: int = Field(default=8, ge=1, le=50)
    stock_max_value_chars: int = Field(default=80, ge=16, le=500)
    stock_max_total_chars: int = Field(default=5000, ge=500, le=30000)
    # 公告/新闻属于高 token 外部文本，默认关闭，仅在用户显式开启并将工具加入白名单后调用。
    news_enabled: bool = False
    news_days: int = Field(default=3, ge=1, le=30)
    news_max_items: int = Field(default=3, ge=1, le=20)
    news_max_chars: int = Field(default=1200, ge=200, le=10000)


class TradeAIConfig(_ConfigModel):
    version: int = 1
    active_provider: str = "codexapis"
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    providers: dict[str, ProviderConfig]
    request: RequestConfig = Field(default_factory=RequestConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    miaoxiang: MiaoxiangConfig = Field(default_factory=MiaoxiangConfig)
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)

    @property
    def provider(self) -> ProviderConfig:
        if self.active_provider not in self.providers:
            raise ValueError(f"未配置 Provider: {self.active_provider}")
        return self.providers[self.active_provider]

    def resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.base_dir / path).resolve()


_ENV_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.match(value.strip())
        return os.environ.get(match.group(1), "") if match else value
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件顶层必须是对象: {path}")
    return payload


def repository_root() -> Path:
    """Return the source repository root for editable/development installs."""
    return Path(__file__).resolve().parents[3]


def load_trade_ai_config(
    path: str | Path | None = None, *, base_dir: str | Path | None = None
) -> TradeAIConfig:
    """Load example defaults and overlay the ignored local configuration."""
    root = Path(base_dir).resolve() if base_dir else repository_root()
    example_path = root / "llm_trade.example.yaml"
    configured = (
        path or os.environ.get("AKQUANT_LLM_CONFIG") or root / "llm_trade.local.yaml"
    )
    local_path = Path(configured)
    if not local_path.is_absolute():
        local_path = (root / local_path).resolve()
    merged = _deep_merge(_read_yaml(example_path), _read_yaml(local_path))
    merged = _expand_env(merged)
    config = TradeAIConfig.model_validate(merged)
    config.base_dir = root
    config.config_path = local_path
    return config
