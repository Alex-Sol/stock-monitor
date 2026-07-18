"""WeChat Work (企业微信) notifier — 支持群机器人(webhook) 和 自建应用 两种模式.

群机器人：配置 webhook_url 即可（如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx）
自建应用：配置 wechat_app 字段（corp_id, agent_id, secret, user_id），通过 access_token 发送消息到指定用户.

两种模式在 config.json 中互斥配置，notifier 自动识别.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

MAX_CONTENT_BYTES = 4096
"""企业微信消息内容上限（UTF-8 字节）."""

REQUEST_TIMEOUT = 10
"""HTTP 超时（秒）."""

TOKEN_EXPIRE_SECONDS = 7200
"""access_token 有效期，留 300 秒缓冲."""


# ----------------------------------------------------------------------
# 配置识别
# ----------------------------------------------------------------------

def _is_webhook_mode(cfg: Dict) -> bool:
    url = cfg.get("webhook_url", "")
    return bool(url) and url.startswith("http")


def _get_app_cfg(cfg: Dict) -> Optional[Dict]:
    app = cfg.get("wechat_app")
    if app and app.get("corp_id") and app.get("secret"):
        return app
    return None


# ----------------------------------------------------------------------
# 应用模式：access_token 管理
# ----------------------------------------------------------------------

def _load_token_cache(corp_id: str) -> Optional[str]:
    cache_path = Path(f"/tmp/wechat_token_{corp_id}.json")
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if time.time() < data.get("expires_at", 0):
            return data.get("token")
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_token_cache(corp_id: str, token: str) -> None:
    cache_path = Path(f"/tmp/wechat_token_{corp_id}.json")
    cache_path.write_text(
        json.dumps({
            "token": token,
            "expires_at": time.time() + TOKEN_EXPIRE_SECONDS - 300
        }, ensure_ascii=False),
        encoding="utf-8"
    )


def _get_access_token(corp_id: str, secret: str) -> Optional[str]:
    """获取企业微信 access_token，带文件缓存."""
    cached = _load_token_cache(corp_id)
    if cached:
        return cached

    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": corp_id, "corpsecret": secret}
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            token = data["access_token"]
            _save_token_cache(corp_id, token)
            return token
        print(f"[notifier] gettoken error: {data}")
    except requests.RequestException as exc:
        print(f"[notifier] Failed to get access_token: {exc}")
    return None


def _send_app_message(
    content: str,
    corp_id: str,
    agent_id: str,
    secret: str,
    user_id: str,
    msg_type: str = "markdown"
) -> bool:
    """通过企业微信应用发送消息到指定用户."""
    token = _get_access_token(corp_id, secret)
    if not token:
        return False

    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"

    # 企业微信应用消息不支持 markdown，用 text 类型，内容前加标记
    payload = {
        "touser": user_id,
        "msgtype": "text",
        "agentid": agent_id,
        "text": {"content": content},
        "safe": 0
    }

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") == 0:
            return True
        # token 过期，清缓存重试一次
        if data.get("errcode") == 42001:
            Path(f"/tmp/wechat_token_{corp_id}.json").unlink(missing_ok=True)
            token = _get_access_token(corp_id, secret)
            if token:
                resp = requests.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                    json=payload,
                    timeout=REQUEST_TIMEOUT
                )
                data = resp.json()
                return data.get("errcode") == 0
        print(f"[notifier] App message error: {data}")
        return False
    except requests.RequestException as exc:
        print(f"[notifier] Failed to send app message: {exc}")
        return False


# ----------------------------------------------------------------------
# 群机器人模式
# ----------------------------------------------------------------------

def _post_markdown(content: str, webhook_url: str) -> bool:
    """POST markdown 到群机器人 webhook."""
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
    except ValueError as exc:
        print(f"[notifier] Unexpected webhook response: {exc}")
        return False


def _send_webhook_chunks(chunks: List[str], webhook_url: str) -> bool:
    all_ok = True
    for chunk in chunks:
        if not _post_markdown(chunk, webhook_url):
            all_ok = False
    return all_ok


# ----------------------------------------------------------------------
# 文本分块
# ----------------------------------------------------------------------

def _split_text_by_bytes(text: str, max_bytes: int) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_bytes = 0

    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > max_bytes:
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


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# ----------------------------------------------------------------------
# 公共 API
# ----------------------------------------------------------------------

def send_wechat_alert(alerts: List[dict], cfg: Dict) -> bool:
    """发送异动提醒.

    Parameters
    ----------
    alerts : list of dict
    cfg : dict
        完整配置（含 webhook_url 或 wechat_app）
    """
    if not alerts:
        return True

    lines = ["【股票异动提醒】\n"]
    for i, alert in enumerate(alerts, 1):
        level = str(alert.get("level", ""))
        price = _safe_float(alert.get("price"))
        change_pct = _safe_float(alert.get("change_pct"))
        lines.append(
            f"{i}. [{level}] {alert.get('code', '')} {alert.get('name', '')} "
            f"价格 {price:.2f} 涨跌幅 {change_pct:+.2f}% "
            f"{alert.get('reason', '')}\n"
        )

    content = "".join(lines)

    if _is_webhook_mode(cfg):
        chunks = _split_text_by_bytes(content, MAX_CONTENT_BYTES)
        return _send_webhook_chunks(chunks, cfg["webhook_url"])

    app = _get_app_cfg(cfg)
    if app:
        return _send_app_message(
            content,
            corp_id=app["corp_id"],
            agent_id=app["agent_id"],
            secret=app["secret"],
            user_id=app["user_id"],
        )

    print("[notifier] 未配置 webhook_url 或 wechat_app，无法发送消息")
    return False


def send_daily_report(report_md: str, cfg: Dict) -> bool:
    """发送日报."""
    if not report_md:
        return True

    if _is_webhook_mode(cfg):
        chunks = _split_text_by_bytes(report_md, MAX_CONTENT_BYTES)
        return _send_webhook_chunks(chunks, cfg["webhook_url"])

    app = _get_app_cfg(cfg)
    if app:
        return _send_app_message(
            report_md,
            corp_id=app["corp_id"],
            agent_id=app["agent_id"],
            secret=app["secret"],
            user_id=app["user_id"],
        )

    print("[notifier] 未配置 webhook_url 或 wechat_app，无法发送消息")
    return False
