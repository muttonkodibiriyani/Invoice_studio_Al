"""Encrypted subscription credentials used by the headless provider adapters."""

import re
import time


CLAUDE_SECRET = "claude_subscription"
_TOKEN = re.compile(r"^[\x21-\x7e]{32,8192}$")


class ClaudeSubscription:
    """Store a Claude setup token without ever returning it from status calls."""

    def __init__(self, store):
        self.store = store

    def connect(self, setup_token):
        if not isinstance(setup_token, str):
            raise ValueError("Claude setup token must be text")
        token = setup_token.strip()
        if not _TOKEN.fullmatch(token):
            raise ValueError("Claude setup token is invalid")
        self.store.secret(
            CLAUDE_SECRET,
            {
                "version": 1,
                "token": token,
                "connected_at": int(time.time()),
                "source": "claude setup-token",
            },
        )
        return {"connected": True, "provider": "claude_local"}

    def connected(self,c=None):
        saved = self.store.secret(CLAUDE_SECRET,c=c)
        return bool(isinstance(saved, dict) and _TOKEN.fullmatch(saved.get("token", "")))

    def token(self):
        saved = self.store.secret(CLAUDE_SECRET)
        token = saved.get("token") if isinstance(saved, dict) else None
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Connect a Claude subscription with a setup token first")
        return token

    def disconnect(self):
        self.store.secret(CLAUDE_SECRET, delete=True)
        return {"connected": False, "provider": "claude_local"}
