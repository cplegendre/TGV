import re
from pathlib import Path

import pytest

from tgv.config import default_config
from tgv.exceptions import RepositoryAccessError
from tgv.models import RepositoryConfig
from tgv.repository import Repository


def test_search_returns_line_evidence(sample_repo) -> None:
    config = default_config()
    repo = Repository(sample_repo, config.repository, config.privacy.deny_patterns)
    results = repo.search("validate_token")
    assert results
    assert results[0].path in {"src/auth.py", "tests/test_auth.py"}
    assert "validate_token" in results[0].excerpt


def test_denied_file_is_not_readable(sample_repo) -> None:
    config = default_config()
    repo = Repository(sample_repo, config.repository, config.privacy.deny_patterns)
    with pytest.raises(RepositoryAccessError):
        repo.read_text(".env")


def test_path_escape_is_rejected(sample_repo) -> None:
    config = default_config()
    repo = Repository(sample_repo, config.repository, config.privacy.deny_patterns)
    with pytest.raises(RepositoryAccessError):
        repo.read_text("../outside.txt")


def test_symlink_escaping_root_is_excluded(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    repository = Repository(tmp_path, RepositoryConfig(), [])
    assert "escape.txt" not in repository.list_files(100)
    with pytest.raises(RepositoryAccessError, match=re.escape("Path escapes repository root")):
        repository.read_text("escape.txt")


def test_file_over_maximum_bytes_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_text("x" * 2048, encoding="utf-8")
    repository = Repository(tmp_path, RepositoryConfig(maximum_file_bytes=1024), [])
    assert repository.list_files(100) == []
    with pytest.raises(RepositoryAccessError):
        repository.read_text("large.txt")


def test_non_utf8_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9")
    repository = Repository(tmp_path, RepositoryConfig(), [])
    with pytest.raises(RepositoryAccessError, match="UTF-8"):
        repository.read_text("latin1.txt")


def test_double_star_deny_pattern_matches_nested_and_root(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "secret.key").write_text("secret", encoding="utf-8")
    (tmp_path / "root.key").write_text("secret", encoding="utf-8")
    (tmp_path / "safe.txt").write_text("safe", encoding="utf-8")
    repository = Repository(tmp_path, RepositoryConfig(), ["**/*.key"])
    assert repository.list_files(100) == ["safe.txt"]


def test_binary_file_with_null_byte_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"header\x00payload")
    repository = Repository(tmp_path, RepositoryConfig(ignored_suffixes=[]), [])
    with pytest.raises(RepositoryAccessError, match="Binary file"):
        repository.read_text("binary.dat")


def test_dangling_symlink_is_not_listed(tmp_path: Path) -> None:
    link = tmp_path / "dangling.txt"
    try:
        link.symlink_to(tmp_path / "missing.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    repository = Repository(tmp_path, RepositoryConfig(), [])
    assert "dangling.txt" not in repository.list_files(100)


def test_search_skips_unreadable_text_candidate(tmp_path: Path) -> None:
    (tmp_path / "valid.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "invalid.txt").write_bytes(b"needle\xff")
    repository = Repository(tmp_path, RepositoryConfig(), [])
    results = repository.search("needle")
    assert [result.path for result in results] == ["valid.txt"]
