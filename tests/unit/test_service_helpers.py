from portal_mcp.service import RateLimited, RateLimiter, _filename_from, _query


def test_rate_limiter_blocks_over_limit_and_recovers():
    now = [0.0]
    limiter = RateLimiter(per_minute=2, clock=lambda: now[0])
    limiter.acquire()
    limiter.acquire()
    try:
        limiter.acquire()
    except RateLimited:
        pass
    else:
        raise AssertionError("third call inside one minute must be refused")
    now[0] = 61.0
    limiter.acquire()  # window has moved on


def test_query_encoding():
    assert _query({"page": 2, "flag": True, "tags": ["a", "b"], "gone": None}) == [
        ("page", "2"),
        ("flag", "true"),
        ("tags", "a"),
        ("tags", "b"),
    ]


def test_filename_from_content_disposition_is_sanitised():
    assert _filename_from('attachment; filename="invoice-1.pdf"') == "invoice-1.pdf"
    assert _filename_from("attachment; filename=../../etc/passwd") == "passwd"
    assert _filename_from("attachment; filename*=UTF-8''r%C3%A4kning.pdf") is not None
    assert _filename_from(None) is None
    assert _filename_from("inline") is None
