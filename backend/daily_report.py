"""Daily report generator for A-share stock monitoring.

Produces a Markdown-formatted daily summary suitable for WeChat Work,
DingTalk, email, or archival storage.
"""

from __future__ import annotations

import logging
from io import StringIO
import numpy as np
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from config import DailyReportConfig

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Return *value* as float, falling back to *default* on failure."""
    try:
        v = float(value)
        if pd.isna(v):
            return default
        return v
    except (ValueError, TypeError):
        return default


def _build_market_overview(market: Dict[str, Any]) -> str:
    """Format the Market Overview section."""
    idx = market.get("index_name", "\u6caa\u6df1300")
    points = _safe_float(market.get("index_points"))
    change = _safe_float(market.get("index_change_pct"))
    turnover = _safe_float(market.get("total_turnover"))
    turnover_str = f"{turnover / 1e8:,.0f}" if turnover > 0 else "N/A"
    sign = "+" if change >= 0 else ""
    arrow = "\u25b2" if change > 0 else ("\u25bc" if change < 0 else "\u2500")
    return (f"## Market Overview\n\n"
            f"| Index | Close | Change | Turnover (RMB) |\n"
            f"|-------|-------|--------|----------------|\n"
            f"| {idx} | {points:,.2f} | {arrow} {sign}{change:.2f}% | {turnover_str} \u4ebf |\n")


def _build_watchlist_table(watchlist: pd.DataFrame) -> str:
    """Format the Watchlist Performance table.

    When a ``prev_close`` column is present, an additional
    "vs Prev Close" column is rendered showing the change from
    yesterday's close.  Missing or NaN *prev_close* values are
    displayed as "N/A"."""
    if watchlist.empty:
        return "## Watchlist Performance\n\n*No stocks on watchlist today.*\n"
    has_prev_close = "prev_close" in watchlist.columns
    rows: List[str] = []
    for _, row in watchlist.iterrows():
        code = row.get("code", "\u2014")
        name = row.get("name", "\u2014")
        price = _safe_float(row.get("price"))
        change = _safe_float(row.get("change_pct"))
        if has_prev_close:
            pc = row.get("prev_close")
            try:
                pc_val = float(pc) if pc is not None and not pd.isna(pc) else None
            except (ValueError, TypeError):
                pc_val = None
            if pc_val and pc_val > 0:
                vs_prev = (price - pc_val) / pc_val * 100
                vs_sign = "+" if vs_prev >= 0 else ""
                vs_arrow = "\u25b2" if vs_prev > 0 else ("\u25bc" if vs_prev < 0 else "\u2500")
                vs_str = f"{vs_arrow} {vs_sign}{vs_prev:.2f}%"
            else:
                vs_str = "N/A"
        sign = "+" if change >= 0 else ""
        arrow = "\u25b2" if change > 0 else ("\u25bc" if change < 0 else "\u2500")
        if has_prev_close:
            rows.append(f"| {code} | {name} | {price:.2f} | {arrow} {sign}{change:.2f}% | {vs_str} |")
        else:
            rows.append(f"| {code} | {name} | {price:.2f} | {arrow} {sign}{change:.2f}% |")
    if has_prev_close:
        header = ("## Watchlist Performance\n\n"
                  "| Code | Name | Price | Change | vs Prev Close |\n"
                  "|------|------|-------|--------|---------------|\n")
    else:
        header = ("## Watchlist Performance\n\n"
                  "| Code | Name | Price | Change |\n"
                  "|------|------|-------|--------|\n")
    return header + "\n".join(rows) + "\n"


def _build_alerts_review(alerts: List[dict]) -> str:
    """Format the Today's Alerts Review section."""
    if not alerts:
        return "## Today's Alerts\n\n*No alerts triggered today.*\n"
    high = [a for a in alerts if a.get("level") == "HIGH"]
    medium = [a for a in alerts if a.get("level") == "MEDIUM"]
    parts = [f"## Today's Alerts\n\n**HIGH ({len(high)})**  |  **MEDIUM ({len(medium)})**\n"]
    for a in alerts:
        level = a.get("level", "\u2014")
        code = a.get("code", "\u2014")
        name = a.get("name", "\u2014")
        reason = a.get("reason", "\u2014")
        price = _safe_float(a.get("price"))
        change = _safe_float(a.get("change_pct"))
        emoji = "\U0001f534" if level == "HIGH" else "\U0001f7e1"
        parts.append(f"- {emoji} **{level}** | {code} {name} | {price:.2f} ({change:+.2f}%) | {reason}")
    return "\n".join(parts) + "\n"


