"""self_test never saves an empty answer as a baseline."""

from __future__ import annotations

import json

from portal_mcp.login.base import LoginManager
from portal_mcp.manifest import Manifest
from portal_mcp.registry import Registry


class FakeClient:
    """Stands in for ServiceClient: one read tool whose answer the test chooses."""

    def __init__(self, answer, pick=None):
        tool = {"name": "search", "kind": "read", "description": "Search.", "path": "/search",
                "params": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                "test_args": {"q": "test"}}
        if pick:
            tool["pick"] = pick
        self.manifest = Manifest.model_validate({"name": "fake", "base_url": "https://fake.example.com",
                                                 "session": {"type": "none"}, "tools": [tool]})
        self.name = "fake"
        self.answer = answer

    def resolve_profile(self, profile):
        return profile or "default"

    async def call(self, name, arguments, *, use_cache=True):
        return self.answer


def registry(paths, store, downloads):
    return Registry(paths, store, LoginManager(None, store), downloads)


async def test_an_empty_answer_gets_status_empty_and_no_baseline(paths, store, downloads):
    reg = registry(paths, store, downloads)
    for answer in ([], {}, None, {"items": []}):
        out = await reg.self_test(FakeClient(answer), None, False)
        result = out["results"][0]
        assert result["status"] == "empty", answer
        assert "test_args" in result["reason"]
    assert not reg.baseline_path("fake").exists()
    assert "need attention" in out["summary"]


async def test_items_that_are_all_empty_after_pick_get_status_empty(paths, store, downloads):
    reg = registry(paths, store, downloads)
    client = FakeClient([{}, {}], pick=["item.name", "item.url|url"])
    result = (await reg.self_test(client, None, False))["results"][0]
    assert result["status"] == "empty" and "pick paths" in result["reason"]
    assert result["unmatched_pick"] == ["item.name", "item.url|url"]
    assert not reg.baseline_path("fake").exists()


async def test_a_real_answer_saves_a_baseline_and_an_empty_saved_one_is_replaced(paths, store, downloads):
    reg = registry(paths, store, downloads)
    reg.baseline_path("fake").parent.mkdir(parents=True, exist_ok=True)
    reg.baseline_path("fake").write_text(json.dumps({"version": 1, "tools": {"search": {"schema": [{}]}}}))
    client = FakeClient([{"item": {"name": "Testperson Ett"}}, {}], pick=["item.name", "item.zip"])
    result = (await reg.self_test(client, None, False))["results"][0]
    assert result["status"] == "baseline_saved"  # the old [{}] baseline did not count
    assert result["unmatched_pick"] == ["item.zip"]
    saved = json.loads(reg.baseline_path("fake").read_text())["tools"]["search"]["schema"]
    assert saved == [{"item": {"name": "string"}}, {}]  # both item shapes, as variants
    again = (await reg.self_test(client, None, False))["results"][0]
    assert again["status"] == "ok"
