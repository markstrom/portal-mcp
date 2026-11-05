import pytest

from portal_mcp.manifest import Manifest, load_manifests

from tests.conftest import mock_manifest_yaml, write_manifest

BASE = "http://127.0.0.1:9"


def _valid_manifest(**overrides) -> dict:
    data = {
        "name": "example",
        "base_url": BASE,
        "login_url": BASE + "/login",
        "session": {"check": "/api/session"},
        "tools": [{"name": "list_items", "kind": "read", "method": "GET", "path": "/api/items"}],
    }
    data.update(overrides)
    return data


def test_valid_manifest_gets_defaults():
    manifest = Manifest.model_validate(_valid_manifest())
    assert manifest.enabled is True
    assert manifest.profiles == ["default"]
    assert manifest.rate_limit_per_minute == 30
    assert manifest.cache_seconds == 0
    assert manifest.session.type == "cookies"


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Bad-Name"},
        {"name": "1starts_with_digit"},
        {"profiles": []},
        {"profiles": ["Has Space"]},
        {"rate_limit_per_minute": 0},
        {"cache_seconds": 99999},
        {"tools": [{"name": "x", "kind": "delete", "path": "/x"}]},
        {"tools": [{"name": "Bad Name", "kind": "read", "path": "/x"}]},
        {"tools": [{"name": "x", "kind": "read", "path": "relative"}]},
        {
            "tools": [
                {"name": "dup", "kind": "read", "path": "/a"},
                {"name": "dup", "kind": "read", "path": "/b"},
            ]
        },
        {"session": {"type": "bearer", "check": "/api/session"}},
        {"session": {"check": "no-slash"}},
    ],
)
def test_invalid_manifests_are_rejected(overrides):
    with pytest.raises(ValueError):
        Manifest.model_validate(_valid_manifest(**overrides))


def test_bearer_with_source_is_accepted():
    manifest = Manifest.model_validate(
        _valid_manifest(session={"type": "bearer", "bearer_source": "localStorage:token", "check": "/api/session"})
    )
    assert manifest.session.bearer_source == "localStorage:token"


def test_broken_manifest_does_not_affect_others(tmp_path):
    services = tmp_path / "services"
    write_manifest(services, "good", mock_manifest_yaml(BASE).replace("mockportal", "good"))
    write_manifest(services, "broken", "name: broken\nbase_url: [not, a, string]\n")
    write_manifest(services, "mismatch", mock_manifest_yaml(BASE).replace("mockportal", "other"))
    write_manifest(services, "disabled", mock_manifest_yaml(BASE).replace("mockportal", "disabled").replace("enabled: true", "enabled: false"))

    result = load_manifests(services)

    assert set(result.manifests) == {"good"}
    assert set(result.errors) == {"broken", "mismatch"}  # disabled is skipped silently
    assert "does not match" in result.errors["mismatch"]


def test_missing_services_directory_is_empty(tmp_path):
    result = load_manifests(tmp_path / "nope")
    assert result.manifests == {} and result.errors == {}


def _tool(**overrides) -> dict:
    tool = {"name": "do_it", "kind": "write", "method": "POST", "path": "/api/do",
            "body_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}
    tool.update(overrides)
    return tool


@pytest.mark.parametrize(
    "tool,message",
    [
        (_tool(method="GET"), "cannot use GET"),
        (_tool(body_schema=None), "needs body_schema"),
        (_tool(kind="binding"), "needs limits"),
        (_tool(kind="read"), "must use GET"),
        ({"name": "r", "kind": "read", "path": "/x", "messaging": True}, "messaging only applies"),
        (_tool(kind="binding", limits={"maximum": {"x": 1}}), "Extra inputs"),
    ],
)
def test_write_tool_rules(tool, message):
    with pytest.raises(ValueError, match=message):
        Manifest.model_validate(_valid_manifest(tools=[tool]))


def test_delete_write_needs_no_body_schema():
    manifest = Manifest.model_validate(_valid_manifest(tools=[_tool(method="DELETE", body_schema=None)]))
    assert manifest.tools[0].is_write


def test_messaging_tools_are_off_by_default():
    off = Manifest.model_validate(_valid_manifest(tools=[_tool(messaging=True)])).tools[0]
    on = Manifest.model_validate(_valid_manifest(tools=[_tool(messaging=True, enabled=True)])).tools[0]
    disabled = Manifest.model_validate(_valid_manifest(tools=[_tool(enabled=False)])).tools[0]
    assert off.exposed is False and on.exposed is True and disabled.exposed is False


def test_login_hosts_default_to_empty():
    assert Manifest.model_validate(_valid_manifest()).login_hosts == []


def test_valid_login_hosts_are_accepted():
    manifest = Manifest.model_validate(_valid_manifest(login_hosts=["login.example.com", "id.example.org"]))
    assert manifest.login_hosts == ["login.example.com", "id.example.org"]


@pytest.mark.parametrize(
    "host",
    [
        "https://login.example.com",
        "login.example.com/path",
        "login.example.com:443",
        "Login.Example.com",
        "127.0.0.1",
        "::1",
        "localhost",
        "auth.localhost",
        "nodot",
        "-bad.example.com",
        "",
    ],
)
def test_invalid_login_hosts_are_rejected(host):
    with pytest.raises(ValueError):
        Manifest.model_validate(_valid_manifest(login_hosts=[host]))


def test_login_hosts_reject_duplicates_and_the_service_host():
    with pytest.raises(ValueError, match="twice"):
        Manifest.model_validate(_valid_manifest(login_hosts=["login.example.com", "login.example.com"]))
    service = Manifest.model_validate(_valid_manifest()).base_url
    with pytest.raises(ValueError, match="base_url"):
        Manifest.model_validate(
            _valid_manifest(base_url="https://app.example.com", login_url="https://app.example.com/login",
                            login_hosts=["app.example.com"])
        )
    assert service  # the default fixture stays valid
