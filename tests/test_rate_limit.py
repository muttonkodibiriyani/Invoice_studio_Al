from app.authorization import RateLimiter, allows, client_ip, ADMIN, MEMBER, PUBLIC


def test_sliding_window():
    clock = [1000.0]
    limiter = RateLimiter(clock=lambda: clock[0])
    assert all(limiter.allow("k", 3, 60) for _ in range(3))
    assert limiter.allow("k", 3, 60) is False
    clock[0] += 59
    assert limiter.allow("k", 3, 60) is False
    clock[0] += 2
    assert limiter.allow("k", 3, 60) is True
    assert limiter.allow("other", 3, 60) is True


def test_key_cap_evicts_idle_then_least_recently_used_keys():
    clock = [0.0]
    limiter = RateLimiter(max_keys=3, clock=lambda: clock[0])
    for key in ("a", "b", "c"):
        assert limiter.allow(key, 1, 60)
    clock[0] += 4000
    assert limiter.allow("d", 1, 60)
    assert list(limiter._hits) == ["d"], "idle keys go first"
    # Everything fresh: only the least recently used key goes. Flooding new keys never resets a key that is in use.
    assert limiter.allow("e", 1, 60) and limiter.allow("f", 1, 60)
    assert limiter.allow("d", 1, 60) is False  # d is now the most recently used
    for key in ("g", "h", "i"):
        limiter.allow(key, 1, 60)
        assert len(limiter._hits) == 3
    assert "i" in limiter._hits


def test_client_ip_is_the_last_forwarded_entry():
    class Request:
        def __init__(self, forwarded=None, host="10.0.0.9"):
            self.headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
            self.client = type("Client", (), {"host": host})()
    assert client_ip(Request("198.51.100.7"), True) == "198.51.100.7"
    assert client_ip(Request("203.0.113.9 , 198.51.100.7"), True) == "198.51.100.7"
    assert client_ip(Request(" , "), True) == "10.0.0.9"
    assert client_ip(Request(), True) == "10.0.0.9"
    assert len(client_ip(Request("x" * 500), True)) == 64
    # Without a front end the header is the caller's own text: only the connection counts.
    assert client_ip(Request("198.51.100.7"), False) == "10.0.0.9"


def test_role_levels():
    assert allows(PUBLIC, None) and allows(MEMBER, "member") and allows(MEMBER, "admin") and allows(ADMIN, "admin")
    assert not allows(ADMIN, "member") and not allows(MEMBER, "guest")
