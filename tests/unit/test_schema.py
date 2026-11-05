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


def test_list_items_of_different_shapes_show_every_variant():
    from portal_mcp.schema import compare, has_fields, schema_of

    items = [
        {"item": {"name": "Testperson Ett", "url": "/a"}},
        {"name": "Testperson Två", "url": "/b", "position": 2},
        {"item": {"name": "Testperson Tre", "url": "/c"}},
    ]
    schema = schema_of(items)
    assert schema == [
        {"item": {"name": "string", "url": "string"}},
        {"name": "string", "position": "integer", "url": "string"},
    ]
    assert "Testperson" not in str(schema)
    # One shape still gives one variant, as saved baselines expect.
    assert schema_of(items[:1]) == [{"item": {"name": "string", "url": "string"}}]
    # A baseline of one variant is still compatible; the new variant is only noted.
    diff = compare(schema_of(items[:1]), schema)
    assert diff["removed"] == [] and diff["changed"] == []
    assert diff["added"] == ["[] variant ['name', 'position', 'url']"]
    # A variant that loses a field is a breaking change.
    diff = compare(schema, [{"item": {"name": "string"}}, {"name": "string", "position": "integer", "url": "string"}])
    assert diff["removed"] == ["[].item.url"]
    assert not has_fields([{}]) and not has_fields({}) and has_fields(schema)
