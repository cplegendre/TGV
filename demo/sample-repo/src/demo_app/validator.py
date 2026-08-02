"""Validation helpers."""


def validate_record(record: dict[str, str]) -> bool:
    return bool(record.get("name")) and record.get("active") in {"true", "false"}
