# mypy: ignore-errors
from demo_app.parser import parse_record


def test_parse_record() -> None:
    assert parse_record("Ada, TRUE") == {"name": "Ada", "active": "true"}
