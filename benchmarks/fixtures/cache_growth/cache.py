"""Small fixture with a process-lifetime cache."""

_CACHE: dict[str, bytes] = {}


def render(key: str) -> bytes:
    if key not in _CACHE:
        _CACHE[key] = key.encode() * 1024
    return _CACHE[key]
