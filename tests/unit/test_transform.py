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
