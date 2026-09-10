"""One-shot OAuth `state` tokens shared by the Spotify and Google flows.

A callback is only accepted when it carries a state this process issued within
the last few minutes, so a forged or replayed callback URL cannot bind someone
else's authorization code to this install."""
import secrets
import threading
import time

_TTL_SECONDS = 600
_states: dict[str, float] = {}
_lock = threading.Lock()


def issue() -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _states.items() if now - t > _TTL_SECONDS]:
            _states.pop(k, None)
        _states[token] = now
    return token


def verify(token: str | None) -> bool:
    """True once for a live token; False for unknown, used, expired or empty."""
    if not token:
        return False
    with _lock:
        issued = _states.pop(token, None)
    return issued is not None and time.monotonic() - issued <= _TTL_SECONDS
