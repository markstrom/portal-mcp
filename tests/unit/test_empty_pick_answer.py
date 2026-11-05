"""A tool call whose items are all empty after pick must fail with a clear error,
not return empty objects. Items with the picked fields, and empty lists, pass."""

from __future__ import annotations

import httpx
import pytest

from portal_mcp.manifest import ToolConfig
from portal_mcp.service import ServiceClient, ServiceError


class _Manifest:
    name = "fake"
    redact = None


def _client() -> ServiceClient:
    client = ServiceClient.__new__(ServiceClient)
    client.manifest = _Manifest()
    return client


def _tool(pick: list[str]) -> ToolConfig:
    return ToolConfig.model_validate(
        {"name": "search", "kind": "read", "path": "/s", "response": "json", "pick": pick}
    )


def _response(body: object) -> httpx.Response:
    return httpx.Response(200, json=body, headers={"content-type": "application/json"})


def test_items_without_picked_fields_raise_an_error() -> None:
    body = [{"name": "A", "url": "/a"}, {"name": "B", "url": "/b"}]
    with pytest.raises(ServiceError, match="none had the fields in pick"):
        _client()._shape_json(_tool(["item.name", "item.url"]), _response(body))


def test_items_with_picked_fields_are_returned() -> None:
    body = [{"item": {"name": "A", "url": "/a"}}, {"item": {"name": "B", "url": "/b"}}]
    result = _client()._shape_json(_tool(["item.name", "item.url"]), _response(body))
    assert result == [{"item": {"name": "A", "url": "/a"}}, {"item": {"name": "B", "url": "/b"}}]


def test_an_empty_list_is_not_an_error() -> None:
    assert _client()._shape_json(_tool(["item.name"]), _response([])) == []


def test_a_fallback_path_that_matches_is_returned() -> None:
    body = [{"name": "A"}, {"name": "B"}]
    result = _client()._shape_json(_tool(["item.name|name"]), _response(body))
    # Values are written at the first alternative's path, so both shapes come out the same.
    assert result == [{"item": {"name": "A"}}, {"item": {"name": "B"}}]
