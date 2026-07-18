"""Stock screening engine for Chinese A-share market.

Applies a set of mandatory filters to a DataFrame of stock data, then
assigns bonus points based on momentum, volume, and sector signals.
Returns a scored, ranked list of candidates.
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np
import pandas as pd

from config import StockSelectorConfig

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS: List[str] = [
    "code", "name", "close", "ma20", "volume_5d",
    "rsi14", "macd_hist", "pe", "high_20d", "low_20d",
    "change_20d", "sector",
]

OPTIONAL_COLUMNS: List[str] = ["volume_today", "macd_hist_prev", "has_bad_news"]


def _validate_columns(df: pd.DataFrame) -> None:
    """Raise ValueError when any required column is missing.

    The ``has_bad_news`` column is checked separately: if present it is
    used for the bad-news filter; if absent every stock is treated as
    having no bad news (passes the filter)."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")


def _apply_nan_policy(
    mask: pd.Series,
    nan_rows: pd.Series,
    policy: str,
    filter_name: str,
) -> pd.Series:
    """Apply NaN policy to a boolean filter mask.

    Parameters
    ----------
    mask : pd.Series[bool]
        The boolean mask from a pandas comparison.  NaN values evaluate
        to ``False`` (backward-compatible default).
    nan_rows : pd.Series[bool]
        Rows where *any* of the columns involved in this filter were NaN.
    policy : str
        ``"exclude"`` (default) or ``"include"``.
    filter_name : str
        Human-readable name for logging.

    Returns
    -------
    pd.Series[bool]
        Adjusted mask.
    """
    if policy == "include":
        return mask | nan_rows
    # Default: exclude NaN rows (same as raw pandas behavior)
    nan_count = nan_rows.sum()
    if nan_count > 0:
        logger.info(
            "Filter '%s': %d row(s) with NaN excluded", filter_name, nan_count
        )
    return mask


def _apply_filters(df: pd.DataFrame, cfg: StockSelectorConfig) -> pd.DataFrame:
    """Return rows that pass every mandatory screening rule.

    NaN handling follows *cfg.nan_policy* (``"exclude"`` by default).
    The ``has_bad_news`` column is optional; when absent or NaN, stocks
    are treated as having *no* bad news (they pass the filter)."""
    policy = cfg.nan_policy
    initial_count = len(df)

    def _nan_rows(*cols: str) -> pd.Series:
        present = [c for c in cols if c in df.columns]
        if not present:
            return pd.Series(False, index=df.index)
        return df[present].isna().any(axis=1)

    filters: list[tuple[str, pd.Series]] = [
        ("close > ma20", df["close"] > df["ma20"]),
        ("volume_5d > min_volume_5d", df["volume_5d"] > cfg.min_volume_5d),
        ("rsi14 >= rsi_lower", df["rsi14"] >= cfg.rsi_lower),
        ("rsi14 <= rsi_upper", df["rsi14"] <= cfg.rsi_upper),
        ("pe > pe_lower", df["pe"] > cfg.pe_lower),
        ("pe < pe_upper", df["pe"] < cfg.pe_upper),
        ("change_20d <= max_change_20d", df["change_20d"] <= cfg.max_change_20d),
    ]

    # Build composite mask with NaN policy per filter
    mask = pd.Series(True, index=df.index)
    nan_applied: dict[str, int] = {}
    for name, fmask in filters:
        cols_involved = name.split(" ")[0]  # e.g. "close" or "rsi14"
        nans = _nan_rows(cols_involved)
        adjusted = _apply_nan_policy(fmask, nans, policy, name)
        if nans.any():
            nan_applied[name] = int(nans.sum())
        mask &= adjusted

    # --- bad news filter (separate because the column is optional) ----------
    if "has_bad_news" in df.columns:
        bad_news = df["has_bad_news"].fillna(False).astype(bool)
        if df["has_bad_news"].isna().any():
            nan_applied["has_bad_news"] = int(df["has_bad_news"].isna().sum())
        mask &= ~bad_news

    result = df.loc[mask].copy()

    if cfg.log_filter_drops:
        dropped = initial_count - len(result)
        logger.info(
            "Filters passed: %d / %d stocks (dropped: %d)",
            len(result), initial_count, dropped,
        )
        if nan_applied:
            for name, cnt in nan_applied.items():
                logger.info("  NaN-excluded in '%s': %d rows", name, cnt)

    return result


