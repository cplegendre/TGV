# mypy: ignore-errors
"""Application service."""

from demo_app.parser import parse_record
from demo_app.validator import validate_record


def accept(line: str) -> bool:
    return validate_record(parse_record(line))
