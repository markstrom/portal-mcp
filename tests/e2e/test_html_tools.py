"""HTML read tools against the mock service: same shape as JSON tools, self_test baselines,
and no script on the page ever runs."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from portal_mcp.login.base import LoginManager
from portal_mcp.registry import Registry
from portal_mcp.server import build_server
from tests.conftest import mock_manifest_yaml, write_manifest
from tests.e2e.test_runtime import FakeLogin
from tests.mock_service.data import PASSWORD, USERNAME

HTML_TOOLS = """  - name: search_html
    kind: read
    description: Search invoices on the HTML search page.
    response: html
    method: GET
    path: /search
    params:
      type: object
      properties:
        q: {type: string}
    test_args: {q: Testkund}
    extract:
      items:
        select: article.hit
        fields:
          name: {select: .hit-name a}
          url: {select: .hit-name a, attr: href}
          amount: {select: .hit-amount}
          missing: {select: .not-on-the-page}
  - name: search_embedded
    kind: read
    description: Search invoices, from the page's embedded data.
    response: html
    method: GET
    path: /search
    params:
      type: object
      properties:
        q: {type: string}
    test_args: {q: Testkund}
    embedded:
      select: "script#__NEXT_DATA__"
      path: props.pageProps.result
    pick: [total, items.id, items.name]
"""


@pytest.fixture
def setup(paths, store, downloads, mock_base_url):
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url, extra_tools=HTML_TOOLS))
    registry = Registry(paths, store, LoginManager(FakeLogin(), store), downloads)
    server, _ = build_server(registry)
    return {"server": server, "paths": paths, "base_url": mock_base_url}


def _json(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


async def test_html_tools_return_json_like_json_tools(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        css = _json(await client.call_tool("mockportal_search_html", {"q": "Testkund"}))
        embedded = _json(await client.call_tool("mockportal_search_embedded", {"q": "Testkund"}))
        listing = _json(await client.call_tool("mockportal_list_invoices", {"page_size": 50}))

    assert css["items"] and all(set(i) == {"name", "url", "amount", "missing"} for i in css["items"])
    assert css["items"][0]["missing"] is None and css["items"][0]["url"].startswith("/invoices/")
    assert embedded["total"] == len(embedded["items"]) == len(css["items"])
    assert set(embedded["items"][0]) == {"id", "name"}  # pick works on HTML tools too
    assert {i["id"] for i in embedded["items"]} <= {i["id"] for i in listing["items"]}

    # The page's script would have created a payment if it had run.
    async with httpx.AsyncClient(base_url=setup["base_url"]) as http:
        await http.post("/login", data={"username": USERNAME, "password": PASSWORD})
        assert (await http.get("/api/payments")).json()["items"] == []


async def test_self_test_saves_and_compares_html_baselines(setup):
    async with create_connected_server_and_client_session(setup["server"]) as client:
        first = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        status = {r["tool"]: r["status"] for r in first["results"]}
        assert status["search_html"] == "baseline_saved" and status["search_embedded"] == "baseline_saved"
        baseline = (setup["paths"].services / "mockportal.schemas.json").read_text()
        assert "Testkund" not in baseline  # structure only
        second = _json(await client.call_tool("self_test", {"service": "mockportal"}))
        status = {r["tool"]: r["status"] for r in second["results"]}
        assert status["search_html"] == "ok" and status["search_embedded"] == "ok"


LISTING_TOOL = """  - name: listings{suffix}
    kind: read
    description: Listings from the page's structured data.
    response: html
    method: GET
    path: /listings
    public: true
    params:
      type: object
      properties:
        q: {{type: string}}
      required: [q]
    test_args: {{q: {query}}}
    embedded:
      select: 'script[type="application/ld+json"]'
      path: itemListElement
    pick: [item.name|name, item.url|url, item.address.addressLocality]
"""


@pytest.fixture
def listings(paths, store, downloads, mock_base_url):
    tools = LISTING_TOOL.format(suffix="", query="mixed") + LISTING_TOOL.format(suffix="_none", query="none")
    write_manifest(paths.services, "mockportal", mock_manifest_yaml(mock_base_url, extra_tools=tools))
    registry = Registry(paths, store, LoginManager(FakeLogin(), store), downloads)
    server, _ = build_server(registry)
    return {"server": server, "paths": paths}


async def test_fallback_picks_give_results_for_both_item_shapes(listings):
    async with create_connected_server_and_client_session(listings["server"]) as client:
        nested = _json(await client.call_tool("mockportal_listings", {"q": "nested"}))
        flat = _json(await client.call_tool("mockportal_listings", {"q": "flat"}))
        mixed = _json(await client.call_tool("mockportal_listings", {"q": "mixed"}))
    assert nested == [{"item": {"name": "Testperson Ett", "url": "/p/1", "address": {"addressLocality": "Teststad"}}}]
    assert flat == [{"item": {"name": "Testperson Två", "url": "/p/2"}}]  # same shape as the nested one
    assert len(mixed) == 2 and all(i["item"]["name"] for i in mixed)


async def test_self_test_saves_a_real_baseline_and_flags_an_empty_page(listings):
    async with create_connected_server_and_client_session(listings["server"]) as client:
        out = _json(await client.call_tool("self_test", {"service": "mockportal"}))
    results = {r["tool"]: r for r in out["results"]}
    assert results["listings"]["status"] == "baseline_saved"
    assert results["listings_none"]["status"] == "empty"
    assert "listings_none" in out["summary"]
    saved = json.loads((listings["paths"].services / "mockportal.schemas.json").read_text())["tools"]
    assert "listings_none" not in saved  # no baseline for an empty answer
    assert saved["listings"]["schema"][0]["item"]["name"] == "string"
    assert "Testperson" not in json.dumps(saved)
