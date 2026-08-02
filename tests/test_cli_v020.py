from __future__ import annotations

import json
from pathlib import Path

from tgv.cli import main


def repo(tmp_path: Path) -> Path:
    (tmp_path / "mod.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    return tmp_path


def test_new_cli_commands(tmp_path: Path, capsys) -> None:
    root = repo(tmp_path)
    main(["profile", "fix helper bug", "--repo", str(root), "--json"])
    assert json.loads(capsys.readouterr().out)["task_category"] == "bug_fix"
    main(["retrieve", "helper", "--repo", str(root), "--json"])
    assert json.loads(capsys.readouterr().out)[0]["path"] == "mod.py"
    main(["scoreboard", "fix helper bug", "--repo", str(root), "--json"])
    assert json.loads(capsys.readouterr().out)["fallback_used"] is True
    main(["outcomes", "list", "--repo", str(root)])
    assert json.loads(capsys.readouterr().out) == []
    main(["outcomes", "export", "--repo", str(root), "--format", "csv"])
    assert capsys.readouterr().out == ""


def test_integrated_benchmark_cli(tmp_path: Path, capsys) -> None:
    root = repo(tmp_path)
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps(
            {
                "case_id": "x",
                "request": "find helper",
                "expected_task_category": "repository_investigation",
                "expected_complexity_band": "low",
                "acceptable_routes": ["local_fast"],
                "expected_risk_factors": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    main(["benchmark", "run", "--repo", str(root), "--cases", str(cases), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["total_cases"] == 1