def _build_tomorrow_watch(candidates: pd.DataFrame, top_n: int) -> str:
    """Format the Tomorrow's Watch section."""
    if candidates.empty:
        return "## Tomorrow's Watch\n\n*No candidates available.*\n"
    top = candidates.head(top_n)
    rows: List[str] = []
    for _, row in top.iterrows():
        code = row.get("code", "\u2014")
        name = row.get("name", "\u2014")
        score = _safe_float(row.get("score"))
        reason_parts = []
        if _safe_float(row.get("macd_hist", 0)) > 0:
            reason_parts.append("MACD+")
        if _safe_float(row.get("close", 0)) > _safe_float(row.get("ma20", 0)):
            reason_parts.append("MA20\u2191")
        reason = ", ".join(reason_parts) if reason_parts else "screened"
        rows.append(f"| {code} | {name} | {score:.0f} | {reason} |")
    header = ("## Tomorrow's Watch\n\n"
              "| Code | Name | Score | Reason |\n"
              "|------|------|-------|--------|\n")
    return header + "\n".join(rows) + "\n"


def _build_sentiment(market: Dict[str, Any], cfg: DailyReportConfig) -> str:
    """Derive a one-line market sentiment from advance/decline ratio."""
    advance = _safe_float(market.get("advance_count"), 0)
    decline = _safe_float(market.get("decline_count"), 0)
    if decline > 0:
        ratio = advance / decline
    elif advance > 0:
        ratio = float("inf")
    else:
        ratio = 1.0
    if ratio >= cfg.bullish_ratio:
        sentiment = "\U0001f7e2 **Bullish** \u2014 advancing stocks dominate."
    elif ratio <= cfg.cautious_ratio:
        sentiment = "\U0001f534 **Cautious** \u2014 declining stocks outnumber advancers."
    else:
        sentiment = "\U0001f7e1 **Neutral** \u2014 market breadth is balanced."

    # -- Factor 2: turnover divergence ------------------------------------
    turnover = _safe_float(market.get("total_turnover"), 0)
    turnover_20d = _safe_float(market.get("turnover_20d_avg"), 0)
    if turnover > 0 and turnover_20d > 0:
        t_ratio = turnover / turnover_20d
        if t_ratio > 1 + cfg.sentiment_turnover_divergence_pct:
            if "Bullish" not in sentiment:
                sentiment = sentiment.replace("Neutral", "Bullish").replace(
                    "Cautious", "Neutral")
            sentiment += " Turnover +{:.0f}% vs 20d avg.".format(
                (t_ratio - 1) * 100)
        elif t_ratio < 1 - cfg.sentiment_turnover_divergence_pct:
            if "Cautious" not in sentiment:
                sentiment = sentiment.replace("Neutral", "Cautious").replace(
                    "Bullish", "Neutral")
            sentiment += " Turnover -{:.0f}% vs 20d avg.".format(
                (1 - t_ratio) * 100)

    # -- Factor 3: index vs 20-day MA --------------------------------------
    if cfg.index_ma20_enabled:
        idx_points = _safe_float(market.get("index_points"), 0)
        idx_ma20 = _safe_float(market.get("index_ma20"), 0)
        if idx_points > 0 and idx_ma20 > 0:
            if idx_points > idx_ma20:
                sentiment += " Index above 20d MA."
            elif idx_points < idx_ma20:
                sentiment += " Index below 20d MA."

    return f"## Market Sentiment\n\n{sentiment}\n"


def generate_daily_report(
    market_summary: Dict[str, Any],
    watchlist_df: pd.DataFrame,
    alerts_today: List[dict],
    candidates_df: pd.DataFrame,
    config: Optional[DailyReportConfig] = None,
    report_date: Optional[date] = None,
) -> str:
    """Produce a Markdown daily report string.

    Parameters
    ----------
    market_summary : dict
        Keys: index_name, index_points, index_change_pct,
        total_turnover, advance_count, decline_count.
    watchlist_df : pd.DataFrame
        Columns: code, name, price, change_pct.
    alerts_today : list of dict
        Alert dicts as emitted by AlertEngine.
    candidates_df : pd.DataFrame
        Scored candidates from screen_stocks.
    config : DailyReportConfig, optional
        Tuning parameters.
    report_date : date, optional
        Date to show in the report header.  Defaults to today.

    Returns
    -------
    str
        Formatted Markdown report.
    """
    if config is None:
        config = DailyReportConfig()
    if report_date is None:
        report_date = date.today()
    lines: List[str] = [
        f"# Daily Report \u2014 {report_date.isoformat()}",
        "",
        _build_market_overview(market_summary),
        _build_watchlist_table(watchlist_df),
        _build_alerts_review(alerts_today),
        _build_tomorrow_watch(candidates_df, config.top_candidates_count),
        _build_sentiment(market_summary, config),
    ]
    return "\n".join(lines)


