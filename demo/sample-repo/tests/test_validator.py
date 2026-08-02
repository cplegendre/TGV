from demo_app.validator import validate_record


def test_valid_record() -> None:
    assert validate_record({"name": "Ada", "active": "true"})
