"""Configuration schema for the A-share stock monitoring system.

All tunable parameters live here as a single dataclass so that every
downstream module can receive the same config object without scattered
magic numbers or env-var reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class StockSelectorConfig:
    """Thresholds and scoring weights for the stock screening engine."""

    # -- Screening thresholds -------------------------------------------------
    min_volume_5d: float = 100_000_000.0
    """5-day average turnover floor (RMB)."""

    rsi_lower: float = 40.0
    """RSI-14 lower bound for screening."""

    rsi_upper: float = 70.0
    """RSI-14 upper bound for screening."""

    pe_lower: float = 0.0
    """PE lower bound (exclusive)."""

    pe_upper: float = 100.0
    """PE upper bound (exclusive)."""

    max_change_20d: float = 0.50
    """Maximum 20-day return (50 %) before a stock is excluded."""

    # -- Volume surge config --------------------------------------------------
    volume_surge_avg_column: str = "volume_5d"
    """Column name for the average volume used in surge comparison.
    Set to ``volume_5d_shares`` when ``volume_today`` is in shares and
    ``volume_5d`` is an RMB turnover column."""

    # -- Scoring weights ------------------------------------------------------
    score_volume_surge: int = 2
    """Bonus when today's volume > 1.5x the 5-day average."""

    score_macd_expanding: int = 2
    """Bonus when MACD histogram is positive and rising."""

    score_breakout: int = 2
    """Bonus when close exceeds the 20-day high."""

    score_sector_hot: int = 1
    """Bonus when the stock's sector is in the top-10 performing list."""

    # -- Volume surge multiplier ----------------------------------------------
    volume_surge_ratio: float = 1.5
    """Today's volume must exceed this multiple of the 5-day average."""

    # -- Top-N sector cutoff --------------------------------------------------
    top_sector_count: int = 10
    """Number of top sectors that qualify for the sector bonus."""

    # -- NaN handling ---------------------------------------------------------
    nan_policy: str = "exclude"
    """Policy for rows with NaN values in numeric filter columns.

    Options:
    - ``"exclude"`` (default): NaN rows fail the filter (backward compatible).
    - ``"include"``: NaN rows pass the filter.

    When ``"exclude"``, rows dropped by NaN in each filter are logged."""

    log_filter_drops: bool = False
    """When True, log the number of rows dropped by each filter step."""



@dataclass
class AlertEngineConfig:
    """Thresholds and limits for real-time alerting."""

    change_pct_threshold: float = 5.0
    """Absolute percentage change that triggers a HIGH alert."""

    volume_surge_ratio: float = 3.0
    """Volume multiplier over 5-day average for MEDIUM alert."""

    max_alerts_per_stock_per_day: int = 2
    """Maximum alert count per stock per trading day."""

    min_volume_absolute: float = 10_000_000.0
    """Minimum absolute volume (shares) required for a volume-surge alert.
    A low-liquidity stock must exceed both the relative multiplier *and*
    this floor before triggering MEDIUM volume surge."""

    rsi_divergence_enabled: bool = False
    """When True, emit MEDIUM alerts for RSI divergence (bearish or bullish).
    Requires optional columns ``rsi14``, ``rsi14_prev``, ``high_today``,
    ``high_prev`` to be present in the input DataFrame."""


@dataclass
class DailyReportConfig:
    """Parameters for daily report generation."""

    top_candidates_count: int = 3
    """Number of candidate stocks shown in the Tomorrow's Watch section."""

    bullish_ratio: float = 1.5
    """Advance/decline ratio above which sentiment is 'bullish'."""

    cautious_ratio: float = 0.67
    """Advance/decline ratio below which sentiment is 'cautious'."""

    # -- Enhanced sentiment thresholds ----------------------------------------
    sentiment_turnover_divergence_pct: float = 0.20
    """When total_turnover deviates from turnover_20d_avg by more than this
    fraction, sentiment is adjusted toward bullish (above) or cautious (below)."""

    index_ma20_enabled: bool = True
    """When True and index_points is available alongside index_ma20, factor
    index position relative to its 20-day MA into sentiment."""


@dataclass
class NotificationConfig:
    """Notification channel placeholders."""

    webhook_url_high: str = "https://hooks.example.com/high"
    """Webhook URL for HIGH-severity alerts."""

    webhook_url_medium: str = "https://hooks.example.com/medium"
    """Webhook URL for MEDIUM-severity alerts."""

    dingtalk_url: str = "https://oapi.dingtalk.com/robot/send"
    """DingTalk robot webhook (placeholder)."""

    wechat_url: str = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    """WeChat Work webhook (placeholder)."""


@dataclass
class LLMConfig:
    """LLM (OpenAI-compatible) settings for AI-generated outputs.

    When ``enabled`` is False (default) every subsystem uses the existing
    rule-based code path.  When True, select/monitor/report call the
    configured model instead, falling back to the rules on any failure."""

    enabled: bool = False
    """Master switch: False = rule-based code, True = LLM."""

    api_key: str = ""
    """API key for the LLM provider (e.g. Kimi/Moonshot)."""

    base_url: str = "https://api.kimi.com/coding/v1"
    """OpenAI-compatible API base URL (no trailing slash)."""

    model: str = "kimi-k2.6"
    """Model name sent in the chat-completions request."""

    timeout: int = 60
    """HTTP timeout in seconds for LLM calls."""

    temperature: float = 0.6
    """Sampling temperature.  Note: kimi-k2.6 currently only accepts 0.6."""


@dataclass
class SystemConfig:
    """Top-level configuration aggregating every subsystem's settings."""

    watchlist: List[str] = field(default_factory=list)
    """Core watchlist of stock codes (e.g. ['000001', '600519'])."""

    selector: StockSelectorConfig = field(default_factory=StockSelectorConfig)
    """Stock screening parameters."""

    alert: AlertEngineConfig = field(default_factory=AlertEngineConfig)
    """Alerting thresholds and limits."""

    report: DailyReportConfig = field(default_factory=DailyReportConfig)
    """Daily report parameters."""

    notification: NotificationConfig = field(default_factory=NotificationConfig)
    """Notification channel URLs."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    """LLM switch and credentials for AI-generated outputs."""

    timezone: str = "Asia/Shanghai"
    """Default timezone for timestamp generation."""

    data_dir: str = "./data"
    """Root directory for cached market data."""

    log_level: str = "INFO"
    """Application log level."""


# -- Type alias for forward references ----------------------------------------
__all__ = [
    "StockSelectorConfig",
    "AlertEngineConfig",
    "DailyReportConfig",
    "NotificationConfig",
    "LLMConfig",
    "SystemConfig",
]
