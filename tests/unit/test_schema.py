from portal_mcp.schema import compare, is_breaking, schema_of


def test_schema_has_types_not_values():
    schema = schema_of({"id": "secret-id", "n": 3, "x": 1.5, "ok": True, "none": None, "items": [{"a": "v"}], "e": []})
    assert schema == {
        "id": "string", "n": "integer", "x": "number", "ok": "boolean", "none": "null",
        "items": [{"a": "string"}], "e": ["empty"],
    }
    assert "secret-id" not in str(schema)


def test_compare_finds_removed_added_and_changed():
    old = {"items": [{"id": "string", "amount": "integer", "gone": "string"}], "page": "integer"}
    new = {"items": [{"id": "integer", "amount": "number", "fresh": "string"}], "page": "integer"}
    diff = compare(old, new)
    assert diff["removed"] == ["items[].gone"]
    assert diff["added"] == ["items[].fresh"]
    assert diff["changed"] == ["items[].id: string -> integer"]  # integer -> number is not a change
    assert is_breaking(diff)


def test_empty_lists_and_nulls_are_not_breaking():
    old = {"items": [{"id": "string"}], "note": "string"}
    new = {"items": ["empty"], "note": "null"}
    diff = compare(old, new)
    assert diff["nullable"] == ["note"]
    assert not is_breaking(diff)