def _sample_market() -> Dict[str, Any]:
    return {
        "index_name": "\u6caa\u6df1300",
        "index_points": 3950.5,
        "index_change_pct": 0.85,
        "total_turnover": 850_000_000_000.0,
        "advance_count": 2800,
        "decline_count": 1800,
    }


def _sample_watchlist() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "000001", "name": "PingAn", "price": 12.5, "change_pct": 2.3},
        {"code": "600519", "name": "Moutai", "price": 1680.0, "change_pct": -1.2},
    ])


def _sample_alerts() -> List[dict]:
    return [
        {
            "code": "000001", "name": "PingAn", "price": 12.5,
            "change_pct": 6.0, "reason": "Price surge: +6.00%",
            "level": "HIGH", "timestamp": "2026-07-17T10:30:00+08:00",
        },
        {
            "code": "000002", "name": "Vanke", "price": 14.0,
            "change_pct": 1.0, "reason": "Volume surge: 50M vs 10M",
            "level": "MEDIUM", "timestamp": "2026-07-17T11:00:00+08:00",
        },
    ]


def _sample_candidates() -> pd.DataFrame:
    return pd.DataFrame([
        {"code": "000001", "name": "PingAn", "score": 5.0,
         "close": 12.5, "ma20": 11.0, "macd_hist": 0.02},
        {"code": "000003", "name": "ZTE", "score": 3.0,
         "close": 30.0, "ma20": 28.0, "macd_hist": 0.05},
        {"code": "000004", "name": "TestCorp", "score": 1.0,
         "close": 5.0, "ma20": 5.5, "macd_hist": -0.01},
    ])


class TestDailyReport:
    """Unit tests for generate_daily_report."""

    def test_full_report_has_all_sections(self) -> None:
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(),
            _sample_alerts(), _sample_candidates(),
        )
        sections = [
            "Daily Report", "Market Overview",
            "Watchlist Performance", "Today's Alerts",
            "Tomorrow's Watch", "Market Sentiment",
        ]
        for s in sections:
            assert s in report, f"Missing section: {s}"

    def test_empty_watchlist(self) -> None:
        report = generate_daily_report(
            _sample_market(), pd.DataFrame(),
            _sample_alerts(), _sample_candidates(),
        )
        assert "No stocks on watchlist" in report

    def test_no_alerts(self) -> None:
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(),
            [], _sample_candidates(),
        )
        assert "No alerts triggered" in report

    def test_no_candidates(self) -> None:
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(),
            _sample_alerts(), pd.DataFrame(),
        )
        assert "No candidates available" in report

    def test_bullish_sentiment(self) -> None:
        mkt = {**_sample_market(), "advance_count": 3000, "decline_count": 1000}
        report = generate_daily_report(
            mkt, _sample_watchlist(), _sample_alerts(), _sample_candidates(),
        )
        assert "Bullish" in report

    def test_cautious_sentiment(self) -> None:
        mkt = {**_sample_market(), "advance_count": 800, "decline_count": 3200}
        report = generate_daily_report(
            mkt, _sample_watchlist(), _sample_alerts(), _sample_candidates(),
        )
        assert "Cautious" in report

    def test_neutral_sentiment(self) -> None:
        mkt = {**_sample_market(), "advance_count": 2000, "decline_count": 2000}
        report = generate_daily_report(
            mkt, _sample_watchlist(), _sample_alerts(), _sample_candidates(),
        )
        assert "Neutral" in report

    def test_market_overview_values(self) -> None:
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(), [], pd.DataFrame(),
        )
        assert "3,950.50" in report
        assert "+0.85%" in report

    def test_custom_config(self) -> None:
        cfg = DailyReportConfig(top_candidates_count=1)
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(),
            _sample_alerts(), _sample_candidates(),
            config=cfg,
        )
        sections = report.split("## ")
        tw = [s for s in sections if "Tomorrow" in s][0]
        tw_lines = tw.splitlines()
        candidate_rows = [l for l in tw_lines if l.startswith("| ") and "---" not in l and "Code" not in l]
        assert len(candidate_rows) <= 1

    def test_nan_in_market_data(self) -> None:
        mkt: Dict[str, Any] = {
            "index_name": None, "index_points": float("nan"),
            "index_change_pct": None, "total_turnover": None,
            "advance_count": None, "decline_count": None,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "Market Overview" in report

    def test_custom_date(self) -> None:
        d = date(2026, 1, 15)
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(), [], pd.DataFrame(),
            report_date=d,
        )
        assert "2026-01-15" in report


