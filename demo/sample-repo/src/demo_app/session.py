"""Synthetic authentication and session checks."""


def session_is_valid(token: str) -> bool:
    return token.startswith("demo-") and len(token) > 8
