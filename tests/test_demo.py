from __future__ import annotations

import json
from pathlib import Path

import pytest

from tgv.cli import main
from tgv.demo import _expanded_task, _load_tasks, _task_count, render_demo, run_demo
from tgv.exceptions import TGVError
from tgv.models import Route


def test_demo_internal_and_artifacts(tmp_path: Path) -> None:
    result = run_demo(output=tmp_path)
    assert result.success
    assert result.tasks_processed == 450
    assert result.matched_routes == 450
    assert result.local_routes == 240
    assert result.frontier_routes == 210
    assert result.safety_escalations == 105
    assert result.local_share >= 0
    assert result.savings_tokens >= 0
    assert result.savings_ratio >= 0
    assert (tmp_path / "report.json").is_file()
    records = list((tmp_path / "telemetry").glob("*.jsonl"))
    assert len(records) == 450
    assert '"synthetic":true' in records[0].read_text(encoding="utf-8")
    assert '"source":"tgv_demo"' in records[0].read_text(encoding="utf-8")
    deep_tasks = [task for task in result.tasks if task.expected_route == Route.FRONTIER_DEEP]
    assert len(deep_tasks) == 105
    assert all(task.actual_route == Route.FRONTIER_DEEP for task in deep_tasks)


def test_demo_no_report(tmp_path: Path) -> None:
    result = run_demo(output=tmp_path, generate_report=False)
    assert not result.report_generated
    assert not (tmp_path / "report.json").exists()


def test_cli_demo_json_and_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["demo", "--json", "--output", str(tmp_path / "one")])
    first = json.loads(capsys.readouterr().out)
    main(["--demo", "--json", "--output", str(tmp_path / "two")])
    second = json.loads(capsys.readouterr().out)
    assert first["success"] is True
    volatile = {"output_directory", "elapsed_seconds", "tasks_per_second"}
    first_without_output = {key: value for key, value in first.items() if key not in volatile}
    second_without_output = {key: value for key, value in second.items() if key not in volatile}
    assert second_without_output == first_without_output
    assert "tasks" not in first
    assert first["tasks_processed"] == 450
    assert first["scale"] == "small"
    assert first["canonical_tasks"] == 450
    assert first["elapsed_seconds"] >= 0
    assert first["tasks_per_second"] >= 0


def test_demo_keep_data_and_default_cleanup(capsys: pytest.CaptureFixture[str]) -> None:
    kept = run_demo(keep_data=True)
    assert kept.output_directory is not None
    kept_path = Path(kept.output_directory)
    assert kept_path.is_dir()
    default = run_demo()
    assert default.output_directory is None
    text = render_demo(kept)
    assert "TGV synthetic demo" in text
    assert "Workspace:" in text
    assert "Result - PASS : 450/450 100.0% success" in text
    assert "Local-routed token share (proxy):" in text
    assert "Scale: small" in text
    assert "Routing throughput:" in text
    assert "local-fast-001" not in text
    import shutil

    shutil.rmtree(kept_path)


def test_demo_malformed_tasks(tmp_path: Path) -> None:
    (tmp_path / "sample-repo").mkdir()
    (tmp_path / "tasks.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(TGVError, match="Cannot parse demo tasks"):
        run_demo(output=tmp_path / "out", assets_root=tmp_path)


def test_demo_route_mismatch(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src" / "tgv" / "demo_assets"
    import shutil

    assets = tmp_path / "assets"
    shutil.copytree(source, assets)
    tasks = json.loads((assets / "tasks.json").read_text(encoding="utf-8"))
    tasks[0]["expected_route"] = "frontier_deep"
    (assets / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
    result = run_demo(output=tmp_path / "out", assets_root=assets)
    assert not result.success
    assert result.failed_routes == 1


def test_load_tasks_rejects_invalid_shape(tmp_path: Path) -> None:
    path = tmp_path / "tasks.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(TGVError, match="JSON array"):
        _load_tasks(path)
    path.write_text('[{"id": 1}]', encoding="utf-8")
    with pytest.raises(TGVError, match="requires string"):
        _load_tasks(path)


def test_demo_missing_repository(tmp_path: Path) -> None:
    (tmp_path / "tasks.json").write_text("[]", encoding="utf-8")
    with pytest.raises(TGVError, match="cannot be located"):
        run_demo(output=tmp_path / "out", assets_root=tmp_path)


def test_demo_unknown_route(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "src" / "tgv" / "demo_assets"
    import shutil

    assets = tmp_path / "assets-unknown"
    shutil.copytree(source, assets)
    tasks = json.loads((assets / "tasks.json").read_text(encoding="utf-8"))
    tasks[0]["expected_route"] = "unknown"
    (assets / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")
    with pytest.raises(TGVError, match="Unknown expected route"):
        run_demo(output=tmp_path / "out-unknown", assets_root=assets)


def test_demo_rejects_negative_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tgv.demo.basic_value_report",
        lambda _path: {"local_share": -1.0, "savings_tokens": 0, "savings_ratio": 0.0},
    )
    with pytest.raises(TGVError, match="Invalid demo report field"):
        run_demo(output=tmp_path / "negative")


def test_demo_reports_progress(tmp_path: Path) -> None:
    updates: list[tuple[int, int]] = []
    result = run_demo(
        output=tmp_path,
        progress=lambda completed, total: updates.append((completed, total)),
    )
    assert result.success
    assert updates[0] == (0, 450)
    assert updates[-1] == (450, 450)
    assert len(updates) == 451


def test_demo_result_can_include_task_details(tmp_path: Path) -> None:
    result = run_demo(output=tmp_path)
    payload = result.to_dict(include_tasks=True)
    assert len(payload["tasks"]) == 450
    first = payload["tasks"][0]
    assert first["expected_route"] == "local_fast"
    assert first["actual_route"] == "local_fast"


def test_demo_scale_presets_and_custom_count(tmp_path: Path) -> None:
    assert _task_count("small", None) == 450
    assert _task_count("medium", None) == 5_000
    assert _task_count("large", None) == 25_000
    assert _task_count("stress", None) == 100_000
    assert _task_count("small", 901) == 901

    result = run_demo(
        output=tmp_path,
        tasks=901,
        retain_task_details=False,
    )
    assert result.success
    assert result.scale == "custom"
    assert result.tasks_processed == 901
    assert result.canonical_tasks == 450
    assert result.tasks == ()
    assert result.matched_routes == 901
    assert result.tasks_per_second > 0


def test_demo_rejects_non_positive_task_count(tmp_path: Path) -> None:
    with pytest.raises(TGVError, match="greater than zero"):
        run_demo(output=tmp_path, tasks=0)


def test_demo_expands_canonical_tasks_deterministically() -> None:
    canonical = [
        {"id": "one", "request": "Summarize this module", "expected_route": "local_fast"}
    ]
    assert _expanded_task(canonical, 0) == canonical[0]
    assert _expanded_task(canonical, 1) == {
        "id": "one--variant-00001",
        "request": "Summarize this module",
        "expected_route": "local_fast",
    }


def test_cli_demo_custom_task_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["demo", "--json", "--tasks", "7", "--output", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks_processed"] == 7
    assert payload["scale"] == "custom"
    assert payload["matched_routes"] == 7


def test_demo_rejects_empty_task_set(tmp_path: Path) -> None:
    assets = tmp_path / "assets-empty"
    (assets / "sample-repo").mkdir(parents=True)
    (assets / "tasks.json").write_text("[]", encoding="utf-8")
    with pytest.raises(TGVError, match="must not be empty"):
        run_demo(output=tmp_path / "out-empty", assets_root=assets)
