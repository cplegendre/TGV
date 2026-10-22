from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tgv.efficiency import (
    ScoredSession,
    TERAnalysis,
    TERAnalysisError,
    TERUnavailableError,
    _ter_analyzer,
    score_sessions,
)

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="fake TER executables use POSIX shebang scripts"
)


def _fake_ter(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-ter"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "session-1",
        "aggregate_ter": 0.875,
        "aligned_tokens": 875,
        "waste_tokens": 125,
    }
    payload.update(overrides)
    return payload


def _emit(payload: object) -> str:
    return f"import sys\nsys.stdout.write({json.dumps(payload)!r})"


def _session(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"user"}\n', encoding="utf-8")
    return path


def test_valid_ter_json_output(tmp_path: Path) -> None:
    script = _fake_ter(tmp_path, _emit(_payload()))
    result = _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))
    assert result == TERAnalysis(aggregate_ter=0.875, aligned_tokens=875, waste_tokens=125)


def test_additional_json_fields_are_ignored(tmp_path: Path) -> None:
    payload = _payload(
        raw_ratio=0.5,
        classified_spans=[{"text": "SECRET SPAN TEXT"}],
        waste_summary={"total_waste_tokens": 125},
    )
    script = _fake_ter(tmp_path, _emit(payload))
    result = _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))
    assert result == TERAnalysis(aggregate_ter=0.875, aligned_tokens=875, waste_tokens=125)
    assert not hasattr(result, "classified_spans")
    assert "SECRET SPAN TEXT" not in repr(result)


@pytest.mark.parametrize("missing", ["aggregate_ter", "aligned_tokens", "waste_tokens"])
def test_missing_required_json_fields(tmp_path: Path, missing: str) -> None:
    payload = _payload()
    payload.pop(missing)
    script = _fake_ter(tmp_path, _emit(payload))

    with pytest.raises(TERAnalysisError, match=missing):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_invalid_json_response(tmp_path: Path) -> None:
    script = _fake_ter(tmp_path, "import sys\nsys.stdout.write('not-json')")
    with pytest.raises(TERAnalysisError, match="invalid JSON"):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_non_object_json_response(tmp_path: Path) -> None:
    script = _fake_ter(tmp_path, _emit([]))
    with pytest.raises(TERAnalysisError, match="non-object"):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_missing_ter_executable(tmp_path: Path) -> None:
    analyzer = _ter_analyzer(executable=str(tmp_path / "does-not-exist"), timeout=5)
    with pytest.raises(TERUnavailableError, match="tgv-agent\\[ter\\]"):
        analyzer(_session(tmp_path))


def test_default_executable_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tgv.efficiency.shutil.which", lambda _: None)
    with pytest.raises(TERUnavailableError, match="tgv-agent\\[ter\\]"):
        _ter_analyzer()


def test_default_executable_resolution_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _fake_ter(tmp_path, _emit(_payload()))
    monkeypatch.setattr("tgv.efficiency.shutil.which", lambda _: str(script))
    result = _ter_analyzer(timeout=5)(_session(tmp_path))
    assert result.aligned_tokens == 875


def test_nonzero_exit_status_with_short_diagnostic(tmp_path: Path) -> None:
    body = "import sys\nsys.stderr.write('bad flag')\nsys.exit(7)"
    script = _fake_ter(tmp_path, body)
    with pytest.raises(TERAnalysisError, match="exit code 7: bad flag"):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_nonzero_exit_status_without_diagnostic(tmp_path: Path) -> None:
    body = "import sys\nsys.exit(3)"
    script = _fake_ter(tmp_path, body)
    with pytest.raises(TERAnalysisError, match="exit code 3: <no diagnostic output>"):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_nonzero_exit_status_bounds_diagnostic_output(tmp_path: Path) -> None:
    body = "import sys\nsys.stderr.write('failure ' * 2000)\nsys.exit(9)"
    script = _fake_ter(tmp_path, body)
    with pytest.raises(TERAnalysisError) as exc_info:
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))
    message = str(exc_info.value)
    assert "exit code 9" in message
    assert "..." in message
    assert len(message) < 900


def test_ter_execution_timeout(tmp_path: Path) -> None:
    script = _fake_ter(tmp_path, "import time\ntime.sleep(5)")
    with pytest.raises(TERAnalysisError, match="timed out"):
        _ter_analyzer(executable=str(script), timeout=0.05)(_session(tmp_path))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("aggregate_ter", "0.9"),
        ("aggregate_ter", True),
        ("aggregate_ter", float("nan")),
        ("aligned_tokens", 1.5),
        ("aligned_tokens", True),
        ("aligned_tokens", -1),
        ("waste_tokens", "10"),
    ],
)
def test_malformed_numeric_values(tmp_path: Path, field: str, value: object) -> None:
    script = _fake_ter(tmp_path, _emit(_payload(**{field: value})))
    with pytest.raises(TERAnalysisError, match=field):
        _ter_analyzer(executable=str(script), timeout=5)(_session(tmp_path))


def test_subprocess_start_failure_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_run(*args: object, **kwargs: object) -> object:
        raise OSError("denied")

    monkeypatch.setattr("tgv.efficiency.subprocess.run", fail_run)
    with pytest.raises(TERAnalysisError, match="could not start"):
        _ter_analyzer(executable="ter", timeout=5)(_session(tmp_path))


def test_session_path_with_shell_metacharacters_is_not_interpreted(tmp_path: Path) -> None:
    body = (
        "import json, sys\n"
        "open(sys.argv[2] + '.seen', 'w', encoding='utf-8').write(sys.argv[2])\n"
        f"sys.stdout.write({json.dumps(_payload())!r})"
    )
    script = _fake_ter(tmp_path, body)
    session = tmp_path / "session; touch pwned.jsonl"
    session.write_text('{"type":"user"}\n', encoding="utf-8")

    result = _ter_analyzer(executable=str(script), timeout=5)(session)

    assert result.aligned_tokens == 875
    assert Path(f"{session}.seen").read_text(encoding="utf-8") == str(session.resolve())
    assert not (tmp_path / "pwned").exists()


def test_score_sessions_uses_cli_adapter(tmp_path: Path) -> None:
    log = tmp_path / "a.jsonl"
    log.write_text(json.dumps({"route_decision": {"route": "local_code"}}) + "\n", encoding="utf-8")
    script = _fake_ter(tmp_path, _emit(_payload()))

    sessions = score_sessions(
        tmp_path,
        analyzer=_ter_analyzer(executable=str(script), timeout=5),
    )

    assert sessions == [ScoredSession("local_code", 0.875, 875, 125)]