def _score_stocks(
    df: pd.DataFrame,
    sector_rank: Optional[List[str]],
    cfg: StockSelectorConfig,
) -> pd.DataFrame:
    """Add a score column based on bonus-point rules.

    The volume-surge bonus uses *cfg.volume_surge_avg_column* (defaults
    to ``"volume_5d"``) so that callers can supply a column whose units
    match ``volume_today``.  Stocks whose sector is ``"unknown"`` (data
    unavailable) can never earn the sector-hot bonus."""
    score = pd.Series(0, index=df.index, dtype=int)

    if "volume_today" in df.columns:
        avg_col = cfg.volume_surge_avg_column
        if avg_col in df.columns:
            avg_values = df[avg_col].fillna(0)
        else:
            logger.warning("volume_surge_avg_column '%s' not found, skipping volume-surge bonus", avg_col)
            avg_values = pd.Series(0, index=df.index)
        vol_surge = df["volume_today"].fillna(0) > (avg_values * cfg.volume_surge_ratio)
        score += vol_surge.fillna(False).astype(int) * cfg.score_volume_surge

    if "macd_hist_prev" in df.columns:
        macd_expanding = (df["macd_hist"].fillna(0) > 0) & (
            df["macd_hist"] > df["macd_hist_prev"].fillna(0)
        )
        score += macd_expanding.fillna(False).astype(int) * cfg.score_macd_expanding

    breakout = df["close"] > df["high_20d"]
    score += breakout.fillna(False).astype(int) * cfg.score_breakout

    if sector_rank:
        top_sectors = set(sector_rank[: cfg.top_sector_count])
        in_top = df["sector"].isin(top_sectors)
        score += in_top.fillna(False).astype(int) * cfg.score_sector_hot

    df["score"] = score
    return df


def screen_stocks(
    df: pd.DataFrame,
    sector_rank: Optional[List[str]] = None,
    top_n: int = 20,
    config: Optional[StockSelectorConfig] = None,
) -> pd.DataFrame:
    """Screen and rank A-share stocks by mandatory filters and bonus scoring.

    Parameters
    ----------
    df : pd.DataFrame
        Stock data with required columns: code, name, close, ma20,
        volume_5d, rsi14, macd_hist, pe, high_20d, low_20d, change_20d,
        sector, has_bad_news.  Optional columns volume_today and
        macd_hist_prev enable additional scoring.
    sector_rank : list of str, optional
        Sector names ordered by performance, best first.
    top_n : int
        Max stocks to return (default 20).
    config : StockSelectorConfig, optional
        Screening and scoring parameters.

    Returns
    -------
    pd.DataFrame
        Filtered, scored, sorted DataFrame limited to top_n rows.
        Returns an empty DataFrame when nothing passes.
    """
    if config is None:
        config = StockSelectorConfig()

    if df.empty:
        return df.assign(score=pd.Series([], dtype=int)).head(0)

    _validate_columns(df)
    passed = _apply_filters(df, config)

    if passed.empty:
        result = df.head(0).copy()
        result["score"] = pd.Series([], dtype=int)
        return result

    scored = _score_stocks(passed, sector_rank, config)
    scored = scored.sort_values("score", ascending=False)
    return scored.head(top_n).reset_index(drop=True)


# =========================================================================
# Unit tests (pytest-compatible)
# =========================================================================


def _make_base_df() -> pd.DataFrame:
    """Factory for a minimal test DataFrame with all required columns."""
    return pd.DataFrame({
        "code": ["000001", "000002", "000003"],
        "name": ["PingAn", "Vanke", "ZTE"],
        "close": [12.0, 15.0, 30.0],
        "ma20": [11.0, 16.0, 28.0],
        "volume_5d": [200_000_000, 50_000_000, 150_000_000],
        "rsi14": [55, 75, 45],
        "macd_hist": [0.02, -0.01, 0.05],
        "pe": [5.0, 12.0, 30.0],
        "high_20d": [12.5, 15.5, 31.0],
        "low_20d": [10.5, 14.0, 27.0],
        "change_20d": [0.05, 0.10, -0.02],
        "sector": ["Bank", "RealEstate", "Telecom"],
        "has_bad_news": [False, False, False],
    })


