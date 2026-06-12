from __future__ import annotations


HANDSHAKE_FILE = ".bucket-sync-handshake"


def extract_bearer(authorization: str | None) -> str | None:
    """Pull a token out of an `Authorization: Bearer <token>` header value.

    Returns None if the header is missing, malformed, or empty.
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None