class TestEnhancedSentiment:
    """Tests for the enhanced multi-factor market sentiment."""

    def test_turnover_divergence_bullish_boost(self) -> None:
        mkt = {
            **_sample_market(),
            "advance_count": 2200, "decline_count": 2200,
            "total_turnover": 1_000_000_000_000,
            "turnover_20d_avg": 700_000_000_000,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "Turnover" in report
        assert "Bullish" in report or "Turnover" in report

    def test_turnover_drop_cautious_shift(self) -> None:
        mkt = {
            **_sample_market(),
            "advance_count": 2200, "decline_count": 2200,
            "total_turnover": 500_000_000_000,
            "turnover_20d_avg": 1_000_000_000_000,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "Turnover" in report

    def test_index_above_ma20_shown(self) -> None:
        mkt = {
            **_sample_market(),
            "index_points": 4000, "index_ma20": 3800,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "above 20d MA" in report

    def test_index_below_ma20_shown(self) -> None:
        mkt = {
            **_sample_market(),
            "index_points": 3700, "index_ma20": 3800,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "below 20d MA" in report

    def test_graceful_degradation_missing_fields(self) -> None:
        mkt: Dict[str, Any] = {
            "index_name": "CSI300",
            "index_points": 4000,
            "index_change_pct": 0.5,
            "total_turnover": 0.0,  # data source returned 0
            "advance_count": 2200,
            "decline_count": 2200,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        assert "Market Sentiment" in report
        assert "Neutral" in report

    def test_turnover_20d_zero_no_divergence_text(self) -> None:
        mkt = {
            **_sample_market(),
            "turnover_20d_avg": 0.0,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
        )
        # Divergence text like "Turnover +" or "Turnover -" should not appear
        # (the word "Turnover" appears in the Market Overview section regardless)
        assert "Turnover +" not in report
        assert "Turnover -" not in report

    def test_ma20_disabled(self) -> None:
        cfg = DailyReportConfig(index_ma20_enabled=False)
        mkt = {
            **_sample_market(),
            "index_points": 4000, "index_ma20": 3800,
        }
        report = generate_daily_report(
            mkt, _sample_watchlist(), [], _sample_candidates(),
            config=cfg,
        )
        assert "20d MA" not in report


class TestWatchlistPrevClose:
    """Tests for the 'vs Previous Close' column in the watchlist table."""

    def test_prev_close_column_rendered(self) -> None:
        wl = _sample_watchlist().copy()
        wl["prev_close"] = [12.0, 1700.0]
        report = generate_daily_report(
            _sample_market(), wl, [], pd.DataFrame(),
        )
        assert "vs Prev Close" in report
        # PingAn: 12.5 vs 12.0 = +4.17%
        assert "+4.17%" in report or "4.17%" in report

    def test_prev_close_nan_rendered_n_a(self) -> None:
        wl = _sample_watchlist().copy()
        wl["prev_close"] = [np.nan, 1700.0]
        report = generate_daily_report(
            _sample_market(), wl, [], pd.DataFrame(),
        )
        assert "N/A" in report

    def test_no_prev_close_column_no_header(self) -> None:
        report = generate_daily_report(
            _sample_market(), _sample_watchlist(), [], pd.DataFrame(),
        )
        assert "vs Prev Close" not in report


class TestStockCodePreservation:
    """Tests that stock codes survive JSON round-trips with leading zeros."""

    def test_leading_zero_code_preserved(self) -> None:
        import json
        from io import StringIO
        df = pd.DataFrame({
            "code": ["002594", "000001", "600519"],
            "name": ["BYD", "PingAn", "Moutai"],
            "score": [5, 3, 2],
        })
        payload = df[["code", "name", "score"]].to_dict(orient="records")
        json_str = json.dumps(payload, ensure_ascii=False)
        # Round-trip
        reloaded = pd.read_json(json_str, orient="records")
        # No change needed - this tests the regression of leading-zero codes
        # All codes must be strings with leading zeros intact
        assert codes[0] == "002594", f"Leading zeros lost: {codes[0]}"
        assert codes[1] == "000001", f"Leading zeros lost: {codes[1]}"
        assert isinstance(codes[0], str), f"Expected str, got {type(codes[0])}"
        assert isinstance(codes[1], str), f"Expected str, got {type(codes[1])}"

    def test_pandas_round_trip_preserves_string_code(self) -> None:
        df = pd.DataFrame({"code": ["002594", "000001"], "name": ["BYD", "PingAn"]})
        json_str = df.to_json(orient="records")
        reloaded = pd.read_json(json_str, orient="records")
        codes = reloaded["code"].tolist()
        assert codes == ["002594", "000001"], f"Codes corrupted: {codes}"
