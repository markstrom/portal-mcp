"""The browser is always started with Chrome's sandbox on (no --no-sandbox)."""

from __future__ import annotations

from pathlib import Path

import pytest

from portal_mcp.login.base import LoginError
from portal_mcp.login.local import SANDBOX_FLAG, _launch


class _Chromium:
    def __init__(self, fail_chrome: bool = False, fail_all: bool = False):
        self.calls: list[dict] = []
        self.fail_chrome = fail_chrome
        self.fail_all = fail_all

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        self.calls.append(kwargs)
        if self.fail_all or (self.fail_chrome and kwargs.get("channel") == "chrome"):
            raise RuntimeError("cannot start")
        return "context"


class _Playwright:
    def __init__(self, chromium):
        self.chromium = chromium


async def test_sandbox_is_on_for_chrome(tmp_path: Path):
    chromium = _Chromium()
    assert await _launch(_Playwright(chromium), tmp_path, True, record_har_path="x.har") == "context"
    (call,) = chromium.calls
    assert call["channel"] == "chrome"
    assert call["chromium_sandbox"] is True
    assert SANDBOX_FLAG not in call["args"]
    assert call["record_har_path"] == "x.har"  # other options still pass through


async def test_sandbox_is_on_for_the_chromium_fallback(tmp_path: Path):
    chromium = _Chromium(fail_chrome=True)
    await _launch(_Playwright(chromium), tmp_path, True)
    assert len(chromium.calls) == 2
    assert all(c["chromium_sandbox"] is True and SANDBOX_FLAG not in c["args"] for c in chromium.calls)


async def test_callers_cannot_turn_the_sandbox_off(tmp_path: Path):
    chromium = _Chromium()
    with pytest.raises(LoginError, match="cannot be turned off"):
        await _launch(_Playwright(chromium), tmp_path, True, chromium_sandbox=False)
    assert chromium.calls == []
    await _launch(_Playwright(chromium), tmp_path, True, args=[SANDBOX_FLAG, "--lang=sv"])
    assert chromium.calls[0]["args"] == ["--lang=sv"]


async def test_no_silent_fallback_without_sandbox(tmp_path: Path):
    chromium = _Chromium(fail_all=True)
    with pytest.raises(LoginError, match="never starts the browser without the sandbox"):
        await _launch(_Playwright(chromium), tmp_path, True)
    assert len(chromium.calls) == 2
    assert all(c["chromium_sandbox"] is True for c in chromium.calls)