class TestScreenStocks:
    """Unit tests for screen_stocks."""

    def test_all_pass_filters(self) -> None:
        df = _make_base_df()
        result = screen_stocks(df)
        codes = result["code"].tolist()
        assert "000001" in codes
        assert "000003" in codes
        assert "000002" not in codes

    def test_rsi_bounds(self) -> None:
        df = _make_base_df()
        result = screen_stocks(df)
        assert "000002" not in result["code"].tolist()

    def test_pe_filter(self) -> None:
        df = _make_base_df()
        df.loc[0, "pe"] = -5.0
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_bad_news_filter(self) -> None:
        df = _make_base_df()
        df.loc[0, "has_bad_news"] = True
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_change_20d_too_high(self) -> None:
        df = _make_base_df()
        df.loc[0, "change_20d"] = 0.60
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_empty_dataframe(self) -> None:
        df = pd.DataFrame()
        result = screen_stocks(df)
        assert result.empty
        assert "score" in result.columns

    def test_no_stocks_pass(self) -> None:
        df = _make_base_df()
        df["close"] = df["ma20"] - 1
        result = screen_stocks(df)
        assert result.empty
        assert "score" in result.columns

    def test_scoring_volume_surge(self) -> None:
        df = _make_base_df()
        df["volume_today"] = [350_000_000, 80_000_000, 120_000_000]
        result = screen_stocks(df)
        row = result[result["code"] == "000001"]
        assert row["score"].iloc[0] >= 2

    def test_scoring_breakout(self) -> None:
        df = _make_base_df()
        df.loc[0, "close"] = 13.0
        result = screen_stocks(df)
        row = result[result["code"] == "000001"]
        assert row["score"].iloc[0] >= 2

    def test_scoring_sector_hot(self) -> None:
        df = _make_base_df()
        sr = ["Bank", "Wine", "Pharma", "Telecom", "Coal",
              "Metals", "Auto", "Appliance", "Defense", "Chemical"]
        result = screen_stocks(df, sector_rank=sr)
        row = result[result["code"] == "000001"]
        assert row["score"].iloc[0] >= 1

    def test_top_n_limit(self) -> None:
        df = _make_base_df()
        result = screen_stocks(df, top_n=1)
        assert len(result) <= 1

    def test_missing_required_column(self) -> None:
        df = _make_base_df().drop(columns=["rsi14"])
        try:
            screen_stocks(df)
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "Missing required columns" in str(exc)

    def test_nan_in_optional_columns(self) -> None:
        df = _make_base_df()
        df["volume_today"] = [np.nan, 80_000_000, 120_000_000]
        df["macd_hist_prev"] = [0.01, np.nan, 0.03]
        result = screen_stocks(df)
        assert not result.empty

    def test_custom_config(self) -> None:
        df = _make_base_df()
        cfg = StockSelectorConfig(rsi_lower=30, rsi_upper=80, pe_upper=200)
        result = screen_stocks(df, config=cfg)
        assert len(result) >= 2

    def test_macd_expanding_score(self) -> None:
        df = _make_base_df()
        df["macd_hist_prev"] = [0.01, -0.02, 0.03]
        result = screen_stocks(df)
        row = result[result["code"] == "000001"]
        assert row["score"].iloc[0] >= 2
        row3 = result[result["code"] == "000003"]
        assert row3["score"].iloc[0] >= 2

    def test_score_sort_descending(self) -> None:
        df = _make_base_df()
        df["volume_today"] = [350_000_000, 80_000_000, 120_000_000]
        result = screen_stocks(df)
        scores = result["score"].tolist()
        assert scores == sorted(scores, reverse=True)


