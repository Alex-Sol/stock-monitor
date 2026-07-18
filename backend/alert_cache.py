"""Persistent, mockable alert deduplication cache.

Provides an abstract ``AlertCache`` interface and a default
``JsonFileAlertCache`` implementation that stores per-stock alert
counts keyed by trading day in a JSON file.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class AlertCache(ABC):
    """Abstract interface for per-stock alert deduplication.

    Implementations must survive process restarts: counts from a
    prior run within the same trading day must be preserved.
    """

    @abstractmethod
    def get_count(self, code: str) -> int:
        """Return the number of alerts already sent for *code* today."""
        ...

    @abstractmethod
    def increment(self, code: str, max_per_day: int) -> int:
        """Increment the alert count for *code* and return the new count.

        Must *not* exceed *max_per_day*.  Returns the count *after*
        incrementing (i.e. 1 on first call, 2 on second, etc.).
        """
        ...

    @abstractmethod
    def reset_daily(self) -> None:
        """Clear all alert counters for the current trading day.

        Typically called at the start of a new trading day."""
        ...


class JsonFileAlertCache(AlertCache):
    """File-backed alert cache persisted as JSON.

    The JSON structure is::

        {
            "2026-07-18": {
                "000001": 2,
                "000002": 1
            }
        }

    Old trading-day entries are *not* automatically pruned so that
    callers can inspect history if needed.

    Parameters
    ----------
    filepath : str or Path
        Path to the JSON cache file.
    trading_day : date, optional
        Override the trading-day key.  Defaults to ``date.today()``.
        Useful for testing against a fixed date.
    """

    def __init__(
        self,
        filepath: str | Path = "./data/alert_cache.json",
        trading_day: Optional[date] = None,
    ) -> None:
        self._filepath = Path(filepath)
        self._trading_day = trading_day or date.today()
        self._data: Dict[str, Dict[str, int]] = {}
        self._load()

    # -- file I/O ---------------------------------------------------------

    def _load(self) -> None:
        if self._filepath.exists():
            try:
                with open(self._filepath, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
                logger.debug("Loaded alert cache from %s", self._filepath)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read alert cache %s: %s", self._filepath, exc)
                self._data = {}
        else:
            self._data = {}

    def _save(self) -> None:
        self._filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(self._filepath, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, ensure_ascii=False)

    # -- public interface --------------------------------------------------

    @property
    def _today_key(self) -> str:
        return self._trading_day.isoformat()

    def get_count(self, code: str) -> int:
        today = self._data.get(self._today_key, {})
        return today.get(code, 0)

    def increment(self, code: str, max_per_day: int) -> int:
        today = self._data.setdefault(self._today_key, {})
        current = today.get(code, 0)
        if current >= max_per_day:
            return current
        new_count = current + 1
        today[code] = new_count
        self._save()
        return new_count

    def reset_daily(self) -> None:
        today = self._trading_day.isoformat()
        if today in self._data:
            del self._data[today]
            self._save()
            logger.info("Reset alert cache for trading day %s", today)


class InMemoryAlertCache(AlertCache):
    """Non-persistent cache for testing and development.

    Same interface as ``JsonFileAlertCache`` but stores counts in a
    plain dict that is lost on restart."""

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}

    def get_count(self, code: str) -> int:
        return self._counts.get(code, 0)

    def increment(self, code: str, max_per_day: int) -> int:
        current = self._counts.get(code, 0)
        if current >= max_per_day:
            return current
        new_count = current + 1
        self._counts[code] = new_count
        return new_count

    def reset_daily(self) -> None:
        self._counts.clear()


# =========================================================================
# Unit tests (pytest-compatible)
# =========================================================================


class TestInMemoryAlertCache:
    """Tests for InMemoryAlertCache (also exercises the abstract contract)."""

    def test_initial_count_zero(self) -> None:
        cache = InMemoryAlertCache()
        assert cache.get_count("000001") == 0

    def test_increment_and_get(self) -> None:
        cache = InMemoryAlertCache()
        new = cache.increment("000001", max_per_day=3)
        assert new == 1
        assert cache.get_count("000001") == 1

    def test_max_per_day_respected(self) -> None:
        cache = InMemoryAlertCache()
        for _ in range(5):
            cache.increment("000001", max_per_day=2)
        assert cache.get_count("000001") == 2

    def test_multiple_stocks_independent(self) -> None:
        cache = InMemoryAlertCache()
        cache.increment("000001", max_per_day=5)
        cache.increment("000001", max_per_day=5)
        cache.increment("000002", max_per_day=5)
        assert cache.get_count("000001") == 2
        assert cache.get_count("000002") == 1

    def test_reset_daily(self) -> None:
        cache = InMemoryAlertCache()
        cache.increment("000001", max_per_day=5)
        cache.increment("000001", max_per_day=5)
        cache.reset_daily()
        assert cache.get_count("000001") == 0


class TestJsonFileAlertCache:
    """Tests for JsonFileAlertCache with file persistence."""

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        from datetime import date

        fp = tmp_path / "alert_cache.json"
        day = date(2026, 7, 18)

        # First instance: issue alerts
        cache1 = JsonFileAlertCache(filepath=fp, trading_day=day)
        cache1.increment("000001", max_per_day=3)
        cache1.increment("000001", max_per_day=3)
        cache1.increment("000002", max_per_day=3)

        # Simulate restart: create a new instance pointing at the same file
        cache2 = JsonFileAlertCache(filepath=fp, trading_day=day)
        assert cache2.get_count("000001") == 2
        assert cache2.get_count("000002") == 1
        assert cache2.get_count("000003") == 0

    def test_max_per_day_respected_across_restarts(self, tmp_path: Path) -> None:
        from datetime import date

        fp = tmp_path / "alert_cache.json"
        day = date(2026, 7, 18)

        cache1 = JsonFileAlertCache(filepath=fp, trading_day=day)
        for _ in range(5):
            cache1.increment("000001", max_per_day=2)

        cache2 = JsonFileAlertCache(filepath=fp, trading_day=day)
        assert cache2.get_count("000001") == 2
        # Further increments should be capped
        new = cache2.increment("000001", max_per_day=2)
        assert new == 2

    def test_different_trading_day_independent(self, tmp_path: Path) -> None:
        from datetime import date

        fp = tmp_path / "alert_cache.json"
        cache1 = JsonFileAlertCache(filepath=fp, trading_day=date(2026, 7, 17))
        cache1.increment("000001", max_per_day=3)
        cache1.increment("000001", max_per_day=3)

        cache2 = JsonFileAlertCache(filepath=fp, trading_day=date(2026, 7, 18))
        assert cache2.get_count("000001") == 0

    def test_reset_daily_clears_today(self, tmp_path: Path) -> None:
        from datetime import date

        fp = tmp_path / "alert_cache.json"
        day = date(2026, 7, 18)
        cache = JsonFileAlertCache(filepath=fp, trading_day=day)
        cache.increment("000001", max_per_day=5)
        cache.reset_daily()
        assert cache.get_count("000001") == 0

    def test_corrupt_file_graceful(self, tmp_path: Path) -> None:
        fp = tmp_path / "alert_cache.json"
        fp.write_text("this is not valid json", encoding="utf-8")
        cache = JsonFileAlertCache(filepath=fp)
        assert cache.get_count("000001") == 0
        cache.increment("000001", max_per_day=5)
        assert cache.get_count("000001") == 1

    def test_missing_file_starts_fresh(self, tmp_path: Path) -> None:
        fp = tmp_path / "nonexistent.json"
        cache = JsonFileAlertCache(filepath=fp)
        assert cache.get_count("000001") == 0
