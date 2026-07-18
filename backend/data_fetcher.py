"""Market data acquisition layer built on AkShare.

Provides three public functions used by the rest of the monitoring system:

- :func:`get_all_spot_data`   -- realtime snapshot of every A-share stock.
- :func:`get_stock_history`   -- daily bars plus MA20 / RSI14 / MACD indicators.
- :func:`get_market_summary`  -- headline index data for the daily report.
- :func:`get_sector_map`      -- stock code -> THS industry sector mapping (cached daily).
- :func:`get_sector_rank`     -- THS industry sectors sorted by pct change.

Every function swallows network / data errors, prints a Chinese log line and
returns ``None`` on failure so callers never see exceptions from this module.
"""

from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# AkShare calls plain ``requests`` under the hood without exposing a timeout
# parameter, so cap the default socket timeout to keep hangs bounded.
socket.setdefaulttimeout(30)

try:
    import akshare as ak
except ImportError:  # pragma: no cover - dependency may be absent in CI
    ak = None


# ---------------------------------------------------------------------------
# Realtime spot snapshot
# ---------------------------------------------------------------------------
def get_all_spot_data() -> Optional[pd.DataFrame]:
    """Fetch the realtime quote snapshot of all A-share stocks via EastMoney.

    Returns a DataFrame whose Chinese columns are renamed to English; the
    key columns are ``code``, ``name``, ``price`` (latest price),
    ``change_pct`` (percent, e.g. 2.35), ``volume`` (shares),
    ``turnover`` (RMB) and ``pe`` (dynamic PE ratio).

    Returns ``None`` on any network or parsing failure.
    """
    if ak is None:
        print("获取实时行情失败：akshare 未安装")
        return None
    df = _spot_via_eastmoney()
    if df is None or df.empty:
        print("东财实时行情不可用，切换新浪源（较慢）……")
        df = _spot_via_sina()
    if df is None or df.empty:
        print("获取实时行情失败：东财与新浪源均无数据")
        return None
    return df


