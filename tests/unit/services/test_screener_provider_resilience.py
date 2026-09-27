"""Unit tests for screener_provider resilience layer.

Locks in 2026-05-20 hardening:
- Extended retry budget with jitter
- Stale-while-error cache fallback
- Actionable terminal error on persistent failure
"""
from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest

from tradingview_mcp.core.services import screener_provider as sp


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear cache + last-failure timestamp between tests."""
    with sp._SCREENER_CACHE_LOCK:
        sp._SCREENER_CACHE.clear()
    with sp._TA_FAILURE_LOCK:
        sp._LAST_TA_FAILURE_TS = 0.0
    yield


@pytest.fixture
def fast_retry(monkeypatch):
    """Shrink retry delays to keep tests fast."""
    monkeypatch.setenv("TRADINGVIEW_MCP_RETRY_DELAYS", "0.01,0.02,0.03")
    monkeypatch.setenv("TRADINGVIEW_MCP_TA_RETRY_DELAYS", "0.01,0.02,0.03")
    monkeypatch.setenv("TRADINGVIEW_MCP_RETRY_AFTER_CAP_S", "0.05")
    monkeypatch.setenv("TRADINGVIEW_MCP_RETRY_JITTER", "0")
    monkeypatch.setenv("TRADINGVIEW_MCP_FAILURE_COOLDOWN_S", "0")


def _empty_body_error() -> json.JSONDecodeError:
    """Same shape the empty-body cliff raises."""
    return json.JSONDecodeError("Expecting value", "", 0)


def test_is_transient_screener_error_catches_empty_body():
    assert sp._is_transient_screener_error(_empty_body_error()) is True
    assert sp._is_transient_screener_error(RuntimeError("Connection reset by peer")) is True
    assert sp._is_transient_screener_error(ValueError("totally unrelated")) is False


def test_is_transient_screener_error_catches_socket_timeouts():
    """Socket timeouts MUST be classified transient so retry layer fires
    (was causing 8-minute hangs before 2026-05-20 hardening)."""
    import socket as _socket
    assert sp._is_transient_screener_error(_socket.timeout()) is True
    assert sp._is_transient_screener_error(TimeoutError("call timed out")) is True
    assert sp._is_transient_screener_error(RuntimeError("Read timed out")) is True
    assert sp._is_transient_screener_error(RuntimeError("Max retries exceeded")) is True
    assert sp._is_transient_screener_error(RuntimeError("RemoteDisconnected")) is True


def test_socket_default_timeout_applied_on_import():
    """Module import must call socket.setdefaulttimeout() so urllib calls
    inside tradingview_ta/screener never hang indefinitely."""
    import socket as _socket
    t = _socket.getdefaulttimeout()
    assert t is not None
    assert t > 0
    assert t <= 60.0  # sanity: well below the 8-minute hang threshold


def test_retry_then_succeed(fast_retry):
    """Scanner that fails twice then succeeds should return final result."""
    calls = {"n": 0}

    class FakeQuery:
        def get_scanner_data(self, cookies=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _empty_body_error()
            return (1, "ok_df")

    total, df = sp._scan_with_retry(FakeQuery())
    assert (total, df) == (1, "ok_df")
    assert calls["n"] == 3


def test_persistent_failure_raises_runtime_error(fast_retry):
    """All retries fail and no cache → RuntimeError with actionable message."""
    class FakeQuery:
        def get_scanner_data(self, cookies=None):
            raise _empty_body_error()

    with pytest.raises(RuntimeError) as exc_info:
        sp._scan_with_retry(FakeQuery())

    msg = str(exc_info.value)
    assert "transient errors on all" in msg
    assert "scanner.tradingview.com" in msg
    assert "Wait" in msg


def test_stale_while_error_returns_cached_payload(fast_retry):
    """When upstream is dead, stale cached data should serve as fallback."""
    cache_key = ("indicators_v1", "EGX", ("EGX:ASCM",), "1D", None)
    sp._cache_set(cache_key, (1, "stale_df"))

    # Force the freshness window to be already expired so _cache_get misses,
    # but stale lookup still hits.
    with sp._SCREENER_CACHE_LOCK:
        ts, payload = sp._SCREENER_CACHE[cache_key]
        sp._SCREENER_CACHE[cache_key] = (ts - 120.0, payload)  # 2 min old

    class FakeQuery:
        def get_scanner_data(self, cookies=None):
            raise _empty_body_error()

    total, df = sp._scan_with_retry(FakeQuery(), cache_key=cache_key)
    assert (total, df) == (1, "stale_df")


def test_non_transient_error_propagates_immediately(fast_retry):
    """Non-transient errors must NOT be silenced by the retry layer."""
    class FakeQuery:
        def get_scanner_data(self, cookies=None):
            raise ValueError("schema mismatch")

    with pytest.raises(ValueError):
        sp._scan_with_retry(FakeQuery())


def test_jitter_within_band(monkeypatch):
    """_jittered must keep delays within ±jitter of the base value."""
    monkeypatch.setenv("TRADINGVIEW_MCP_RETRY_JITTER", "0.2")
    for _ in range(50):
        d = sp._jittered(10.0)
        assert 8.0 <= d <= 12.0


def test_resilient_ta_uses_fresh_cache_first(fast_retry, monkeypatch):
    """Fresh cache hit must skip upstream entirely."""
    call_count = {"n": 0}

    def fake_request(screener, interval, symbols, timeout):
        call_count["n"] += 1
        return {"EGX:ASCM": object()}

    monkeypatch.setattr(sp, "_ta_request", fake_request)

    # First call hits upstream
    sp.resilient_get_multiple_analysis("egypt", "1D", ["EGX:ASCM"])
    # Second call within TTL must hit cache
    sp.resilient_get_multiple_analysis("egypt", "1D", ["EGX:ASCM"])
    assert call_count["n"] == 1


def test_resilient_ta_passes_timeout_explicitly(fast_retry, monkeypatch):
    """requests has no default timeout, which would hang FOREVER on a stalled
    upstream (the original 8-minute hang root cause). The resilient wrapper
    MUST pass a bounded timeout down to the HTTP call."""
    captured = {}

    def fake_request(screener, interval, symbols, timeout):
        captured["timeout"] = timeout
        return {"EGX:ASCM": "ok"}

    monkeypatch.setattr(sp, "_ta_request", fake_request)

    sp.resilient_get_multiple_analysis("egypt", "1D", ["EGX:ASCM"])

    assert captured["timeout"] is not None, "timeout must be passed explicitly"
    assert 1.0 <= captured["timeout"] <= 60.0, f"timeout should be a sane value, got {captured['timeout']}"


def test_resilient_ta_returns_stale_on_persistent_failure(fast_retry, monkeypatch):
    """When all retries fail, stale-while-error must serve old result."""
    call_count = {"n": 0}

    def fake_request(screener, interval, symbols, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"EGX:ASCM": "good_payload"}
        raise sp.UpstreamHTTPError(200, "empty body", "")

    monkeypatch.setattr(sp, "_ta_request", fake_request)

    # Prime the cache with one successful call
    first = sp.resilient_get_multiple_analysis("egypt", "1D", ["EGX:ASCM"])
    assert first == {"EGX:ASCM": "good_payload"}

    # Manually expire the FRESH window so the next call must re-fetch,
    # but stale window still holds.
    cache_key = ("ta_multi_v1", "egypt", "1D", ("EGX:ASCM",))
    with sp._SCREENER_CACHE_LOCK:
        ts, payload = sp._SCREENER_CACHE[cache_key]
        sp._SCREENER_CACHE[cache_key] = (ts - 120.0, payload)

    # Upstream now broken; stale fallback should kick in
    result = sp.resilient_get_multiple_analysis("egypt", "1D", ["EGX:ASCM"])
    assert result == {"EGX:ASCM": "good_payload"}


# --- 2026-09-27: direct TA request with browser headers + status checks ----

class _FakeResp:
    def __init__(self, status=200, text="", headers=None, reason="OK"):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.reason = reason

    @property
    def ok(self):
        return 200 <= self.status_code < 400


def _tv_rows(symbols):
    from tradingview_ta.main import TradingView
    n = len(TradingView.indicators)
    return json.dumps({"totalCount": len(symbols),
                       "data": [{"s": s, "d": [1.0] * n} for s in symbols]})


def test_ta_request_uses_browser_headers_not_library_ua(monkeypatch):
    """The tradingview_ta/x user-agent is what got rejected during outages;
    the TA path must present the same headers as tradingview-screener."""
    import requests
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update(url=url, headers=headers, timeout=timeout, body=json)
        return _FakeResp(200, _tv_rows(["NASDAQ:AMRX"]))

    monkeypatch.setattr(requests, "post", fake_post)
    out = sp._ta_request("america", "1D", ["NASDAQ:AMRX"], 7.0)

    ua = {k.lower(): v for k, v in captured["headers"].items()}["user-agent"]
    assert "tradingview_ta" not in ua and "Mozilla" in ua
    assert captured["timeout"] == 7.0
    assert captured["url"] == "https://scanner.tradingview.com/america/scan"
    assert out["NASDAQ:AMRX"] is not None
    assert out["NASDAQ:AMRX"].indicators["close"] == 1.0


def test_ta_request_missing_symbol_maps_to_none(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _FakeResp(200, _tv_rows(["NASDAQ:AAPL"])))
    out = sp._ta_request("america", "1D", ["NASDAQ:AAPL", "NASDAQ:ZZZZ"], 5.0)
    assert out["NASDAQ:ZZZZ"] is None


@pytest.mark.parametrize("status,text,transient", [
    (429, "", True),
    (403, "<html>blocked</html>", True),
    (503, "", True),
    (200, "", True),                 # classic empty-body cliff
    (200, "<html>oops</html>", True),  # non-JSON 200
    (400, '{"error":"bad"}', False),   # caller error: must not be retried
])
def test_ta_request_classifies_http_failures(monkeypatch, status, text, transient):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(status, text, reason="x"))
    with pytest.raises(sp.UpstreamHTTPError) as ei:
        sp._ta_request("america", "1D", ["NASDAQ:AMRX"], 5.0)
    assert ei.value.status == status
    assert sp._is_transient_screener_error(ei.value) is transient


def test_ta_request_structured_error_is_not_transient(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _FakeResp(200, '{"totalCount":0,"error":"Unknown field"}'))
    with pytest.raises(ValueError):
        sp._ta_request("america", "1D", ["NASDAQ:AMRX"], 5.0)


def test_resilient_ta_retries_through_rate_limit_and_logs_status(fast_retry, monkeypatch, capsys):
    """A 429 with Retry-After must be retried (not surfaced as a bare
    JSONDecodeError) and the status must show up in the logs."""
    import requests
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResp(429, "rate limited", headers={"Retry-After": "30"}, reason="Too Many Requests")
        return _FakeResp(200, _tv_rows(["NASDAQ:AMRX"]))

    monkeypatch.setattr(requests, "post", fake_post)
    out = sp.resilient_get_multiple_analysis("america", "1D", ["NASDAQ:AMRX"])
    assert out["NASDAQ:AMRX"] is not None
    assert calls["n"] == 3
    err = capsys.readouterr().err
    assert "HTTP 429" in err and "Retry-After=30s" in err


def test_resilient_ta_terminal_error_names_last_status(fast_retry, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResp(403, "denied", reason="Forbidden"))
    with pytest.raises(RuntimeError) as ei:
        sp.resilient_get_multiple_analysis("america", "1D", ["NASDAQ:AMRX"])
    msg = str(ei.value)
    assert msg.startswith("Upstream TradingView scanner returned transient")
    assert "HTTP 403" in msg


def test_resilient_ta_non_transient_propagates_without_retry(fast_retry, monkeypatch):
    import requests
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _FakeResp(400, "bad request", reason="Bad Request")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(sp.UpstreamHTTPError):
        sp.resilient_get_multiple_analysis("america", "1D", ["NASDAQ:AMRX"])
    assert calls["n"] == 1


def test_screener_path_treats_http_429_as_transient():
    """tradingview-screener raises requests.HTTPError on non-2xx; a 429 there
    must be retried like the empty-body case."""
    import requests
    resp = _FakeResp(429, "slow down")
    err = requests.HTTPError("429 Client Error", response=resp)
    assert sp._is_transient_screener_error(err) is True
    resp400 = _FakeResp(400, "bad")
    assert sp._is_transient_screener_error(requests.HTTPError("400", response=resp400)) is False
