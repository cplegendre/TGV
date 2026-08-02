from pathlib import Path

import pytest


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def validate_token(token: str) -> bool:\n    return bool(token)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_auth.py").write_text(
        (
            "from src.auth import validate_token\n\n"
            "def test_token():\n"
            "    assert validate_token('x')\n"
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    return tmp_path
