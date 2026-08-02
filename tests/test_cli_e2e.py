import json
from pathlib import Path

from tgv.cli import main


def test_cli_inspect_route_prepare_end_to_end(sample_repo: Path, tmp_path: Path, capsys) -> None:
    main(["inspect", str(sample_repo)])
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["file_count"] == 2

    main(["route", "Where is validate_token?", "--repo", str(sample_repo)])
    routed = json.loads(capsys.readouterr().out)
    assert routed["route"] == "local_fast"

    output = tmp_path / "bundle"
    main(
        [
            "prepare",
            "Fix validate_token",
            "--repo",
            str(sample_repo),
            "--output",
            str(output),
        ]
    )
    assert capsys.readouterr().out.strip() == str(output.resolve())
    assert (output / "bundle.json").is_file()
    assert (output / "handoff.md").is_file()


def test_cli_search_and_doctor(sample_repo: Path, capsys) -> None:
    main(["search", "validate_token", "--repo", str(sample_repo)])
    searched = json.loads(capsys.readouterr().out)
    assert searched[0]["path"] in {"src/auth.py", "tests/test_auth.py"}

    main(["doctor"])
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["config"] == "ok"
    assert isinstance(doctor["ollama_available"], bool)


def test_cli_route_explain(sample_repo: Path, capsys) -> None:
    main(["route", "Fix parser", "--repo", str(sample_repo), "--explain"])
    output = json.loads(capsys.readouterr().out)
    assert output["matched_category"] == "code"
    assert output["confidence"] == 0.72
