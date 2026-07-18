"""Tests for notifier.py — intercepts requests.post, no real HTTP traffic.

Run with ``python test_notifier.py`` or ``pytest test_notifier.py``.
"""

from __future__ import annotations

import contextlib
from typing import List

import notifier
from notifier import send_daily_report, send_wechat_alert

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, errcode: int = 0) -> None:
        self.status_code = 200
        self._data = {"errcode": errcode, "errmsg": "ok" if errcode == 0 else "bad"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise notifier.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._data


@contextlib.contextmanager
def patched_post(fake):
    """Replace notifier.requests.post with *fake*, restore afterwards."""
    original = notifier.requests.post
    notifier.requests.post = fake
    try:
        yield
    finally:
        notifier.requests.post = original


def make_recorder(errcode: int = 0) -> tuple:
    """Return (calls, fake_post) where calls records every invocation."""
    calls: List[dict] = []

    def fake_post(url, json=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(errcode=errcode)

    return calls, fake_post


def sample_alerts() -> List[dict]:
    return [
        {
            "code": "000001", "name": "PingAn", "price": 12.5,
            "change_pct": 6.0, "reason": "Price surge: +6.00%",
            "level": "HIGH", "timestamp": "2026-07-17T10:00:00+08:00",
        },
        {
            "code": "600519", "name": "Moutai", "price": 1800.0,
            "change_pct": 1.2, "reason": "Volume surge: 5,000,000 vs avg 1,000,000",
            "level": "MEDIUM", "timestamp": "2026-07-17T10:01:00+08:00",
        },
        {
            "code": "000003", "name": "ZTE", "price": 9.5,
            "change_pct": -5.5, "reason": "Price plunge: -5.50%",
            "level": "HIGH", "timestamp": "2026-07-17T10:02:00+08:00",
        },
    ]


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_empty_alerts_no_request() -> None:
    calls, fake = make_recorder()
    with patched_post(fake):
        assert send_wechat_alert([], WEBHOOK) is True
    assert calls == [], "empty alerts must not trigger any HTTP request"


def test_alert_request_format() -> None:
    calls, fake = make_recorder()
    alerts = sample_alerts()
    with patched_post(fake):
        assert send_wechat_alert(alerts, WEBHOOK) is True

    assert len(calls) == 1
    call = calls[0]

    # (a) URL is passed through verbatim
    assert call["url"] == WEBHOOK
    assert call["timeout"] == 10

    # (b) JSON body structure
    body = call["json"]
    assert set(body.keys()) == {"msgtype", "markdown"}
    assert body["msgtype"] == "markdown"
    assert set(body["markdown"].keys()) == {"content"}

    content = body["markdown"]["content"]
    assert "**股票异动提醒**" in content
    # every alert appears with code, name, price, change, reason
    for a in alerts:
        assert a["code"] in content
        assert a["name"] in content
        assert a["reason"] in content
    assert "12.50" in content and "+6.00%" in content
    # HIGH is highlighted with the red/orange warning font
    assert '<font color="warning">[HIGH]</font>' in content
    assert '<font color="info">[MEDIUM]</font>' in content
    # utf-8 byte size within the limit
    assert len(content.encode("utf-8")) <= notifier.MAX_CONTENT_BYTES


def test_many_alerts_split() -> None:
    calls, fake = make_recorder()
    alerts = []
    for i in range(200):
        alerts.append({
            "code": f"{i:06d}", "name": f"Stock{i}", "price": 10.0 + i,
            "change_pct": 5.5, "reason": f"Price surge: +5.50% breakout {i}",
            "level": "HIGH" if i % 2 == 0 else "MEDIUM",
            "timestamp": "2026-07-17T10:00:00+08:00",
        })
    with patched_post(fake):
        assert send_wechat_alert(alerts, WEBHOOK) is True

    assert len(calls) > 1, "200 alerts should be split into multiple messages"
    joined = "".join(c["json"]["markdown"]["content"] for c in calls)
    for c in calls:
        content = c["json"]["markdown"]["content"]
        assert len(content.encode("utf-8")) <= notifier.MAX_CONTENT_BYTES
    for a in alerts:
        assert a["code"] in joined and a["name"] in joined


def test_long_report_split() -> None:
    calls, fake = make_recorder()
    # Build a report well beyond 4096 bytes (Chinese chars are 3 bytes each).
    lines = ["# 每日复盘日报\n"]
    for i in range(300):
        lines.append(f"第 {i:03d} 行：沪深300 收盘点评，涨跌互现，量能温和放大，北向资金净流入。\n")
    report = "".join(lines)
    assert len(report.encode("utf-8")) > notifier.MAX_CONTENT_BYTES

    with patched_post(fake):
        assert send_daily_report(report, WEBHOOK) is True

    assert len(calls) > 1
    joined = "".join(c["json"]["markdown"]["content"] for c in calls)
    for c in calls:
        body = c["json"]
        assert body["msgtype"] == "markdown"
        content = body["markdown"]["content"]
        assert len(content.encode("utf-8")) <= notifier.MAX_CONTENT_BYTES
        assert c["url"] == WEBHOOK
        assert c["timeout"] == 10
    # every report line survives the split exactly once
    for i in range(300):
        assert joined.count(f"第 {i:03d} 行") == 1


def test_webhook_error_returns_false() -> None:
    calls, fake = make_recorder(errcode=93000)
    with patched_post(fake):
        assert send_wechat_alert(sample_alerts(), WEBHOOK) is False


def test_network_exception_returns_false() -> None:
    def boom(url, json=None, timeout=None, **kwargs):
        raise notifier.requests.ConnectionError("connection refused")

    with patched_post(boom):
        assert send_wechat_alert(sample_alerts(), WEBHOOK) is False
        assert send_daily_report("# report", WEBHOOK) is False


def test_short_report_single_request() -> None:
    calls, fake = make_recorder()
    with patched_post(fake):
        assert send_daily_report("# 每日复盘\n一切正常。\n", WEBHOOK) is True
    assert len(calls) == 1
    assert calls[0]["json"]["markdown"]["content"] == "# 每日复盘\n一切正常。\n"


# ----------------------------------------------------------------------
# Plain-python runner
# ----------------------------------------------------------------------


def _run_all() -> None:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_all()
