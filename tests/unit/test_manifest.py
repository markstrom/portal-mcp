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