class TestNanPolicy:
    """Tests for NaN handling and configurable NaN policy."""

    def test_nan_pe_excluded_by_default(self) -> None:
        df = _make_base_df()
        df.loc[0, "pe"] = np.nan
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_nan_pe_included_when_policy_is_include(self) -> None:
        df = _make_base_df()
        df.loc[0, "pe"] = np.nan
        cfg = StockSelectorConfig(nan_policy="include")
        result = screen_stocks(df, config=cfg)
        assert "000001" in result["code"].tolist()

    def test_nan_rsi14_excluded_by_default(self) -> None:
        df = _make_base_df()
        df.loc[0, "rsi14"] = np.nan
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_nan_rsi14_included_when_policy_is_include(self) -> None:
        df = _make_base_df()
        df.loc[0, "rsi14"] = np.nan
        cfg = StockSelectorConfig(nan_policy="include")
        result = screen_stocks(df, config=cfg)
        assert "000001" in result["code"].tolist()

    def test_multiple_nan_columns(self) -> None:
        df = _make_base_df()
        df.loc[0, "pe"] = np.nan
        df.loc[0, "rsi14"] = np.nan
        # exclude policy: row dropped
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()

    def test_log_filter_drops_flag(self) -> None:
        df = _make_base_df()
        df.loc[0, "pe"] = -5.0
        cfg = StockSelectorConfig(log_filter_drops=True)
        result = screen_stocks(df, config=cfg)  # should not crash
        assert "000001" not in result["code"].tolist()


class TestOptionalHasBadNews:
    """Tests for the has_bad_news column being optional."""

    def test_missing_has_bad_news_column(self) -> None:
        df = _make_base_df().drop(columns=["has_bad_news"])
        result = screen_stocks(df)
        assert "000001" in result["code"].tolist()
        assert "000003" in result["code"].tolist()

    def test_nan_in_has_bad_news_treated_as_no_news(self) -> None:
        df = _make_base_df()
        df["has_bad_news"] = df["has_bad_news"].astype(object)
        df.loc[0, "has_bad_news"] = np.nan
        result = screen_stocks(df)
        codes = result["code"].tolist()
        assert "000001" in codes  # NaN treated as False (no bad news)

    def test_has_bad_news_true_still_filters(self) -> None:
        df = _make_base_df()
        df.loc[0, "has_bad_news"] = True
        result = screen_stocks(df)
        assert "000001" not in result["code"].tolist()


class TestVolumeSurgeUnitAware:
    """Tests for unit-aware volume surge comparison."""

    def test_custom_avg_column(self) -> None:
        df = _make_base_df()
        df["volume_today"] = [350_000_000, 80_000_000, 120_000_000]
        df["volume_5d_shares"] = [100_000_000, 100_000_000, 100_000_000]
        cfg = StockSelectorConfig(volume_surge_avg_column="volume_5d_shares")
        result = screen_stocks(df, config=cfg)
        row = result[result["code"] == "000001"]
        assert row["score"].iloc[0] >= 2

    def test_unknown_avg_column_warns_but_continues(self) -> None:
        df = _make_base_df()
        df["volume_today"] = [350_000_000, 80_000_000, 120_000_000]
        cfg = StockSelectorConfig(volume_surge_avg_column="no_such_column")
        result = screen_stocks(df, config=cfg)  # should not crash
        assert not result.empty


class TestSectorUnknown:
    """Tests demonstrating sector == 'unknown' never earns sector bonus."""

    def test_unknown_sector_no_bonus(self) -> None:
        df = _make_base_df()
        df["sector"] = ["unknown", "unknown", "unknown"]
        sr = ["Bank", "Wine", "Pharma", "Telecom", "Coal",
              "Metals", "Auto", "Appliance", "Defense", "Chemical"]
        result = screen_stocks(df, sector_rank=sr)
        # All pass filters, but sector bonus should NOT be applied
        assert all(s == 0 for s in result["score"]), (
            f"Sector 'unknown' should not earn sector bonus, got scores: {result['score'].tolist()}"
        )

    def test_unknown_vs_named_sector(self) -> None:
        df = _make_base_df()
        # Use ZTE (000003) as the unknown-sector representative — it already passes
        df.loc[0, "sector"] = "Bank"
        df.loc[2, "sector"] = "unknown"
        sr = ["Bank", "Wine", "Pharma", "Telecom", "Coal",
              "Metals", "Auto", "Appliance", "Defense", "Chemical"]
        result = screen_stocks(df, sector_rank=sr)
        bank_row = result[result["code"] == "000001"]
        unk_row = result[result["code"] == "000003"]
        assert len(unk_row) > 0, "000003 (ZTE) should survive as unknown"
        assert len(bank_row) > 0, "Bank row should survive"
        assert unk_row["score"].iloc[0] <= bank_row["score"].iloc[0], (
            f"Unknown sector score ({unk_row['score'].iloc[0]}) "
            f"should not exceed Bank score ({bank_row['score'].iloc[0]})"
        )
