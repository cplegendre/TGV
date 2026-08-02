from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from tgv.config import default_config
from tgv.efficiency import basic_value_report
from tgv.exceptions import TGVError
from tgv.models import Route
from tgv.repository import Repository
from tgv.router import Router
from tgv.telemetry import SessionRecorder

DemoScale = Literal["small", "medium", "large", "stress"]

_SCALE_TASKS: dict[DemoScale, int] = {
    "small": 450,
    "medium": 5_000,
    "large": 25_000,
    "stress": 100_000,
}

_TOKEN_ESTIMATES = {
    Route.LOCAL_FAST: 500,
    Route.LOCAL_CODE: 1500,
    Route.FRONTIER_STANDARD: 0,
    Route.FRONTIER_DEEP: 0,
}


@dataclass(frozen=True)
class DemoTaskResult:
    task_id: str
    request: str
    expected_route: Route
    actual_route: Route
    matched: bool
    reason: str | None


@dataclass(frozen=True)
class DemoResult:
    repository: str
    tasks_processed: int
    matched_routes: int
    failed_routes: int
    local_routes: int
    frontier_routes: int
    local_share: float
    savings_tokens: int
    savings_ratio: float
    safety_escalations: int
    report_generated: bool
    output_directory: str | None
    synthetic: bool
    success: bool
    scale: str
    canonical_tasks: int
    elapsed_seconds: float
    tasks_per_second: float
    tasks: tuple[DemoTaskResult, ...]

    def to_dict(self, *, include_tasks: bool = False) -> dict[str, Any]:
        data = asdict(self)
        if not include_tasks:
            data.pop("tasks")
            return data
        data["tasks"] = [
            {
                **asdict(task),
                "expected_route": task.expected_route.value,
                "actual_route": task.actual_route.value,
            }
            for task in self.tasks
        ]
        return data


def _assets_root() -> Path:
    candidate = resources.files("tgv.demo_assets")
    with resources.as_file(candidate) as path:
        resolved = path.resolve()
    if not resolved.is_dir():
        raise TGVError("Bundled demo repository cannot be located")
    return resolved


def _load_tasks(path: Path) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TGVError(f"Cannot parse demo tasks: {exc}") from exc
    if not isinstance(raw, list):
        raise TGVError("Demo tasks must be a JSON array")
    tasks: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("id", "request", "expected_route")
        ):
            raise TGVError("Each demo task requires string id, request, and expected_route")
        tasks.append({key: item[key] for key in ("id", "request", "expected_route")})
    return tasks


def _task_count(scale: DemoScale, tasks: int | None) -> int:
    if tasks is not None:
        if tasks <= 0:
            raise TGVError("Demo task count must be greater than zero")
        return tasks
    return _SCALE_TASKS[scale]


def _expanded_task(canonical_tasks: list[dict[str, str]], index: int) -> dict[str, str]:
    base = canonical_tasks[index % len(canonical_tasks)]
    cycle = index // len(canonical_tasks)
    if cycle == 0:
        return base
    return {
        "id": f"{base['id']}--variant-{cycle:05d}",
        "request": base["request"],
        "expected_route": base["expected_route"],
    }