def _spot_via_eastmoney() -> Optional[pd.DataFrame]:
    """Primary spot source: EastMoney via ``ak.stock_zh_a_spot_em``."""
    try:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        return df.rename(
            columns={
                "序号": "seq",
                "代码": "code",
                "名称": "name",
                "最新价": "price",
                "涨跌幅": "change_pct",
                "涨跌额": "change_amount",
                "成交量": "volume",
                "成交额": "turnover",
                "振幅": "amplitude",
                "最高": "high",
                "最低": "low",
                "今开": "open",
                "昨收": "prev_close",
                "量比": "volume_ratio",
                "换手率": "turnover_rate",
                "市盈率-动态": "pe",
                "市净率": "pb",
                "总市值": "total_market_cap",
                "流通市值": "float_market_cap",
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"东财实时行情接口失败：{exc}")
        return None


def _spot_via_sina() -> Optional[pd.DataFrame]:
    """Fallback spot source: Sina via ``ak.stock_zh_a_spot`` (slow, paginated)."""
    try:
        df = ak.stock_zh_a_spot()
        if df is None or df.empty:
            return None
        df = df.rename(
            columns={
                "代码": "code",
                "名称": "name",
                "最新价": "price",
                "涨跌幅": "change_pct",
                "涨跌额": "change_amount",
                "成交量": "volume",
                "成交额": "turnover",
                "最高": "high",
                "最低": "low",
                "今开": "open",
                "昨收": "prev_close",
                "买入": "bid",
                "卖出": "ask",
                "时间戳": "tick_time",
                "市盈率": "pe",
                "市净率": "pb",
            }
        )
        # The Sina snapshot carries no PE column; keep the schema stable by
        # filling it with NaN so downstream code can rely on its presence.
        if "pe" not in df.columns:
            df["pe"] = np.nan
        # Sina codes carry a market prefix (sh600000 / sz000001); strip it so
        # the schema matches the EastMoney source (plain 6-digit codes).
        df["code"] = df["code"].astype(str).str.replace(
            r"^(sh|sz|bj)", "", regex=True
        )
        for col in ("price", "change_pct", "volume", "turnover", "pe"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as exc:  # noqa: BLE001
        print(f"新浪实时行情接口也失败：{exc}")
        return None


# ---------------------------------------------------------------------------
# Single-stock daily history with indicators
# ---------------------------------------------------------------------------
def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Append MA20, RSI-14 and MACD (12/26/9) columns to a history frame.

    The frame must already contain a numeric ``close`` column ordered by date.
    """
    close = df["close"].astype(float)

    # MA20
    df["ma20"] = close.rolling(window=20, min_periods=20).mean()

    # RSI-14 (Wilder smoothing)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    df["rsi14"] = 100.0 - (100.0 / (1.0 + rs))
    # If there were no losses at all in the window RSI is 100 by definition.
    df["rsi14"] = df["rsi14"].fillna(100.0).where(avg_gain.notna())

    # MACD (12/26/9); histogram follows the Chinese convention 2*(DIF-DEA).
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd_dif"] = ema12 - ema26
    df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = (df["macd_dif"] - df["macd_dea"]) * 2.0
    return df


def get_stock_history(code: str, days: int = 20) -> Optional[pd.DataFrame]:
    """Fetch the recent daily bars of one stock and compute indicators.

    Parameters
    ----------
    code:
        Six-digit stock code, e.g. ``"000001"``.
    days:
        Number of most recent trading days to return (default 20).

    Returns a DataFrame with at least the columns ``date``, ``open``,
    ``close``, ``high``, ``low``, ``volume``, plus the computed ``ma20``,
    ``rsi14``, ``macd_dif``, ``macd_dea`` and ``macd_hist`` columns.
    Roughly 60 trading days are pulled internally so MA20 / MACD have
    enough warm-up data; only the last ``days`` rows are returned.

    Returns ``None`` on any network or parsing failure.
    """
    if ak is None:
        print(f"获取 {code} 历史行情失败：akshare 未安装")
        return None
    # Pull ~150 calendar days (>= ~100 trading days) of history so the
    # indicators have enough warm-up bars before slicing the tail.
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=max(150, days * 3))).strftime(
        "%Y%m%d"
    )
    df = _history_via_eastmoney(code, start_date, end_date)
    if df is None or df.empty:
        print(f"{code} 东财历史行情不可用，切换新浪源……")
        df = _history_via_sina(code, start_date)
    if df is None or df.empty:
        print(f"获取 {code} 历史行情失败：东财与新浪源均无数据")
        return None
    try:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        df = _add_indicators(df)
        return df.tail(days).reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001
        print(f"获取 {code} 历史行情失败：{exc}")
        return None


def _history_via_eastmoney(
    code: str, start_date: str, end_date: str
) -> Optional[pd.DataFrame]:
    """Primary history source: EastMoney via ``ak.stock_zh_a_hist`` (qfq)."""
    try:
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if df is None or df.empty:
            return None
        return df.rename(
            columns={
                "日期": "date",
                "股票代码": "code",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "turnover",
                "振幅": "amplitude",
                "涨跌幅": "change_pct",
                "涨跌额": "change_amount",
                "换手率": "turnover_rate",
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"东财历史行情接口失败（{code}）：{exc}")
        return None


def _history_via_sina(code: str, start_date: str) -> Optional[pd.DataFrame]:
    """Fallback history source: Sina via ``ak.stock_zh_a_daily`` (qfq)."""
    try:
        # Sina wants a market-prefixed symbol (sh600000 / sz000001).
        symbol = ("sh" if code.startswith("6") else "sz") + code
        df = ak.stock_zh_a_daily(symbol=symbol, adjust="qfq")
        if df is None or df.empty:
            return None
        # Sina's own "turnover" is the turnover RATE; "amount" is the RMB
        # turnover, which matches the EastMoney schema.
        df = df.rename(columns={"amount": "turnover", "turnover": "turnover_rate"})
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] >= pd.to_datetime(start_date)]
        return df if not df.empty else None
    except Exception as exc:  # noqa: BLE001
        print(f"新浪历史行情接口也失败（{code}）：{exc}")
        return None


# ---------------------------------------------------------------------------
# Headline market summary
# ---------------------------------------------------------------------------
def _fetch_index_row(symbol: str) -> Optional[dict]:
    """Return the latest daily bar of one index as a plain dict.

    Tries the EastMoney interface first (has pct change and amount), then
    falls back to the Sina interface. Returns ``None`` when both fail.
    """
    try:
        df = ak.stock_zh_index_daily_em(symbol=symbol)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            return {
                "points": float(last["close"]),
                "change_pct": float(last.get("pct_chg", 0.0)),
                "amount": float(last.get("amount", 0.0)),
            }
    except Exception as exc:  # noqa: BLE001
        print(f"指数 {symbol} 东财接口失败，尝试新浪接口：{exc}")
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else last
            prev_close = float(prev["close"])
            close = float(last["close"])
            change_pct = (close - prev_close) / prev_close * 100.0 if prev_close else 0.0
            return {"points": close, "change_pct": change_pct, "amount": 0.0}
    except Exception as exc:  # noqa: BLE001
        print(f"指数 {symbol} 新浪接口也失败：{exc}")
    return None


def get_market_summary() -> Optional[list]:
    """Build the market summary for all major indices.

    Queries 上证指数 (sh000001), 深证成指 (sz399001), 创业板指 (sz399006),
    沪深300 (sh000300), 科创50 (sh000688) and returns a list of dicts.

    Also includes total_turnover, advance_count and decline_count in the
    first (primary) index dict for backward compatibility.

    Returns ``None`` only when even the primary index cannot be fetched.
    """
    if ak is None:
        print("获取大盘概况失败：akshare 未安装")
        return None
    try:
        indices = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000300": "沪深300",
            "sh000688": "科创50",
        }
        rows = []
        for code, name in indices.items():
            row = _fetch_index_row(code)
            if row:
                rows.append({
                    "name": name,
                    "code": code,
                    "price": row["points"],
                    "change_pct": row["change_pct"],
                    "change": row.get("change", None),
                })

        if not rows:
            print("获取大盘概况失败：所有指数均无数据")
            return None

        # Approximate the whole-market turnover with SH + SZ index amounts
        sh = _fetch_index_row("sh000001")
        sz = _fetch_index_row("sz399001")
        total_turnover = (sh["amount"] if sh else 0.0) + (sz["amount"] if sz else 0.0)

        advance_count = 0
        decline_count = 0
        try:
            activity = ak.stock_market_activity_legu()
            if activity is not None and not activity.empty:
                row = activity.set_index("item")["value"]
                advance_count = int(row.get("上涨", 0))
                decline_count = int(row.get("下跌", 0))
        except Exception as exc:  # noqa: BLE001
            print(f"涨跌家数获取失败，填 0：{exc}")

        # Put market-wide stats into the primary (first) index for compat
        rows[0]["total_turnover"] = float(total_turnover)
        rows[0]["advance_count"] = advance_count
        rows[0]["decline_count"] = decline_count

        return rows
    except Exception as exc:  # noqa: BLE001
        print(f"获取大盘概况失败：{exc}")
        return None


# ---------------------------------------------------------------------------
# Industry sector data (TongHuaShun / 同花顺)
# ---------------------------------------------------------------------------
SECTOR_MAP_CACHE_PATH = "sector_map.json"
"""Default cache file for the code -> industry-sector mapping (daily reuse)."""

_THS_BOARD_PAGE_URL = "http://q.10jqka.com.cn/thshy/detail/code/{code}/page/{page}/"
_THS_BOARD_PAGE_ASC_URL = (
    "http://q.10jqka.com.cn/thshy/detail/code/{code}/order/asc/page/{page}/"
)
_THS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/89.0.4389.90 Safari/537.36"
)
_THS_MAX_PAGES_PER_ORDER = 5
"""q.10jqka.com.cn 对同一排序方向第 6 页起返回 chameleon 反爬页，最多翻 5 页。"""


def get_sector_rank() -> Optional[List[str]]:
    """Return THS industry board names sorted by daily pct change, best first.

    Source: ``ak.stock_board_industry_summary_ths``. Returns ``None`` on any
    network or parsing failure.
    """
    if ak is None:
        print("获取行业板块排名失败：akshare 未安装")
        return None
    try:
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            print("获取行业板块排名失败：同花顺行业一览表无数据")
            return None
        df = df.copy()
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        df = df.dropna(subset=["涨跌幅"]).sort_values("涨跌幅", ascending=False)
        return df["板块"].astype(str).tolist()
    except Exception as exc:  # noqa: BLE001
        print(f"获取行业板块排名失败：{exc}")
        return None


def _fetch_board_constituents(
    board_code: str, delay: float = 0.3, retries: int = 3
) -> List[str]:
    """Fetch all constituent stock codes of one THS industry board.

    Walks the paginated plain-HTML detail pages (20 rows per page); the ajax
    endpoints are blocked (401/403) on some networks, the plain ``/page/N/``
    URLs are not. Pages beyond ``_THS_MAX_PAGES_PER_ORDER`` per sort order
    are answered with a chameleon anti-crawler page, so boards with more
    than 5 pages are re-fetched in ascending order to reach their tail;
    for boards with more than 10 pages the middle slice stays uncovered
    (logged). Each page is retried with backoff. Raises on failure -- the
    caller decides how to degrade.
    """
    import requests  # local import: only needed for this THS scraping path
    from bs4 import BeautifulSoup

    headers = {"User-Agent": _THS_UA}

    def get_page(url: str) -> tuple:
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                resp.raise_for_status()
                table = pd.read_html(StringIO(resp.text))[0]
                return resp.text, table
            except Exception:  # noqa: BLE001 - retried, re-raised on last attempt
                if attempt == retries - 1:
                    raise
                time.sleep(3 * (attempt + 1))

    codes: List[str] = []
    url = _THS_BOARD_PAGE_URL.format(code=board_code, page=1)
    text, table = get_page(url)
    soup = BeautifulSoup(text, features="lxml")
    total_pages = 1
    info = soup.find(name="span", attrs={"class": "page_info"})
    if info is not None and "/" in info.text:
        total_pages = max(1, int(info.text.split("/")[1]))
    codes.extend(table["代码"].astype(str).str.zfill(6).tolist())

    # Remaining descending pages (capped), then ascending pages for the tail.
    fetch_plan = [
        (_THS_BOARD_PAGE_URL, p)
        for p in range(2, min(_THS_MAX_PAGES_PER_ORDER, total_pages) + 1)
    ]
    if total_pages > _THS_MAX_PAGES_PER_ORDER:
        asc_pages = min(_THS_MAX_PAGES_PER_ORDER, total_pages - _THS_MAX_PAGES_PER_ORDER)
        fetch_plan += [(_THS_BOARD_PAGE_ASC_URL, p) for p in range(1, asc_pages + 1)]
        if total_pages > 2 * _THS_MAX_PAGES_PER_ORDER:
            print(
                f"板块代码 {board_code} 共 {total_pages} 页，受反爬限制"
                f"中间 {total_pages - 2 * _THS_MAX_PAGES_PER_ORDER} 页成份股无法覆盖"
            )
    for template, p in fetch_plan:
        _, table = get_page(template.format(code=board_code, page=p))
        codes.extend(table["代码"].astype(str).str.zfill(6).tolist())
        time.sleep(delay)
    return codes


def get_sector_map(
    max_boards: Optional[int] = None,
    cache_path: str = SECTOR_MAP_CACHE_PATH,
    delay: float = 0.3,
) -> Optional[Dict[str, str]]:
    """Build a mapping of stock code -> THS industry sector name.

    Iterates the THS industry board list and scrapes each board's
    constituents; boards are processed in pct-change order so that
    ``max_boards`` (when given) keeps only the strongest N boards and bounds
    the request count. The result is cached in ``cache_path`` together with
    today's date and reused for the rest of the day.

    Returns ``None`` (with a Chinese log line) when nothing could be built.
    """
    if ak is None:
        print("获取行业板块映射失败：akshare 未安装")
        return None

    # Reuse today's cache when present.
    today = datetime.now().strftime("%Y-%m-%d")
    cache_file = Path(cache_path)
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            cached_map = cached.get("map")
            if cached.get("date") == today and isinstance(cached_map, dict) and cached_map:
                print(f"使用今日板块映射缓存 {cache_path}（{len(cached_map)} 只股票）")
                return cached_map
        except (json.JSONDecodeError, OSError) as exc:
            print(f"板块映射缓存读取失败（{exc}），重新构建")

    try:
        names = ak.stock_board_industry_name_ths()
        if names is None or names.empty:
            print("获取行业板块映射失败：同花顺行业列表无数据")
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"获取行业板块映射失败：{exc}")
        return None

    name_to_code = dict(
        zip(names["name"].astype(str), names["code"].astype(str))
    )
    # Process boards strongest-first so max_boards keeps the hot ones.
    rank = get_sector_rank() or []
    board_names = [b for b in rank if b in name_to_code]
    board_names += [b for b in name_to_code if b not in set(board_names)]
    if max_boards is not None:
        board_names = board_names[:max_boards]

    sector_map: Dict[str, str] = {}
    total = len(board_names)
    t0 = time.time()
    for i, board in enumerate(board_names, 1):
        try:
            for code in _fetch_board_constituents(name_to_code[board], delay=delay):
                sector_map[code] = board
        except Exception as exc:  # noqa: BLE001
            print(f"板块 {board} 成份股获取失败（{exc}），跳过该板块")
        if i % 10 == 0 or i == total:
            print(f"板块映射构建进度 {i}/{total}，已映射 {len(sector_map)} 只股票……")

    if not sector_map:
        print("获取行业板块映射失败：所有板块均无成份股数据")
        return None
    print(
        f"板块映射构建完成：{total} 个板块、{len(sector_map)} 只股票，"
        f"耗时 {time.time() - t0:.1f}s"
    )
    try:
        cache_file.write_text(
            json.dumps({"date": today, "map": sector_map}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"板块映射缓存写入失败（{exc}），不影响本次使用")
    return sector_map
