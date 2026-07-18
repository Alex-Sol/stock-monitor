"""Main entry point for the A-share stock monitoring system.

Three modes (``--mode``):

- ``select``  : coarse-filter the full-market snapshot, enrich each surviving
  stock with 20-day history, run ``screen_stocks`` and dump candidates.json.
- ``monitor`` : check the configured watchlist against the alert engine and
  push triggered alerts to the WeChat Work webhook.
- ``report``  : build the daily Markdown report, save it to disk and push it
  to the webhook.

Every step logs a timestamped Chinese message; any data source returning
``None`` degrades gracefully instead of crashing.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from alert_engine import AlertEngine
from config import AlertEngineConfig, DailyReportConfig, LLMConfig, StockSelectorConfig
from daily_report import generate_daily_report
from data_fetcher import (
    get_all_spot_data,
    get_market_summary,
    get_sector_map,
    get_sector_rank,
    get_stock_history,
)
from llm_client import (
    generate_alerts as llm_generate_alerts,
    generate_report as llm_generate_report,
    select_stocks as llm_select_stocks,
)
from notifier import send_daily_report, send_wechat_alert
from stock_selector import screen_stocks

# Mirror of config.json, used when the file is missing or unreadable.
DEFAULT_CONFIG: Dict[str, Any] = {
    "watchlist": ["000001", "600519", "000858"],
    "thresholds": {
        "change_pct": 5.0,
        "volume_surge_ratio": 3.0,
        "rsi_lower": 40,
        "rsi_upper": 70,
        "pe_lower": 0,
        "pe_upper": 100,
        "min_volume_5d": 100_000_000,
        "max_change_20d": 0.5,
        "max_alerts_per_stock_per_day": 2,
    },
    "webhook_url": "",
    "llm": {
        "enabled": False,
        "api_key": "",
        "base_url": "https://api.kimi.com/coding/v1",
        "model": "kimi-k2.6",
        "timeout": 60,
        "temperature": 0.6,
    },
    "schedule": {
        "select_time": "09:00",
        "report_time": "15:30",
        "monitor_interval_minutes": 5,
    },
}

HISTORY_DELAY_SECONDS = 0.3
"""Pause between per-stock history calls to avoid rate limiting."""


# ---------------------------------------------------------------------------
# Logging / config helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    """Print a timestamped log line."""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config(path: str) -> Dict[str, Any]:
    """Load config.json; fall back to defaults with a warning on failure."""
    cfg_path = Path(path)
    if not cfg_path.exists():
        log(f"警告：配置文件 {path} 不存在，使用默认配置")
        return dict(DEFAULT_CONFIG)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告：配置文件 {path} 读取失败（{exc}），使用默认配置")
        return dict(DEFAULT_CONFIG)
    # Fill any missing top-level keys from defaults so downstream code can
    # rely on their presence.
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    merged["thresholds"] = {**DEFAULT_CONFIG["thresholds"], **cfg.get("thresholds", {})}
    llm_section = cfg.get("llm", {})
    if isinstance(llm_section, dict):
        merged["llm"] = {**DEFAULT_CONFIG["llm"], **llm_section}
    log(f"已加载配置文件 {path}")
    return merged


def resolve_data_dir(cfg: Dict[str, Any]) -> Path:
    """Resolve data_output_dir relative to the project root (parent of backend/)."""
    raw = cfg.get("data_output_dir", ".")
    p = Path(raw)
    if p.is_absolute():
        return p
    # 相对路径基于 backend/main.py 的父目录的父目录（项目根目录）
    project_root = Path(__file__).resolve().parent.parent
    return (project_root / p).resolve()


def build_selector_config(th: Dict[str, Any]) -> StockSelectorConfig:
    """Map config.json ``thresholds`` onto a StockSelectorConfig."""
    return StockSelectorConfig(
        min_volume_5d=float(th.get("min_volume_5d", 100_000_000)),
        rsi_lower=float(th.get("rsi_lower", 40)),
        rsi_upper=float(th.get("rsi_upper", 70)),
        pe_lower=float(th.get("pe_lower", 0)),
        pe_upper=float(th.get("pe_upper", 100)),
        max_change_20d=float(th.get("max_change_20d", 0.5)),
    )


def build_alert_config(th: Dict[str, Any]) -> AlertEngineConfig:
    """Map config.json ``thresholds`` onto an AlertEngineConfig."""
    return AlertEngineConfig(
        change_pct_threshold=float(th.get("change_pct", 5.0)),
        volume_surge_ratio=float(th.get("volume_surge_ratio", 3.0)),
        max_alerts_per_stock_per_day=int(th.get("max_alerts_per_stock_per_day", 2)),
    )


def build_llm_config(cfg: Dict[str, Any]) -> LLMConfig:
    """Map config.json ``llm`` section onto an LLMConfig."""
    llm = cfg.get("llm", {})
    if not isinstance(llm, dict):
        llm = {}
    return LLMConfig(
        enabled=bool(llm.get("enabled", False)),
        api_key=str(llm.get("api_key", "")),
        base_url=str(llm.get("base_url", "https://api.kimi.com/coding/v1")),
        model=str(llm.get("model", "kimi-k2.6")),
        timeout=int(llm.get("timeout", 60)),
        temperature=float(llm.get("temperature", 0.6)),
    )


def _notify_result(ok: bool, what: str) -> None:
    """Log the webhook push outcome; placeholder-URL failures are expected."""
    if ok:
        log(f"{what}已推送至企业微信")
    else:
        log(f"{what}推送失败（webhook 为占位符时属预期，不影响主流程）")


# ---------------------------------------------------------------------------
# Mode: select
# ---------------------------------------------------------------------------
def coarse_filter(
    spot: pd.DataFrame, th: Dict[str, Any], max_stocks: int
) -> pd.DataFrame:
    """Narrow the full-market snapshot to at most *max_stocks* rows.

    Filters by the configured PE range when PE data exists, keeps rows whose
    PE is NaN (data source may simply lack PE), then sorts by turnover and
    keeps the top N.
    """
    df = spot.copy()
    df["turnover"] = pd.to_numeric(df.get("turnover"), errors="coerce")
    df["pe"] = pd.to_numeric(df.get("pe"), errors="coerce")
    if df["pe"].notna().any():
        pe_lo, pe_hi = float(th.get("pe_lower", 0)), float(th.get("pe_upper", 100))
        in_range = df["pe"].isna() | ((df["pe"] > pe_lo) & (df["pe"] < pe_hi))
        df = df.loc[in_range]
    else:
        log("快照不含 PE 数据，粗筛阶段跳过 PE 过滤")
    df = df.sort_values("turnover", ascending=False, na_position="last")
    return df.head(max_stocks)


def build_screen_row(spot_row: pd.Series, hist: pd.DataFrame) -> Optional[dict]:
    """Assemble one ``screen_stocks`` input row from snapshot + history.

    Returns ``None`` when the history frame is unusable.
    """
    try:
        last = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else last
        first_close = float(hist.iloc[0]["close"])
        last_close = float(last["close"])
        sector = spot_row.get("sector")
        return {
            "code": str(spot_row["code"]),
            "name": str(spot_row.get("name", "")),
            "close": last_close,
            "ma20": float(last["ma20"]),
            # 5-day average turnover in RMB (matches config semantics).
            "volume_5d": float(hist["turnover"].tail(5).mean()),
            "rsi14": float(last["rsi14"]),
            "macd_hist": float(last["macd_hist"]),
            "pe": float(spot_row["pe"]) if pd.notna(spot_row.get("pe")) else np.nan,
            "high_20d": float(hist["high"].max()),
            "low_20d": float(hist["low"].min()),
            "change_20d": (last_close / first_close - 1.0) if first_close else 0.0,
            "sector": str(sector) if pd.notna(sector) else "unknown",
            "has_bad_news": False,  # no news data source available
            # Optional scoring columns.
            "volume_today": (
                float(spot_row["volume"]) if pd.notna(spot_row.get("volume")) else np.nan
            ),
            "macd_hist_prev": float(prev["macd_hist"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        log(f"组装 {spot_row.get('code')} 筛选数据失败：{exc}")
        return None


def run_select(cfg: Dict[str, Any], max_stocks: int) -> None:
    """Run the stock screening pipeline and write candidates.json."""
    selector_cfg = build_selector_config(cfg["thresholds"])

    log("开始获取全市场实时行情...")
    spot = get_all_spot_data()
    if spot is None or spot.empty:
        log("获取行情失败，选股流程中止")
        return
    log(f"获取到 {len(spot)} 只股票快照")

    pool = coarse_filter(spot, cfg["thresholds"], max_stocks)
    log(f"粗筛后剩余 {len(pool)} 只（上限 {max_stocks}），开始逐只拉取历史数据...")

    # 行业板块数据（同花顺）：任一环节失败都降级为 unknown / None，不影响主流程。
    log("开始获取行业板块数据（同花顺）...")
    sector_map = get_sector_map()
    if sector_map:
        pool = pool.copy()
        pool["sector"] = pool["code"].astype(str).map(sector_map).fillna("unknown")
        n_known = int((pool["sector"] != "unknown").sum())
        log(f"板块映射完成：{n_known}/{len(pool)} 只股票匹配到行业板块")
    else:
        log("行业板块映射不可用，sector 统一按 unknown 处理")
    sector_rank = get_sector_rank()
    if sector_rank:
        log(f"行业板块排名：榜首 {sector_rank[0]}，共 {len(sector_rank)} 个板块")
    else:
        log("行业板块排名不可用，板块加分项跳过")

    rows: List[dict] = []
    total = len(pool)
    for i, (_, spot_row) in enumerate(pool.iterrows(), 1):
        code = str(spot_row["code"])
        hist = get_stock_history(code, days=20)
        if hist is None or hist.empty:
            log(f"({i}/{total}) {code} 历史数据获取失败，跳过")
        else:
            row = build_screen_row(spot_row, hist)
            if row is not None:
                rows.append(row)
            else:
                log(f"({i}/{total}) {code} 数据组装失败，跳过")
        time.sleep(HISTORY_DELAY_SECONDS)

    log(f"成功组装 {len(rows)}/{total} 只股票数据，开始执行筛选...")
    data_dir = resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "candidates.json"
    if not rows:
        out_path.write_text("[]", encoding="utf-8")
        log("无可用数据，已写入空的 candidates.json")
        return

    df = pd.DataFrame(rows)
    # When the source lacks PE entirely (e.g. Sina fallback), neutralize the
    # PE filter with the configured range midpoint so screening can proceed.
    if df["pe"].isna().any():
        mid = (selector_cfg.pe_lower + selector_cfg.pe_upper) / 2.0
        n_missing = int(df["pe"].isna().sum())
        log(f"警告：{n_missing} 只股票缺少 PE 数据，已按区间中值 {mid} 填充"
            "（相当于 PE 项不参与过滤）")
        df["pe"] = df["pe"].fillna(mid)

    result = None
    llm_cfg = build_llm_config(cfg)
    if llm_cfg.enabled:
        log("已配置走大模型，调用 LLM 选股...")
        picked = llm_select_stocks(
            df.to_dict(orient="records"), llm_cfg, top_n=20,
            sector_rank=sector_rank,
        )
        if picked is not None:
            result = pd.DataFrame(picked)
            log(f"LLM 选股完成，入选 {len(result)} 只（模型 {llm_cfg.model}）")
        else:
            log("LLM 选股失败，回退到规则选股")
    if result is None:
        result = screen_stocks(df, sector_rank=sector_rank, top_n=20, config=selector_cfg)
    result.to_json(out_path, orient="records", force_ascii=False, indent=2)
    log(f"筛选完成，入选 {len(result)} 只，已保存至 {out_path}")
    if not result.empty:
        preview = result[["code", "name", "close", "score"]].head(5)
        for _, r in preview.iterrows():
            log(f"  入选：{r['code']} {r['name']} 收盘 {r['close']:.2f} 得分 {r['score']}")


# ---------------------------------------------------------------------------
# Mode: monitor
# ---------------------------------------------------------------------------
def run_monitor(cfg: Dict[str, Any]) -> None:
    """Check the watchlist for abnormal moves and push alerts."""
    alert_cfg = build_alert_config(cfg["thresholds"])
    watchlist = [str(c) for c in cfg.get("watchlist", [])]
    if not watchlist:
        log("自选股列表为空，监控流程中止")
        return

    log(f"开始获取行情，监控自选股：{', '.join(watchlist)}")
    spot = get_all_spot_data()
    if spot is None or spot.empty:
        log("获取行情失败，监控流程中止")
        return
    spot["code"] = spot["code"].astype(str)
    sub = spot.loc[spot["code"].isin(watchlist)]
    missing = sorted(set(watchlist) - set(sub["code"]))
    for code in missing:
        log(f"警告：自选股 {code} 不在快照中，跳过")
    if sub.empty:
        log("快照中没有匹配的自选股，监控流程中止")
        return

    log(f"匹配到 {len(sub)} 只自选股，开始拉取历史数据补全指标...")
    rows: List[dict] = []
    for _, srow in sub.iterrows():
        code = str(srow["code"])
        hist = get_stock_history(code, days=20)
        if hist is None or hist.empty:
            log(f"{code} 历史数据获取失败，跳过")
            continue
        try:
            rows.append({
                "code": code,
                "name": str(srow.get("name", "")),
                "price": float(srow["price"]),
                "change_pct": float(srow["change_pct"]),
                "volume": float(srow["volume"]),
                # 5-day average VOLUME (shares) to compare against today's volume.
                "volume_5d_avg": float(hist["volume"].tail(5).mean()),
                "high_20d": float(hist["high"].max()),
                "low_20d": float(hist["low"].min()),
            })
        except (KeyError, TypeError, ValueError) as exc:
            log(f"{code} 数据组装失败，跳过：{exc}")
        time.sleep(HISTORY_DELAY_SECONDS)

    if not rows:
        log("无可用自选股数据，监控流程结束")
        return

    # 写入 watchlist.json（自选股实时数据）
    data_dir = resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    watchlist_path = data_dir / "watchlist.json"
    watchlist_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已保存自选股数据到 {watchlist_path}")

    alerts = None
    llm_cfg = build_llm_config(cfg)
    if llm_cfg.enabled:
        log("已配置走大模型，调用 LLM 识别异动...")
        alerts = llm_generate_alerts(rows, llm_cfg)
        if alerts is not None:
            log(f"LLM 识别完成，{len(alerts)} 条异动（模型 {llm_cfg.model}）")
        else:
            log("LLM 识别失败，回退到规则引擎")
    if alerts is None:
        engine = AlertEngine(config=alert_cfg)
        alerts = engine.process_alerts(pd.DataFrame(rows))
    if not alerts:
        log("自选股无异动")
        return
    log(f"检测到 {len(alerts)} 条异动：")
    for a in alerts:
        log(f"  [{a['level']}] {a['code']} {a['name']} {a['reason']}")

    # 追加写入 alerts.json
    data_dir = resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    alerts_path = data_dir / "alerts.json"
    existing = []
    if alerts_path.exists():
        try:
            existing = json.loads(alerts_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except Exception:
            existing = []
    for a in alerts:
        a["timestamp"] = datetime.now().isoformat()
    existing.extend(alerts)
    alerts_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已追加 {len(alerts)} 条预警到 {alerts_path}")

    ok = send_wechat_alert(alerts, cfg)
    _notify_result(ok, "异动提醒")


# ---------------------------------------------------------------------------
# Mode: report
# ---------------------------------------------------------------------------
def _load_recent_alerts(cfg: Dict[str, Any], limit: int = 10) -> List[dict]:
    """Read the most recent alerts from alerts.json (for the LLM report)."""
    alerts_path = resolve_data_dir(cfg) / "alerts.json"
    if not alerts_path.exists():
        return []
    try:
        data = json.loads(alerts_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"警告：{alerts_path} 解析失败（{exc}），按无预警处理")
        return []
    if not isinstance(data, list):
        return []
    return data[-limit:]


def run_report(cfg: Dict[str, Any]) -> None:
    """Generate, print, save and push the daily Markdown report."""
    report_cfg = DailyReportConfig()

    log("开始获取大盘概况...")
    market = get_market_summary()
    if market is None:
        log("大盘概况获取失败，使用占位数据继续生成报告")
        market = {
            "index_name": "上证指数",
            "index_points": 0.0,
            "index_change_pct": 0.0,
            "total_turnover": 0.0,
            "advance_count": 0,
            "decline_count": 0,
        }

    log("开始获取自选股当日表现...")
    watch_cols = ["code", "name", "price", "change_pct"]
    watch_df = pd.DataFrame(columns=watch_cols)
    watchlist = [str(c) for c in cfg.get("watchlist", [])]
    spot = get_all_spot_data()
    if spot is None or spot.empty:
        log("获取行情失败，自选股表现按空表处理")
    else:
        spot["code"] = spot["code"].astype(str)
        watch_df = spot.loc[spot["code"].isin(watchlist), watch_cols].reset_index(drop=True)
        log(f"自选股当日表现共 {len(watch_df)} 条")

    cand_path = resolve_data_dir(cfg) / "candidates.json"
    if cand_path.exists():
        try:
            # json.load keeps codes as strings; pd.read_json would coerce
            # numeric-looking codes to int and drop leading zeros.
            candidates_df = pd.DataFrame(json.loads(cand_path.read_text(encoding="utf-8")))
            log(f"已读取 {cand_path}，候选股 {len(candidates_df)} 只")
        except (json.JSONDecodeError, OSError) as exc:
            log(f"警告：{cand_path} 解析失败（{exc}），按空表处理")
            candidates_df = pd.DataFrame()
    else:
        log(f"{cand_path} 不存在，候选股按空表处理")
        candidates_df = pd.DataFrame()

    log("开始生成收盘日报...")
    llm_cfg = build_llm_config(cfg)
    report_md: Optional[str] = None
    report_date = cfg.get("report_date", date.today().isoformat())
    log(f"日报日期: {report_date}")

    llm_preview: Optional[Dict[str, Any]] = None
    if llm_cfg.enabled:
        log("已配置走大模型，调用 LLM 生成日报...")
        recent_alerts = _load_recent_alerts(cfg, limit=10)
        llm_preview = llm_generate_report(
            market,
            watch_df.to_dict(orient="records") if not watch_df.empty else [],
            recent_alerts,
            candidates_df.to_dict(orient="records") if not candidates_df.empty else [],
            llm_cfg,
            report_date=report_date,
        )
        if llm_preview is not None:
            report_md = llm_preview["markdown"]
            log(f"LLM 日报生成完成（模型 {llm_cfg.model}）")
        else:
            log("LLM 日报生成失败，回退到规则模板")
    if report_md is None:
        report_md = generate_daily_report(
            market, watch_df, [], candidates_df, config=report_cfg,
            report_date=date.fromisoformat(report_date) if isinstance(report_date, str) else report_date
        )
    print(report_md)

    data_dir = resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 保存 Markdown 日报
    out_path = data_dir / f"report_{report_date}.md"
    out_path.write_text(report_md, encoding="utf-8")
    log(f"日报已保存至 {out_path}")

    # 保存结构化 daily_report.json
    daily_report_json = {
        "date": report_date,
        "market": market,
        "watchlist": watch_df.to_dict(orient="records") if not watch_df.empty else [],
        "alerts": [],
        "candidates": candidates_df.to_dict(orient="records") if not candidates_df.empty else [],
    }
    if llm_preview is not None:
        # 供前端「日报预览」展示的 LLM 摘要字段
        daily_report_json.update({
            "title": llm_preview["title"],
            "summary": llm_preview["summary"],
            "highlights": llm_preview["highlights"],
        })
    report_json_path = data_dir / "daily_report.json"
    report_json_path.write_text(json.dumps(daily_report_json, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结构化日报已保存至 {report_json_path}")

    # 保存 market_summary.json
    summary_path = data_dir / "market_summary.json"
    summary_path.write_text(json.dumps(market, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"大盘概况已保存至 {summary_path}")

    ok = send_daily_report(report_md, cfg)
    _notify_result(ok, "收盘日报")



import subprocess

def git_commit_push(repo_root: str) -> bool:
    """自动将 data/ 目录的变更提交并推送到 GitHub 的 data 分支。"""
    try:
        root = Path(repo_root).resolve()
        # 检查是否在 git 仓库内
        if not (root / ".git").exists():
            log(f"Git 仓库未初始化，跳过自动提交：{root}")
            return False

        # 检查是否有变更
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root), capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            log(f"git status 失败：{result.stderr}")
            return False

        # 过滤出 data/ 目录的变更
        lines = [l for l in result.stdout.strip().splitlines() if "data/" in l]
        if not lines:
            log("data/ 目录无变更，跳过 Git 提交")
            return True

        log(f"data/ 目录有 {len(lines)} 个变更，开始 Git 提交...")

        # 切换到 data 分支
        r0 = subprocess.run(
            ["git", "checkout", "data"],
            cwd=str(root), capture_output=True, text=True, timeout=15
        )
        if r0.returncode != 0:
            # data 分支不存在，创建它
            r0b = subprocess.run(
                ["git", "checkout", "-b", "data"],
                cwd=str(root), capture_output=True, text=True, timeout=15
            )
            if r0b.returncode != 0:
                log(f"切换到 data 分支失败：{r0.stderr}")
                return False
            log("已创建并切换到 data 分支")

        # git add data/
        r1 = subprocess.run(
            ["git", "add", "data/"],
            cwd=str(root), capture_output=True, text=True, timeout=15
        )
        if r1.returncode != 0:
            log(f"git add 失败：{r1.stderr}")
            return False

        # git commit
        msg = f"[小盘] {datetime.now():%Y-%m-%d %H:%M} 更新数据"
        r2 = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(root), capture_output=True, text=True, timeout=15
        )
        if r2.returncode != 0:
            # 可能是无变更，或者是其他错误
            if "nothing to commit" in r2.stdout.lower() or "nothing to commit" in r2.stderr.lower():
                log("无变更需要提交")
                return True
            log(f"git commit 失败：{r2.stderr}")
            return False

        log(f"已提交：{msg}")

        # git pull --rebase 再 push，处理可能的远程更新
        r3 = subprocess.run(
            ["git", "pull", "--rebase", "origin", "data"],
            cwd=str(root), capture_output=True, text=True, timeout=30
        )
        if r3.returncode != 0:
            if "no upstream branch" in r3.stderr.lower() or "No such file or directory" in r3.stderr:
                log("无上游分支，直接 push")
            else:
                log(f"git pull --rebase 失败：{r3.stderr}")
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=str(root), capture_output=True, text=True, timeout=10
                )
                return False

        r4 = subprocess.run(
            ["git", "push", "origin", "data"],
            cwd=str(root), capture_output=True, text=True, timeout=30
        )
        if r4.returncode != 0:
            log(f"git push 失败：{r4.stderr}")
            return False

        log("Git 推送到 data 分支成功")

        # 切回 main 分支（避免后续操作在 data 分支上）
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(root), capture_output=True, text=True, timeout=10
        )
        return True

    except subprocess.TimeoutExpired:
        log("Git 操作超时")
        return False
    except Exception as exc:
        log(f"Git 自动提交异常：{exc}")
        return False

def main() -> None:
    parser = argparse.ArgumentParser(description="A股监控系统主入口")
    parser.add_argument(
        "--mode",
        choices=["select", "monitor", "report"],
        required=True,
        help="运行模式：select=选股 / monitor=自选股异动监控 / report=收盘日报",
    )
    parser.add_argument("--config", default="config.json", help="配置文件路径")
    parser.add_argument(
        "--max-stocks",
        type=int,
        default=50,
        help="select 模式粗筛后拉取历史数据的股票上限（默认 50）",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="跳过 Git 自动提交",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    log(f"启动模式：{args.mode}")
    if args.mode == "select":
        run_select(cfg, args.max_stocks)
    elif args.mode == "monitor":
        run_monitor(cfg)
    else:
        run_report(cfg)
    log("流程结束")

    # Git 自动提交：仓库根目录为 backend 的父目录
    if not args.no_git:
        repo_root = Path(__file__).resolve().parent.parent
        git_commit_push(str(repo_root))


if __name__ == "__main__":
    main()
