"""WeChat Work (企业微信) webhook notifier for A-share stock monitoring.

Sends alert digests and daily reports to a WeChat Work group robot
webhook using ``markdown`` messages.  Message content is capped at
4096 UTF-8 bytes per request (WeChat Work limit); longer payloads are
split into multiple messages automatically.
"""

from __future__ import annotations

from typing import Dict, List

import requests

MAX_CONTENT_BYTES = 4096
"""WeChat Work markdown content limit (UTF-8 bytes per message)."""

REQUEST_TIMEOUT = 10
"""HTTP timeout (seconds) for webhook POSTs."""


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def send_wechat_alert(alerts: List[dict], webhook_url: str) -> bool:
    """Format *alerts* as a WeChat Work markdown message and POST it.

    Parameters
    ----------
    alerts : list of dict
        Each dict has keys ``code``, ``name``, ``price``,
        ``change_pct``, ``reason``, ``level`` (``"HIGH"`` /
        ``"MEDIUM"``), ``timestamp`` — as produced by
        :meth:`alert_engine.AlertEngine.process_alerts`.
    webhook_url : str
        WeChat Work group robot webhook URL.

    Returns
    -------
    bool
        ``True`` when every message chunk was delivered successfully,
        ``False`` when any POST failed.  An empty *alerts* list sends
        nothing and returns ``True``.
    """
    if not alerts:
        return True

    lines = ["**股票异动提醒**\n"]
    for i, alert in enumerate(alerts, 1):
        level = str(alert.get("level", ""))
        color = "warning" if level == "HIGH" else "info"
        price = _safe_float(alert.get("price"))
        change_pct = _safe_float(alert.get("change_pct"))
        lines.append(
            f"> {i}. <font color=\"{color}\">[{level}]</font> "
            f"{alert.get('code', '')} {alert.get('name', '')} "
            f"价格 {price:.2f} 涨跌幅 {change_pct:+.2f}% "
            f"{alert.get('reason', '')}\n"
        )

    chunks = _split_text_by_bytes("".join(lines), MAX_CONTENT_BYTES)
    return _send_chunks(chunks, webhook_url)


def send_daily_report(report_md: str, webhook_url: str) -> bool:
    """POST the daily report Markdown to a WeChat Work webhook.

    The report is split at line boundaries so that no single message
    exceeds the 4096-byte WeChat Work markdown limit.

    Returns
    -------
    bool
        ``True`` when every chunk was delivered, ``False`` otherwise.
    """
    if not report_md:
        return True

    chunks = _split_text_by_bytes(report_md, MAX_CONTENT_BYTES)
    return _send_chunks(chunks, webhook_url)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _safe_float(value, default: float = 0.0) -> float:
    """Return *value* as float, falling back to *default* on failure."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _split_text_by_bytes(text: str, max_bytes: int) -> List[str]:
    """Split *text* into chunks each no longer than *max_bytes* UTF-8 bytes.

    Splits at line boundaries when possible; a single line longer than
    *max_bytes* is hard-split at character boundaries.
    """
    chunks: List[str] = []
    current: List[str] = []
    current_bytes = 0

    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))

        if line_bytes > max_bytes:
            # Flush what we have, then hard-split the oversized line.
            if current:
                chunks.append("".join(current))
                current, current_bytes = [], 0
            buf = ""
            buf_bytes = 0
            for ch in line:
                ch_bytes = len(ch.encode("utf-8"))
                if buf_bytes + ch_bytes > max_bytes:
                    chunks.append(buf)
                    buf, buf_bytes = "", 0
                buf += ch
                buf_bytes += ch_bytes
            if buf:
                chunks.append(buf)
            continue

        if current_bytes + line_bytes > max_bytes:
            chunks.append("".join(current))
            current, current_bytes = [], 0
        current.append(line)
        current_bytes += line_bytes

    if current:
        chunks.append("".join(current))
    return chunks


def _send_chunks(chunks: List[str], webhook_url: str) -> bool:
    """POST every chunk; return ``True`` only when all succeed."""
    all_ok = True
    for chunk in chunks:
        if not _post_markdown(chunk, webhook_url):
            all_ok = False
    return all_ok


def _post_markdown(content: str, webhook_url: str) -> bool:
    """POST one markdown message to *webhook_url*; never raises."""
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            print(f"[notifier] WeChat webhook error: {data}")
            return False
        return True
    except requests.RequestException as exc:
        print(f"[notifier] Failed to send WeChat message: {exc}")
        return False
    except ValueError as exc:  # invalid JSON in response
        print(f"[notifier] Unexpected webhook response: {exc}")
        return False
