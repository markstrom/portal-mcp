import portal_mcp.notify as notify_module


async def test_macos_uses_osascript_with_text_as_arguments(monkeypatch):
    calls = []

    async def fake_run(*command):
        calls.append(command)
        return True

    monkeypatch.delenv("PORTAL_MCP_NO_NOTIFY", raising=False)
    monkeypatch.setattr(notify_module.sys, "platform", "darwin")
    monkeypatch.setattr(notify_module.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(notify_module, "_run", fake_run)
    assert await notify_module.notify('Title "quoted"', "Message; rm -rf ~") is True
    command = calls[0]
    assert command[0] == "osascript"
    # the text is passed as argv, never spliced into the AppleScript source
    assert command[-2:] == ('Title "quoted"', "Message; rm -rf ~")
    assert all("rm -rf" not in part for part in command[:-2])


async def test_linux_prefers_notify_send(monkeypatch):
    calls = []

    async def fake_run(*command):
        calls.append(command)
        return True

    monkeypatch.delenv("PORTAL_MCP_NO_NOTIFY", raising=False)
    monkeypatch.setattr(notify_module.sys, "platform", "linux")
    monkeypatch.setattr(notify_module.shutil, "which", lambda name: "/usr/bin/notify-send" if name == "notify-send" else None)
    monkeypatch.setattr(notify_module, "_run", fake_run)
    await notify_module.notify("T", "M")
    assert calls[0][0] == "notify-send"


async def test_other_systems_use_desktop_notifier(monkeypatch):
    used = []

    async def fake_dn(title, message):
        used.append((title, message))
        return True

    monkeypatch.delenv("PORTAL_MCP_NO_NOTIFY", raising=False)
    monkeypatch.setattr(notify_module.sys, "platform", "win32")
    monkeypatch.setattr(notify_module, "_desktop_notifier", fake_dn)
    assert await notify_module.notify("T", "M") is True
    assert used == [("T", "M")]


async def test_notifications_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("PORTAL_MCP_NO_NOTIFY", "1")
    assert await notify_module.notify("T", "M") is False
