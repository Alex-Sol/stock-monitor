"""Real-time alert engine for A-share stock monitoring.

Scans incoming stock snapshots against configurable thresholds and
emits alerts with severity levels.  Deduplication is delegated to an
``AlertCache`` implementation so that counts survive process restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from alert_cache import AlertCache, InMemoryAlertCache
from config import AlertEngineConfig

logger = logging.getLogger(__name__)


class AlertEngine:
    """Rule-based alert emitter with per-stock daily deduplication.

    Parameters
    ----------
    config : AlertEngineConfig, optional
        Thresholds and limits.  Uses defaults when ``None``.
    timezone : str, optional
        IANA timezone name for timestamp generation (default ``Asia/Shanghai``).
    cache : AlertCache, optional
        Alert deduplication cache.  Defaults to ``InMemoryAlertCache``
        for backward compatibility.  Use ``JsonFileAlertCache`` for
        persistence across restarts.
    """

    def __init__(
        self,
        config: Optional[AlertEngineConfig] = None,
        timezone: str = "Asia/Shanghai",
        cache: Optional[AlertCache] = None,
    ) -> None:
        self.config = config or AlertEngineConfig()
        self._tz = timezone
        self.cache: AlertCache = cache or InMemoryAlertCache()
        self.alerted_stocks: Dict[str, int] = {}  # deprecated, kept for backward compat

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_alerts(
        self,
        df: pd.DataFrame,
        current_time: Optional[datetime] = None,
    ) -> List[dict]:
        """Evaluate every row in *df* and return triggered alerts.

        Parameters
        ----------
        df : pd.DataFrame
            Required columns: ``code``, ``name``, ``price``,
            ``change_pct``, ``volume``, ``volume_5d_avg``,
            ``high_20d``, ``low_20d``.
        current_time : datetime, optional
            Timestamp to stamp on emitted alerts.  Uses ``now(tz)``
            when ``None``.

        Returns
        -------
        list of dict
            Each dict has keys ``code``, ``name``, ``price``,
            ``change_pct``, ``reason``, ``level``, ``timestamp``.
        """
        if df.empty:
            return []

        self._validate_input(df)

        if current_time is None:
            try:
                current_time = datetime.now(ZoneInfo(self._tz))
            except Exception:
                logger.warning("Invalid timezone '%s', falling back to UTC", self._tz)
                current_time = datetime.now(timezone.utc)

        alerts: List[dict] = []
        max_per_stock = self.config.max_alerts_per_stock_per_day
        for _, row in df.iterrows():
            code = row["code"]
            if self.cache.get_count(code) >= max_per_stock:
                continue

            row_alerts = self._evaluate_row(row, current_time, df)
            for alert in row_alerts:
                if self.cache.get_count(code) >= max_per_stock:
                    break
                alerts.append(alert)
                new_count = self.cache.increment(code, max_per_stock)
                self.alerted_stocks[code] = new_count  # backward compat

        return alerts

    def reset_daily(self) -> None:
        """Clear per-stock alert counters for the current trading day."""
        self.cache.reset_daily()
        self.alerted_stocks.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_input(df: pd.DataFrame) -> None:
        required = ["code", "name", "price", "change_pct",
                     "volume", "volume_5d_avg", "high_20d", "low_20d"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise ValueError(f"Missing required columns: {missing}")

    def _evaluate_row(
        self, row: pd.Series, current_time: datetime, df: pd.DataFrame,
    ) -> List[dict]:
        cfg = self.config
        code = row["code"]
        name = row["name"]
        price = float(row["price"])
        change_pct = float(row["change_pct"])
        volume = float(row.get("volume", np.nan))
        vol_avg = float(row.get("volume_5d_avg", np.nan))
        high_20d = float(row.get("high_20d", np.nan))
        low_20d = float(row.get("low_20d", np.nan))

        alerts: List[dict] = []

        # --- High: sudden price move (+/- > threshold %) -----------------
        if not np.isnan(change_pct) and abs(change_pct) > cfg.change_pct_threshold:
            direction = "surge" if change_pct > 0 else "plunge"
            alerts.append({
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "reason": f"Price {direction}: {change_pct:+.2f}%",
                "level": "HIGH",
                "timestamp": current_time.isoformat(),
            })

        # --- Medium: volume > N x average ---------------------------------
        if (not np.isnan(volume) and not np.isnan(vol_avg)
                and vol_avg > 0
                and volume > vol_avg * cfg.volume_surge_ratio
                and volume >= cfg.min_volume_absolute):
            alerts.append({
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "reason": f"Volume surge: {volume:,.0f} vs avg {vol_avg:,.0f}",
                "level": "MEDIUM",
                "timestamp": current_time.isoformat(),
            })

        # --- High: breakout above 20-day high ------------------------------
        if not np.isnan(high_20d) and price > high_20d:
            alerts.append({
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "reason": f"Breakout above 20d high ({high_20d:.2f})",
                "level": "HIGH",
                "timestamp": current_time.isoformat(),
            })

        # --- High: breakdown below 20-day low ------------------------------
        if not np.isnan(low_20d) and price < low_20d:
            alerts.append({
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "reason": f"Breakdown below 20d low ({low_20d:.2f})",
                "level": "HIGH",
                "timestamp": current_time.isoformat(),
            })

        # --- Medium: RSI divergence -----------------------------------------
        if cfg.rsi_divergence_enabled:
            rsi_alerts = self._check_rsi_divergence(row, df, code, name,
                                                    price, change_pct,
                                                    current_time)
            alerts.extend(rsi_alerts)

        return alerts


    def _check_rsi_divergence(
        self,
        row: pd.Series,
        df: pd.DataFrame,
        code: str,
        name: str,
        price: float,
        change_pct: float,
        current_time: datetime,
    ) -> List[dict]:
        optional_cols = ["rsi14", "rsi14_prev", "high_today", "high_prev"]
        if not all(c in df.columns for c in optional_cols):
            return []
        rsi = float(row.get("rsi14", np.nan))
        rsi_prev = float(row.get("rsi14_prev", np.nan))
        high = float(row.get("high_today", np.nan))
        high_prev = float(row.get("high_prev", np.nan))
        if any(np.isnan(v) for v in [rsi, rsi_prev, high, high_prev]):
            return []
        alerts: List[dict] = []
        if high > high_prev and rsi < rsi_prev:
            alerts.append({
                "code": code, "name": name, "price": price,
                "change_pct": change_pct,
                "reason": (f"RSI bearish divergence: price HH {high:.2f} > "
                           f"{high_prev:.2f}, RSI {rsi:.1f} < {rsi_prev:.1f}"),
                "level": "MEDIUM",
                "timestamp": current_time.isoformat(),
            })
        if high < high_prev and rsi > rsi_prev:
            alerts.append({
                "code": code, "name": name, "price": price,
                "change_pct": change_pct,
                "reason": (f"RSI bullish divergence: price LH {high:.2f} < "
                           f"{high_prev:.2f}, RSI {rsi:.1f} > {rsi_prev:.1f}"),
                "level": "MEDIUM",
                "timestamp": current_time.isoformat(),
            })
        return alerts


# =========================================================================
# Unit tests (pytest-compatible)
# =========================================================================


def _make_alert_df() -> pd.DataFrame:
    """Factory for a minimal alert-test DataFrame."""
    return pd.DataFrame([
        {
            "code": "000001", "name": "PingAn", "price": 12.5,
            "change_pct": 6.0, "volume": 50_000_000,
            "volume_5d_avg": 10_000_000, "high_20d": 12.0,
            "low_20d": 10.0,
        },
        {
            "code": "000002", "name": "Vanke", "price": 14.0,
            "change_pct": 1.0, "volume": 20_000_000,
            "volume_5d_avg": 30_000_000, "high_20d": 15.0,
            "low_20d": 13.0,
        },
        {
            "code": "000003", "name": "ZTE", "price": 9.5,
            "change_pct": -3.0, "volume": 10_000_000,
            "volume_5d_avg": 20_000_000, "high_20d": 11.0,
            "low_20d": 10.0,
        },
    ])


class TestAlertEngine:
    """Unit tests for AlertEngine."""

    def test_price_surge_high_alert(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        high = [a for a in alerts if a["level"] == "HIGH" and "surge" in a["reason"]]
        assert len(high) >= 1
        assert high[0]["code"] == "000001"

    def test_breakout_alert(self) -> None:
        engine = AlertEngine()
        df = pd.DataFrame([{
            "code": "000999", "name": "TestBrk", "price": 20.0,
            "change_pct": 2.0, "volume": 10_000_000,
            "volume_5d_avg": 10_000_000, "high_20d": 19.0,
            "low_20d": 15.0,
        }])
        alerts = engine.process_alerts(df)
        breakout = [a for a in alerts if "Breakout" in a["reason"]]
        assert len(breakout) == 1
        assert breakout[0]["code"] == "000999"

    def test_breakdown_alert(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        breakdown = [a for a in alerts if "Breakdown" in a["reason"]]
        assert len(breakdown) >= 1
        assert breakdown[0]["code"] == "000003"

    def test_volume_surge_medium_alert(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        medium = [a for a in alerts if a["level"] == "MEDIUM"]
        assert len(medium) >= 1
        assert medium[0]["code"] == "000001"

    def test_empty_dataframe(self) -> None:
        engine = AlertEngine()
        alerts = engine.process_alerts(pd.DataFrame())
        assert alerts == []

    def test_deduplication(self) -> None:
        engine = AlertEngine()
        engine.config = AlertEngineConfig(max_alerts_per_stock_per_day=2)
        df = _make_alert_df()
        # 000001 triggers multiple rules; should cap at 2
        for stock_df in [df[df["code"] == "000001"]] * 5:
            engine.process_alerts(stock_df)
        assert engine.alerted_stocks.get("000001", 0) <= 2

    def test_reset_daily(self) -> None:
        engine = AlertEngine()
        engine.process_alerts(_make_alert_df())
        assert len(engine.alerted_stocks) > 0
        engine.reset_daily()
        assert len(engine.alerted_stocks) == 0

    def test_normal_stock_no_alert(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        alerted_codes = {a["code"] for a in alerts}
        assert "000002" not in alerted_codes

    def test_missing_column_raises(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df().drop(columns=["change_pct"])
        try:
            engine.process_alerts(df)
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "Missing required columns" in str(exc)

    def test_alert_structure(self) -> None:
        engine = AlertEngine()
        alerts = engine.process_alerts(_make_alert_df())
        for a in alerts:
            assert set(a.keys()) == {
                "code", "name", "price", "change_pct",
                "reason", "level", "timestamp",
            }
            assert a["level"] in ("HIGH", "MEDIUM")

    def test_custom_config(self) -> None:
        cfg = AlertEngineConfig(change_pct_threshold=10.0)
        engine = AlertEngine(config=cfg)
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        surge = [a for a in alerts if "Price surge" in a.get("reason", "")]
        assert len(surge) == 0  # 6% < 10%

    def test_nan_safety(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        df.loc[0, "change_pct"] = np.nan
        df.loc[0, "volume"] = np.nan
        df.loc[0, "high_20d"] = np.nan
        df.loc[0, "low_20d"] = np.nan
        alerts = engine.process_alerts(df)
        # 000001 should not crash and should still trigger breakout/breakdown on other rows
        assert isinstance(alerts, list)


class TestAlertCacheIntegration:
    """Tests that verify AlertEngine correctly delegates to AlertCache."""

    def test_cache_counts_preserved_across_calls(self) -> None:
        from alert_cache import InMemoryAlertCache
        cache = InMemoryAlertCache()
        engine = AlertEngine(cache=cache, config=AlertEngineConfig(max_alerts_per_stock_per_day=2))
        df = _make_alert_df()
        engine.process_alerts(df)
        assert cache.get_count("000001") >= 1

    def test_cache_respects_max_per_day(self) -> None:
        from alert_cache import InMemoryAlertCache
        cache = InMemoryAlertCache()
        engine = AlertEngine(cache=cache, config=AlertEngineConfig(max_alerts_per_stock_per_day=1))
        df = _make_alert_df()
        # 000001 triggers both price surge and volume surge
        alerts = engine.process_alerts(df)
        code_alerts = [a for a in alerts if a["code"] == "000001"]
        assert len(code_alerts) <= 1

    def test_reset_daily_clears_cache(self) -> None:
        from alert_cache import InMemoryAlertCache
        cache = InMemoryAlertCache()
        engine = AlertEngine(cache=cache)
        engine.process_alerts(_make_alert_df())
        engine.reset_daily()
        assert cache.get_count("000001") == 0
        assert len(engine.alerted_stocks) == 0

    def test_json_cache_persistence(self, tmp_path) -> None:
        from alert_cache import JsonFileAlertCache
        from datetime import date
        fp = tmp_path / "alert_cache.json"
        day = date(2026, 7, 18)
        cache = JsonFileAlertCache(filepath=fp, trading_day=day)
        engine = AlertEngine(cache=cache, config=AlertEngineConfig(max_alerts_per_stock_per_day=3))
        engine.process_alerts(_make_alert_df())
        # Simulate restart
        cache2 = JsonFileAlertCache(filepath=fp, trading_day=day)
        assert cache2.get_count("000001") >= 1


class TestTimezoneCorrectness:
    """Tests that the configured timezone is actually applied."""

    def test_configured_timezone_used(self) -> None:
        engine = AlertEngine(timezone="Asia/Tokyo")
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        ts = alerts[0]["timestamp"]
        assert "+09:00" in ts or "JST" in ts or "Asia/Tokyo" in ts, \
            f"Expected Tokyo timezone in timestamp, got: {ts}"

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        engine = AlertEngine(timezone="Mars/Olympus")
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        ts = alerts[0]["timestamp"]
        assert "+00:00" in ts or "UTC" in ts or "Z" in ts, \
            f"Expected UTC fallback in timestamp, got: {ts}"

    def test_explicit_current_time_preserved(self) -> None:
        from datetime import datetime, timezone, timedelta
        engine = AlertEngine(timezone="Asia/Shanghai")
        custom_time = datetime(2026, 7, 18, 14, 30, 0, tzinfo=timezone(timedelta(hours=8)))
        df = _make_alert_df()
        alerts = engine.process_alerts(df, current_time=custom_time)
        assert "14:30:00" in alerts[0]["timestamp"]


class TestMinVolumeAbsolute:
    """Tests that absolute volume floor is enforced."""

    def test_below_floor_no_volume_alert(self) -> None:
        cfg = AlertEngineConfig(min_volume_absolute=100_000_000)
        engine = AlertEngine(config=cfg)
        df = pd.DataFrame([{
            "code": "000888", "name": "LowLiq", "price": 10.0,
            "change_pct": 1.0, "volume": 9_000_000,
            "volume_5d_avg": 1_000_000, "high_20d": 15.0,
            "low_20d": 5.0,
        }])
        # volume is 9x avg but below 100M floor
        alerts = engine.process_alerts(df)
        volume_alerts = [a for a in alerts if "Volume surge" in a["reason"]]
        assert len(volume_alerts) == 0

    def test_above_floor_volume_alert_fires(self) -> None:
        cfg = AlertEngineConfig(min_volume_absolute=10_000_000)
        engine = AlertEngine(config=cfg)
        df = _make_alert_df()
        alerts = engine.process_alerts(df)
        volume_alerts = [a for a in alerts if "Volume surge" in a["reason"]]
        # 000001: volume=50M, avg=10M, floor=10M -> should fire
        assert len(volume_alerts) >= 1


class TestRsiDivergence:
    """Tests for RSI divergence alerting."""

    def test_no_divergence_when_disabled(self) -> None:
        engine = AlertEngine()
        df = _make_alert_df()
        df["rsi14"] = [45, 55, 60]
        df["rsi14_prev"] = [55, 60, 50]
        df["high_today"] = [15.0, 14.5, 10.0]
        df["high_prev"] = [14.0, 14.0, 10.5]
        alerts = engine.process_alerts(df)
        div = [a for a in alerts if "divergence" in a.get("reason", "")]
        assert len(div) == 0

    def test_bearish_divergence_when_enabled(self) -> None:
        cfg = AlertEngineConfig(rsi_divergence_enabled=True, max_alerts_per_stock_per_day=10)
        engine = AlertEngine(config=cfg)
        df = pd.DataFrame([{
            "code": "000999", "name": "TestDiv", "price": 20.0,
            "change_pct": 1.0, "volume": 10_000_000,
            "volume_5d_avg": 10_000_000, "high_20d": 22.0,
            "low_20d": 15.0,
            "rsi14": 55, "rsi14_prev": 65,
            "high_today": 21.0, "high_prev": 20.0,
        }])
        alerts = engine.process_alerts(df)
        div = [a for a in alerts if "bearish" in a.get("reason", "")]
        assert len(div) == 1
        assert div[0]["level"] == "MEDIUM"

    def test_bullish_divergence_when_enabled(self) -> None:
        cfg = AlertEngineConfig(rsi_divergence_enabled=True, max_alerts_per_stock_per_day=10)
        engine = AlertEngine(config=cfg)
        df = pd.DataFrame([{
            "code": "000888", "name": "TestBull", "price": 15.0,
            "change_pct": -1.0, "volume": 5_000_000,
            "volume_5d_avg": 5_000_000, "high_20d": 18.0,
            "low_20d": 12.0,
            "rsi14": 45, "rsi14_prev": 35,
            "high_today": 14.0, "high_prev": 16.0,
        }])
        alerts = engine.process_alerts(df)
        div = [a for a in alerts if "bullish" in a.get("reason", "")]
        assert len(div) == 1
        assert div[0]["level"] == "MEDIUM"

    def test_no_divergence_when_columns_missing(self) -> None:
        cfg = AlertEngineConfig(rsi_divergence_enabled=True)
        engine = AlertEngine(config=cfg)
        df = _make_alert_df()  # no rsi14, rsi14_prev, high_today, high_prev
        alerts = engine.process_alerts(df)
        div = [a for a in alerts if "divergence" in a.get("reason", "")]
        assert len(div) == 0

    def test_no_divergence_when_nan(self) -> None:
        cfg = AlertEngineConfig(rsi_divergence_enabled=True)
        engine = AlertEngine(config=cfg)
        df = pd.DataFrame([{
            "code": "000999", "name": "TestNan", "price": 20.0,
            "change_pct": 1.0, "volume": 10_000_000,
            "volume_5d_avg": 10_000_000, "high_20d": 22.0,
            "low_20d": 15.0,
            "rsi14": np.nan, "rsi14_prev": 65,
            "high_today": 21.0, "high_prev": 20.0,
        }])
        alerts = engine.process_alerts(df)
        div = [a for a in alerts if "divergence" in a.get("reason", "")]
        assert len(div) == 0
