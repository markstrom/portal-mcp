from portal_mcp.transform import pick, redact

DATA = {
    "items": [
        {"id": 1, "amount": 10, "owner": {"name": "A", "national_id": "111"}, "secret": "x"},
        {"id": 2, "amount": 20, "owner": {"name": "B", "national_id": "222"}, "secret": "y"},
    ],
    "page": 1,
}


def test_redact_removes_nested_fields_in_every_list_item():
    out = redact(DATA, ["items.owner.national_id"])
    assert all("national_id" not in item["owner"] for item in out["items"])
    assert out["items"][0]["owner"]["name"] == "A"


def test_redact_does_not_mutate_input():
    redact(DATA, ["items.secret"])
    assert DATA["items"][0]["secret"] == "x"


def test_redact_missing_path_is_a_no_op():
    assert redact(DATA, ["does.not.exist"]) == DATA


def test_pick_keeps_only_listed_fields_per_item():
    out = pick(DATA, ["items.id", "items.owner.name"])
    assert out == {
        "items": [{"id": 1, "owner": {"name": "A"}}, {"id": 2, "owner": {"name": "B"}}],
    }


def test_pick_top_level_list_response():
    out = pick([{"id": 1, "a": 2}, {"id": 2, "a": 3}], ["id"])
    assert out == [{"id": 1}, {"id": 2}]


def test_pick_missing_fields_are_omitted():
    assert pick({"id": 1}, ["id", "nope"]) == {"id": 1}


# ----- fallback paths -------------------------------------------------------

from portal_mcp.transform import has_content, unmatched_paths  # noqa: E402

SHAPES = [
    {"item": {"name": "Testperson Ett", "url": "/a", "address": {"city": "Teststad"}}},
    {"name": "Testperson Två", "url": "/b", "position": 2},
]


def test_a_fallback_path_uses_the_first_path_that_exists():
    out = pick(SHAPES, ["item.name|name", "item.url|url", "item.address.city|address.city"])
    # both shapes come out in the shape of the first path
    assert out == [
        {"item": {"name": "Testperson Ett", "url": "/a", "address": {"city": "Teststad"}}},
        {"item": {"name": "Testperson Två", "url": "/b"}},
    ]


def test_a_fallback_path_where_no_path_exists_leaves_the_field_out():
    assert pick(SHAPES, ["item.title|title"]) == [{}, {}]
    assert pick({"a": 1}, ["b|c.d"]) == {}


def test_fallbacks_are_tried_again_for_each_item_of_a_nested_list():
    data = {"result": {"items": [{"item": {"name": "N1"}}, {"name": "N2"}]}, "total": 2}
    assert pick(data, ["result.items.item.name|result.items.name", "total"]) == {
        "result": {"items": [{"item": {"name": "N1"}}, {"item": {"name": "N2"}}]},
        "total": 2,
    }


def test_paths_without_fallbacks_behave_as_before():
    assert pick(SHAPES, ["item.name"]) == [{"item": {"name": "Testperson Ett"}}, {}]


def test_empty_answers_and_unmatched_paths_are_recognized():
    assert not has_content([{}, {}]) and not has_content({}) and not has_content({"items": [{}]})
    assert not has_content(None) and not has_content("  ") and not has_content([])
    assert has_content([{}, {"a": 0}]) and has_content({"a": False})
    picked = pick(SHAPES, ["item.name|name", "item.zip"])
    assert unmatched_paths(picked, ["item.name|name", "item.zip"]) == ["item.zip"]
