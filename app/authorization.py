"""Deny-by-default route access and request rate limits.

Every API route declares its access level with `@access(...)`:
  public  - no sign-in (the public config and the password flows)
  member  - any active workspace user
  admin   - active users with the admin role
A route under /api without a declaration is a startup error, and the middleware refuses it anyway.
"""
import ipaddress
import threading
import time
from collections import OrderedDict, deque
from functools import cache
from pathlib import Path

from starlette.routing import Match

PUBLIC, MEMBER, ADMIN = "public", "member", "admin"
LEVELS = (PUBLIC, MEMBER, ADMIN)
ROLES = ("admin", "member")
ATTRIBUTE = "__studio_access__"


def access(level):
    if level not in LEVELS:
        raise ValueError(f"Unknown access level {level!r}")

    def mark(function):
        setattr(function, ATTRIBUTE, level)
        return function

    return mark


def route_access(app, scope):
    """The declared level of the route that will handle this request, or None when no route matches."""
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            endpoint = getattr(route, "endpoint", None)
            return getattr(endpoint, ATTRIBUTE, None) if endpoint is not None else None
    return None


def verify_registry(app, prefix="/api/"):
    """Fail at startup when an API route forgot to declare its access level."""
    missing = []
    for route in app.router.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if path.startswith(prefix) and endpoint is not None and getattr(endpoint, ATTRIBUTE, None) not in LEVELS:
            missing.append(path)
    if missing:
        raise RuntimeError("API routes without an access level: " + ", ".join(sorted(missing)))
    return True


def allows(level, role):
    """Does a user with `role` satisfy an access level?"""
    if level == PUBLIC:
        return True
    if level == MEMBER:
        return role in ROLES
    return role == "admin"


class RateLimiter:
    """A small in-memory sliding-window limiter keyed by caller and group.

    The cloud service runs one instance (maxScale 1), so process memory is the whole picture. Keys are capped so
    an attacker cannot grow the table without bound; when the table is full the least recently used key is dropped
    (never the whole table, so flooding new keys cannot reset everyone else's limits).
    """

    def __init__(self, max_keys=10000, clock=time.monotonic):
        self._hits = OrderedDict()
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._clock = clock

    def allow(self, key, limit, per_seconds):
        now = self._clock()
        with self._lock:
            window = self._hits.get(key)
            if window is None:
                if len(self._hits) >= self._max_keys:
                    self._evict(now)
                window = self._hits[key] = deque()
            self._hits.move_to_end(key)
            while window and window[0] <= now - per_seconds:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True

    def _evict(self, now):
        for key in [k for k, w in self._hits.items() if not w or w[-1] <= now - 3600]:
            del self._hits[key]
        while len(self._hits) >= self._max_keys:
            self._hits.popitem(last=False)


def client_ip(request, behind_proxy):
    """The caller's address as the cloud front end saw it, or the socket peer where no front end exists.

    Only a deployment behind a front end (cloud mode) reads X-Forwarded-For; without one the header is the caller's
    own text, so a local install uses the connection's address.

    Cloud Run's front end appends the address of whoever connected to it as the LAST X-Forwarded-For entry; anything
    before that was supplied by the caller (or by Firebase Hosting, which is itself a caller of the service) and can be
    forged. So the last entry is the only one a caller cannot choose. Behind Firebase Hosting it is a Hosting edge
    address shared by many people, which is why per-address limits here are coarse and per-account limits do the
    fine-grained work.
    """
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if behind_proxy and forwarded:
        return forwarded[-1][:64]
    return (request.client.host if request.client else "unknown")[:64]


@cache
def container_gateway():
    """Inside a Docker container, the bridge gateway's address; otherwise None.

    A port published on the host's loopback reaches the container from this address, while other computers keep their
    own address on the way in.
    """
    if not Path("/.dockerenv").exists():
        return None
    try:
        routes = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return None
    for line in routes:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return str(ipaddress.IPv4Address(int(fields[2], 16).to_bytes(4, "little")))
    return None


def local_peer(request):
    """Whether the socket peer is this computer. Local mode makes every caller the administrator, so it serves no one
    else. The Host header is the caller's choice, so only the connection's own address counts."""
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or host == container_gateway()
