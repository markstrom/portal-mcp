"""Read tools on HTML pages: CSS extraction, embedded JSON, validation, and the analyzer's
text-free skeleton."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from portal_mcp.extract import ExtractError, embedded_json, extract
from portal_mcp.manifest import ToolConfig

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "portal-mcp-creator" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import analyze  # noqa: E402
from html_skeleton import skeleton  # noqa: E402

PAGE = """<!doctype html><title>Search</title>
<h1>Res<script>steal()</script>ults</h1>
<section class="results">
  <article class="hit"><h2 class="hit-name"><a href="/p/1">Testperson   Ett</a></h2>
    <span class="tag">red</span><span class="tag">blue</span><style>.x{}</style></article>
  <article class="hit"><h2 class="hit-name"><a href="/p/2">Testperson Två</a></h2></article>
  <article class="hit"><h2 class="hit-name"><a href="/p/3">Testperson Tre</a></h2></article>
</section>
<script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"result": {"items": [{"name": "Testperson Ett", "id": 1}]},
 "byId": {"person-12345": {"name": "Testperson Ett"}}}}}</script>
<script type="application/ld+json">{"@type": "Person", "name": "Testperson Ett"}</script>
<script type="application/ld+json">{"@type": "Place", "name": "Testgatan"}</script>
<script type="application/ld+json">not json</script>"""


def tool(**extra) -> ToolConfig:
    return ToolConfig.model_validate({"name": "search", "kind": "read", "path": "/search", "response": "html", **extra})


RECIPE = {
    "title": {"select": "h1"},
    "items": {"select": "article.hit", "fields": {
        "name": {"select": ".hit-name a"},
        "url": {"select": ".hit-name a", "attr": "href"},
        "tags": {"select": ".tag", "many": True},
        "missing": {"select": ".nothing"},
    }},
    "first_link": {"select": "a", "attr": "href"},
    "no_such_list": {"select": ".nope", "many": True},
    "no_such_value": {"select": ".nope"},
}


def test_css_extraction_gives_text_attributes_lists_and_nulls():
    result = extract(PAGE, tool(extract=RECIPE).extract)
    assert result["title"] == "Results"  # script content never becomes text
    assert result["items"][0] == {"name": "Testperson Ett", "url": "/p/1", "tags": ["red", "blue"], "missing": None}
    assert [i["name"] for i in result["items"]] == ["Testperson Ett", "Testperson Två", "Testperson Tre"]
    assert result["items"][1]["tags"] == []
    assert result["first_link"] == "/p/1"
    assert result["no_such_list"] == [] and result["no_such_value"] is None
    assert "steal" not in json.dumps(result) and ".x{}" not in json.dumps(result)


def test_too_large_pages_are_refused():
    with pytest.raises(ExtractError, match="larger than"):
        extract("<p>" + "x" * 2000 + "</p>", tool(extract=RECIPE).extract, max_bytes=1000)


def test_embedded_json_is_read_from_script_tags_only():
    data = embedded_json(PAGE, tool(embedded={"select": "script#__NEXT_DATA__", "path": "props.pageProps.result"}).embedded)
    assert data == {"items": [{"name": "Testperson Ett", "id": 1}]}
    ld = embedded_json(PAGE, tool(embedded={"select": 'script[type="application/ld+json"]', "many": True}).embedded)
    assert [d["@type"] for d in ld] == ["Person", "Place"]  # the invalid one is skipped
    assert embedded_json(PAGE, tool(embedded={"select": "h1"}).embedded) is None  # not a script tag
    assert embedded_json(PAGE, tool(embedded={"select": "script#__NEXT_DATA__", "path": "props.nope"}).embedded) is None


def test_html_tools_are_validated():
    with pytest.raises(ValidationError, match="exactly one of embedded and extract"):
        tool()
    with pytest.raises(ValidationError, match="exactly one of embedded and extract"):
        tool(extract=RECIPE, embedded={"select": "script#d"})
    with pytest.raises(ValidationError, match="need response: html"):
        ToolConfig.model_validate({"name": "x", "kind": "read", "path": "/x", "extract": RECIPE})
    with pytest.raises(ValidationError, match="only for read tools"):
        ToolConfig.model_validate({"name": "x", "kind": "write", "method": "POST", "path": "/x", "response": "html",
                                   "body_schema": {"type": "object"}, "extract": RECIPE})
    with pytest.raises(ValidationError, match="extra"):
        tool(extract={"a": {"select": "p", "evaluate": "js"}})


TEXTS = ["Testperson", "Testgatan", "Results", "red", "blue", "steal", "/p/1", "person-12345"]


def test_the_skeleton_has_structure_and_no_text():
    page = skeleton(PAGE)
    dumped = json.dumps(page, ensure_ascii=False)
    for text in TEXTS:
        assert text not in dumped, text
    selects = {e["select"]: e for e in page["embedded_json"]}
    assert set(selects) == {"script#__NEXT_DATA__", 'script[type="application/ld+json"]'}
    schema = selects["script#__NEXT_DATA__"]["schema"]
    assert schema["props"]["pageProps"]["result"]["items"] == [{"name": "string", "id": "integer"}]
    assert "<key>" in schema["props"]["pageProps"]["byId"]  # a key that looks like data is masked
    hit = next(p for p in page["repeated_elements"] if p["select"] == "article.hit")
    assert hit["count"] == 3 and "a" in hit["children"] and "href" in hit["attribute_names"]


def har_entry(path: str, mime: str, text: str, status: int = 200) -> dict:
    return {"request": {"method": "GET", "url": f"https://portal.example.com{path}"},
            "response": {"status": status, "content": {"mimeType": mime, "text": text}}}


def test_the_analyzer_lists_html_pages_with_a_skeleton_only():
    har = {"log": {"entries": [
        har_entry("/search?q=x", "text/html; charset=utf-8", PAGE),
        har_entry("/about", "text/html", "<p>Just text</p>"),
        har_entry("/api/items", "application/json", '{"items": []}'),
    ]}}
    catalog = analyze.analyze(har, "portal.example.com")
    by_path = {t["path"]: t for t in catalog["tools"]}
    assert by_path["/search"]["response"] == "html" and by_path["/search"]["query_params"] == ["q"]
    assert by_path["/search"]["html_skeleton"]["embedded_json"]
    assert "/about" not in by_path  # no embedded JSON and no repeated elements
    assert catalog["skipped"]["HTML without embedded JSON or repeated elements"] == 1
    assert "response" not in by_path["/api/items"]
    dumped = json.dumps(catalog, ensure_ascii=False)
    for text in TEXTS:
        assert text not in dumped, text