def run_demo(
    *,
    output: Path | None = None,
    keep_data: bool = False,
    generate_report: bool = True,
    assets_root: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    scale: DemoScale = "small",
    tasks: int | None = None,
    retain_task_details: bool = True,
) -> DemoResult:
    assets = (assets_root or _assets_root()).resolve()
    source_repo = assets / "sample-repo"
    tasks_path = assets / "tasks.json"
    if not source_repo.is_dir():
        raise TGVError("Bundled demo repository cannot be located")

    temporary = output is None
    if output is None:  # noqa: SIM108
        workspace = Path(tempfile.mkdtemp(prefix="tgv-demo-"))
    else:
        workspace = output.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        repository_path = workspace / "sample-repo"
        shutil.copytree(source_repo, repository_path, dirs_exist_ok=True)
        config = default_config()
        config.routing.use_local_model = False
        config.telemetry.enabled = True
        config.telemetry.log_path = workspace / "telemetry"
        config.intelligence.outcome_database_path = workspace / "outcomes.sqlite3"
        config.repository.index_path = workspace / "index.json"

        repository = Repository(repository_path, config.repository, config.privacy.deny_patterns)
        repository.list_files()
        router = Router(config.routing, provider=None)
        recorder = SessionRecorder(config.telemetry.log_path)
        task_results: list[DemoTaskResult] = []
        safety_escalations = 0
        matched_routes = 0
        local_routes = 0
        canonical_tasks = _load_tasks(tasks_path)
        if not canonical_tasks:
            raise TGVError("Demo task set must not be empty")
        total_tasks = _task_count(scale, tasks)
        if progress is not None:
            progress(0, total_tasks)
        started = time.perf_counter()
        for completed in range(1, total_tasks + 1):
            task = _expanded_task(canonical_tasks, completed - 1)
            try:
                expected = Route(task["expected_route"])
            except ValueError as exc:
                raise TGVError(f"Unknown expected route for {task['id']}") from exc
            decision = router.deterministic(task["request"])
            matched = decision.route == expected
            high_risk = "high_risk" in decision.matched_signals
            if high_risk:
                safety_escalations += 1
                if decision.route != Route.FRONTIER_DEEP:
                    matched = False
            recorder.record(
                task["request"],
                decision,
                metadata={"synthetic": True, "source": "tgv_demo", "task_id": task["id"]},
                usage_tokens=_TOKEN_ESTIMATES[decision.route],
            )
            if matched:
                matched_routes += 1
            if decision.route in {Route.LOCAL_FAST, Route.LOCAL_CODE}:
                local_routes += 1
            if retain_task_details:
                task_results.append(
                    DemoTaskResult(
                        task_id=task["id"],
                        request=task["request"],
                        expected_route=expected,
                        actual_route=decision.route,
                        matched=matched,
                        reason=None if matched else "route mismatch",
                    )
                )
            if progress is not None:
                progress(completed, total_tasks)

        elapsed_seconds = time.perf_counter() - started
        tasks_per_second = total_tasks / elapsed_seconds if elapsed_seconds > 0 else 0.0

        metrics = basic_value_report(config.telemetry.log_path)
        for field in ("local_share", "savings_tokens", "savings_ratio"):
            if field not in metrics or float(metrics[field]) < 0:
                raise TGVError(f"Invalid demo report field: {field}")
        if generate_report:
            (workspace / "report.json").write_text(
                json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        result = DemoResult(
            repository="demo/sample-repo",
            tasks_processed=total_tasks,
            matched_routes=matched_routes,
            failed_routes=total_tasks - matched_routes,
            local_routes=local_routes,
            frontier_routes=total_tasks - local_routes,
            local_share=float(metrics["local_share"]),
            savings_tokens=int(metrics["savings_tokens"]),
            savings_ratio=float(metrics["savings_ratio"]),
            safety_escalations=safety_escalations,
            report_generated=generate_report,
            output_directory=str(workspace) if (keep_data or output is not None) else None,
            synthetic=True,
            success=matched_routes == total_tasks,
            scale="custom" if tasks is not None else scale,
            canonical_tasks=len(canonical_tasks),
            elapsed_seconds=round(elapsed_seconds, 4),
            tasks_per_second=round(tasks_per_second, 2),
            tasks=tuple(task_results),
        )
        return result
    finally:
        if temporary and not keep_data:
            shutil.rmtree(workspace, ignore_errors=True)


def render_demo(result: DemoResult) -> str:
    passed_percent = (
        result.matched_routes / result.tasks_processed * 100 if result.tasks_processed else 0.0
    )
    failed_percent = (
        result.failed_routes / result.tasks_processed * 100 if result.tasks_processed else 0.0
    )
    lines = [
        "TGV synthetic demo",
        "",
        f"Repository: {result.repository}",
        f"Tasks: {result.tasks_processed}",
        f"Scale: {result.scale}",
        f"Canonical tasks: {result.canonical_tasks}",
        "",
        (
            f"Result - PASS : {result.matched_routes}/{result.tasks_processed} "
            f"{passed_percent:.1f}% success"
        ),
        (f"Result - FAIL : {result.failed_routes}/{result.tasks_processed} {failed_percent:.1f}%"),
        "",
        "Report",
        "------",
        f"Local routes: {result.local_routes}",
        f"Frontier routes: {result.frontier_routes}",
        f"Local share: {result.local_share:.1%}",
        f"Estimated token savings: {result.savings_tokens:,}",
        f"Estimated avoided frontier-token share: {result.savings_ratio:.1%}",
        f"Safety escalations: {result.safety_escalations}",
        f"Elapsed time: {result.elapsed_seconds:.3f} s",
        f"Routing throughput: {result.tasks_per_second:,.0f} tasks/s",
        f"Report generated: {'yes' if result.report_generated else 'no'}",
        "",
        "Demo completed successfully." if result.success else "Demo failed.",
        "Data source: synthetic illustration",
    ]
    if result.output_directory is not None:
        lines.append(f"Workspace: {result.output_directory}")
    return "\n".join(lines)
