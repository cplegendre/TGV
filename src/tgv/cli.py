from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from tgv import __version__
from tgv.config import load_config
from tgv.efficiency import (
    aggregate_by_route,
    basic_value_report,
    propose_thresholds,
    score_sessions,
    value_report,
)
from tgv.exceptions import (
    CommandExecutionError,
    ConfigurationError,
    ProviderUnavailableError,
    RepositoryAccessError,
    TGVError,
    ValidationFailure,
)
from tgv.models import HandoffMode
from tgv.providers import OllamaProvider, OpenAICompatibleProvider
from tgv.service import TGVService


def _path(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgv",
        description="Task Gateway and Validator — local-first routing and optional frontier handoff",  # noqa: E501
    )
    parser.add_argument("--version", action="version", version=f"TGV {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="Run the fully local synthetic installation demo")
    demo.add_argument("--json", action="store_true")
    demo.add_argument("--keep-data", action="store_true")
    demo.add_argument("--output", type=_path)
    demo.add_argument("--no-report", action="store_true")
    demo.add_argument(
        "--scale",
        choices=("small", "medium", "large", "stress"),
        default="small",
        help="Demo size preset: 450, 5,000, 25,000, or 100,000 tasks",
    )
    demo.add_argument(
        "--tasks",
        type=int,
        help="Override the scale preset with an explicit positive task count",
    )

    doctor = sub.add_parser("doctor", help="Check configuration and local provider availability")
    doctor.add_argument("--config", type=_path)

    health = sub.add_parser("health", help="Check daemon readiness from its heartbeat status")
    health.add_argument("--status-file", type=_path, required=True)
    health.add_argument("--max-age", type=float, default=15.0)
    health.add_argument("--json", action="store_true")

    metrics = sub.add_parser(
        "metrics", help="Render daemon status as JSON or Prometheus text exposition"
    )
    metrics.add_argument("--status-file", type=_path, required=True)
    metrics.add_argument("--format", choices=("prometheus", "json"), default="prometheus")

    inspect = sub.add_parser("inspect", help="Summarize an accessible repository")
    inspect.add_argument("repo", type=_path, nargs="?", default=Path.cwd())
    inspect.add_argument("--config", type=_path)
    inspect.add_argument("--json", action="store_true")

    route = sub.add_parser("route", help="Classify a task")
    route.add_argument("request")
    route.add_argument("--repo", type=_path, default=Path.cwd())
    route.add_argument("--config", type=_path)
    route.add_argument("--explain", action="store_true")
    route.add_argument("--json", action="store_true")

    explain = sub.add_parser(
        "explain", help="Explain a route and show what would make another route win"
    )
    explain.add_argument("request")
    explain.add_argument("--repo", type=_path, default=Path.cwd())
    explain.add_argument("--config", type=_path)
    explain.add_argument("--json", action="store_true")

    prepare = sub.add_parser("prepare", help="Build an evidence-backed context bundle")
    prepare.add_argument("request")
    prepare.add_argument("--repo", type=_path, default=Path.cwd())
    prepare.add_argument("--config", type=_path)
    prepare.add_argument("--output", type=_path, required=True)
    prepare.add_argument("--mode", choices=[m.value for m in HandoffMode])
    prepare.add_argument("--dry-run", action="store_true")
    prepare.add_argument("--json", action="store_true")

    efficiency = sub.add_parser("efficiency", help="Report TER efficiency by TGV route")
    efficiency.add_argument("--sessions", type=_path, default=Path(".tgv/sessions"))
    efficiency.add_argument("--format", choices=("text", "json"), default="text")

    calibrate = sub.add_parser("calibrate", help="Propose routing thresholds from TER results")
    calibrate.add_argument("--sessions", type=_path, default=Path(".tgv/sessions"))
    calibrate.add_argument("--config", type=_path)
    calibrate.add_argument("--target-waste-ratio", type=float, default=0.25)
    calibrate.add_argument("--minimum-sessions", type=int, default=5)
    calibrate.add_argument("--step-size", type=float, default=0.05)
    calibrate.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite the validated YAML model; comments and key order may change",
    )
    calibrate.add_argument("--format", choices=("text", "json"), default="text")

    bench = sub.add_parser("bench", help="Run deterministic routing or index diagnostics")
    bench_sub = bench.add_subparsers(dest="benchmark", required=True)
    bench_routing = bench_sub.add_parser("routing", help="Evaluate labeled routing cases")
    bench_routing.add_argument("--cases", type=_path)
    bench_routing.add_argument("--json", action="store_true")
    bench_index = bench_sub.add_parser("index", help="Measure local index/search diagnostics")
    bench_index.add_argument("--repo", type=_path, default=Path.cwd())
    bench_index.add_argument("--query", default="router Repository")
    bench_index.add_argument("--json", action="store_true")

    profile = sub.add_parser("profile", help="Estimate task category and complexity")
    profile.add_argument("request")
    profile.add_argument("--repo", type=_path, default=Path.cwd())
    profile.add_argument("--config", type=_path)
    profile.add_argument("--initial-only", action="store_true")
    profile.add_argument("--json", action="store_true")

    retrieve = sub.add_parser("retrieve", help="Retrieve bounded lexical and structural context")
    retrieve.add_argument("request")
    retrieve.add_argument("--repo", type=_path, default=Path.cwd())
    retrieve.add_argument("--config", type=_path)
    retrieve.add_argument("--json", action="store_true")

    outcomes = sub.add_parser(
        "outcomes",
        help="Inspect, export, or complete outcomes (adaptive learning is experimental)",
    )
    outcomes.add_argument("action", choices=("list", "export", "complete"))
    outcomes.add_argument("task_id", nargs="?")
    feedback = outcomes.add_mutually_exclusive_group()
    feedback.add_argument("--accepted", action="store_true")
    feedback.add_argument("--rejected", action="store_true")
    outcomes.add_argument("--repo", type=_path, default=Path.cwd())
    outcomes.add_argument("--config", type=_path)
    outcomes.add_argument("--format", choices=("json", "csv"), default="json")
    outcomes.add_argument("--limit", type=int, default=100)

    scoreboard = sub.add_parser(
        "scoreboard", help="Show historical model utility for a request (experimental)"
    )
    scoreboard.add_argument("request")
    scoreboard.add_argument("--repo", type=_path, default=Path.cwd())
    scoreboard.add_argument("--config", type=_path)
    scoreboard.add_argument("--json", action="store_true")

    benchmark = sub.add_parser(
        "benchmark", help="Run integrated profiling/retrieval/routing evaluation"
    )
    benchmark.add_argument("action", choices=("run", "outcomes"))
    benchmark.add_argument("--cases", type=_path)
    benchmark.add_argument("--repo", type=_path, default=Path.cwd())
    benchmark.add_argument("--config", type=_path)
    benchmark.add_argument("--json", action="store_true")
    benchmark.add_argument(
        "--enforce",
        action="store_true",
        help="Exit non-zero when the selected benchmark quality gate fails",
    )
    benchmark.add_argument("--limit", type=int, default=10000)
    benchmark.add_argument("--minimum-verified-samples", type=int, default=20)
    benchmark.add_argument("--minimum-success-rate", type=float, default=0.85)
    benchmark.add_argument("--maximum-safety-violation-rate", type=float, default=0.0)

    report = sub.add_parser("report", help="Report local routing and token-allocation evidence")
    report.add_argument("--sessions", type=_path, default=Path(".tgv/sessions"))
    report.add_argument(
        "--ter",
        action="store_true",
        help="Use TER-scored aligned/waste tokens instead of coarse telemetry "
        "estimates; requires the optional 'ter' extra to be installed",
    )
    report.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="Serve sequential JSONL routing requests over stdio")
    serve.add_argument("--repo", type=_path, default=Path.cwd())
    serve.add_argument("--config", type=_path)
    serve.add_argument("--status-file", type=_path)
    serve.add_argument("--diagnostics-file", type=_path)
    serve.add_argument(
        "--metrics-file",
        type=_path,
        help="Write Prometheus textfile metrics atomically for external scraping",
    )
    serve.add_argument("--request-timeout", type=float, default=120.0)
    serve.add_argument("--heartbeat-interval", type=float, default=5.0)
    serve.add_argument("--shutdown-grace-seconds", type=float, default=10.0)

    search = sub.add_parser("search", help="Search repository text and symbols")
    search.add_argument("query")
    search.add_argument("--repo", type=_path, default=Path.cwd())
    search.add_argument("--config", type=_path)
    search.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    normalized = list(sys.argv[1:] if argv is None else argv)
    if "--demo" in normalized:
        normalized.remove("--demo")
        normalized.insert(0, "demo")
    args = parser.parse_args(normalized)
    try:
        if args.command == "demo":
            from tqdm import tqdm

            from tgv.demo import render_demo, run_demo

            progress_bar = tqdm(
                total=0,
                desc="Routing demo tasks",
                unit="task",
                disable=args.json or not sys.stderr.isatty(),
                dynamic_ncols=True,
            )

            def update_progress(completed: int, total: int) -> None:
                if progress_bar.total != total:
                    progress_bar.total = total
                    progress_bar.refresh()
                progress_bar.update(completed - progress_bar.n)

            try:
                demo_result = run_demo(
                    output=args.output,
                    keep_data=args.keep_data,
                    generate_report=not args.no_report,
                    progress=update_progress,
                    scale=args.scale,
                    tasks=args.tasks,
                    retain_task_details=False,
                )
            finally:
                progress_bar.close()
            rendered = (
                json.dumps(demo_result.to_dict(), sort_keys=True)
                if args.json
                else render_demo(demo_result)
            )
            print(rendered)
            if not demo_result.success:
                raise SystemExit(1)
            return
        if args.command == "doctor":
            try:
                config = load_config(args.config)
            except ConfigurationError as exc:
                print(
                    json.dumps(
                        {
                            "config": "fail",
                            "ollama_available": False,
                            "openai_compatible_available": False,
                            "fast_model": None,
                            "telemetry": "unknown",
                            "deny_patterns_count": 0,
                            "error": str(exc),
                        },
                        indent=2,
                    )
                )
                raise SystemExit(2) from exc
            fast = config.models.get("fast")
            ollama_available = False
            openai_compatible_available = False
            if fast and fast.provider == "ollama":
                with OllamaProvider(fast) as ollama_provider:
                    ollama_available = ollama_provider.available()
            elif fast and fast.provider == "openai_compatible":
                with OpenAICompatibleProvider(fast) as openai_provider:
                    openai_compatible_available = openai_provider.available()
            print(
                json.dumps(
                    {
                        "config": "ok",
                        "ollama_available": ollama_available,
                        "openai_compatible_available": openai_compatible_available,
                        "fast_model": fast.model if fast else None,
                        "telemetry": ("enabled" if config.telemetry.enabled else "disabled"),
                        "deny_patterns_count": len(config.privacy.deny_patterns),
                    },
                    indent=2,
                )
            )
            return
        if args.command == "health":
            from tgv.observability import health_report

            report = health_report(args.status_file, maximum_age_seconds=args.max_age)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(
                    f"health: {'ok' if report['healthy'] else 'fail'}\n"
                    f"ready: {report['ready']}\n"
                    f"reason: {report['reason']}"
                )
            if not report["healthy"]:
                raise SystemExit(1)
            return
        if args.command == "metrics":
            from tgv.observability import prometheus_text

            try:
                status_payload = json.loads(args.status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TGVError(f"could not read daemon status: {exc}") from exc
            if args.format == "json":
                print(json.dumps(status_payload, indent=2, sort_keys=True))
            else:
                print(prometheus_text(status_payload), end="")
            return
        if args.command == "calibrate":
            import yaml

            config = load_config(args.config)
            report = aggregate_by_route(score_sessions(args.sessions))
            proposals = propose_thresholds(
                report,
                local_answer_confidence=config.routing.local_answer_confidence,
                local_code_confidence=config.routing.local_code_confidence,
                target_waste_ratio=args.target_waste_ratio,
                minimum_sessions=args.minimum_sessions,
                step_size=args.step_size,
            )
            if args.apply:
                # The validated model is serialized; YAML order and comments may change.
                if args.config is None:
                    raise TGVError("--apply requires --config so TGV knows which file to update")
                for proposal in proposals:
                    if proposal.threshold_name and proposal.proposed_threshold is not None:
                        setattr(
                            config.routing,
                            proposal.threshold_name,
                            proposal.proposed_threshold,
                        )
                args.config.write_text(
                    yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False),
                    encoding="utf-8",
                )
            if args.format == "json":
                print(json.dumps([proposal.__dict__ for proposal in proposals], indent=2))
            else:
                for proposal in proposals:
                    print(f"{proposal.route}: {proposal.justification}")
            return
        if args.command == "bench":
            if args.benchmark == "routing":
                from tgv.bench.routing import default_cases_path, run_benchmark

                report = run_benchmark(args.cases or default_cases_path())
                if args.json:
                    print(json.dumps(report, indent=2, sort_keys=True))
                else:
                    print(f"cases: {report['total_cases']}")
                    print(f"accuracy: {report['accuracy']:.2%}")
                    print(f"acceptable-route accuracy: {report['acceptable_route_accuracy']:.2%}")
                    print(f"high-risk violations: {len(report['high_risk_violations'])}")
                return
            from tgv.config import default_config
            from tgv.repository import Repository

            config = default_config()
            repository = Repository(args.repo, config.repository, config.privacy.deny_patterns)
            repository.build_index()
            results = repository.search(args.query)
            search_report: dict[str, Any] = {
                **repository.diagnostics().model_dump(),
                "query": args.query,
                "results": len(results),
            }
            print(
                json.dumps(search_report, indent=2, sort_keys=True)
                if args.json
                else "\n".join(f"{key}: {value}" for key, value in search_report.items())
            )
            return
        if args.command == "report":
            value_metrics = (
                value_report(score_sessions(args.sessions))
                if args.ter
                else basic_value_report(args.sessions)
            )
            rendered = (
                json.dumps(value_metrics, indent=2, sort_keys=True)
                if args.json
                else "\n".join(f"{key}: {value}" for key, value in value_metrics.items())
            )
            print(rendered)
            return
        if args.command == "serve":
            from tgv.daemon import serve

            with closing(TGVService.create(args.repo, args.config)) as service:
                serve(
                    service,
                    timeout_seconds=args.request_timeout,
                    status_file=args.status_file,
                    diagnostics_file=args.diagnostics_file,
                    metrics_file=args.metrics_file,
                    heartbeat_interval=args.heartbeat_interval,
                    shutdown_grace_seconds=args.shutdown_grace_seconds,
                )
            return
        if args.command == "efficiency":
            report = aggregate_by_route(score_sessions(args.sessions))
            if args.format == "json":
                print(json.dumps(report, indent=2))
            else:
                print("route        sessions  TER     aligned  waste")
                for route, metrics in report.items():
                    print(
                        f"{route:<12} {metrics['session_count']:>8}  "
                        f"{metrics['aggregate_ter']:.4f}  "
                        f"{metrics['aligned_tokens']:>7}  {metrics['waste_tokens']:>5}"
                    )
            return
        with closing(TGVService.create(args.repo, args.config)) as service:
            if args.command == "inspect":
                print(service.repository.summary().model_dump_json(indent=2))
            elif args.command == "route":
                decision = service.route(args.request)
                if args.explain:
                    print(
                        json.dumps(
                            {
                                "route": decision.route,
                                "confidence": decision.confidence,
                                "thresholds": {
                                    "local_answer_confidence": (
                                        service.config.routing.local_answer_confidence
                                    ),
                                    "local_code_confidence": (
                                        service.config.routing.local_code_confidence
                                    ),
                                },
                                "decision_source": decision.decision_source,
                                "execution_plan": {
                                    "model": decision.selected_model,
                                    "capability_tier": decision.selected_capability_tier,
                                },
                                "risk_signals": decision.risk_signals,
                                "matched_category": decision.matched_category,
                                "matched_pattern": decision.matched_pattern,
                                "task_profile": (
                                    decision.task_profile.model_dump(mode="json")
                                    if decision.task_profile
                                    else None
                                ),
                                "historical_evidence": (
                                    decision.scoreboard_recommendation.model_dump(mode="json")
                                    if decision.scoreboard_recommendation
                                    else None
                                ),
                                "rationale": decision.rationale,
                            },
                            indent=2,
                        )
                    )
                else:
                    print(decision.model_dump_json(indent=2))
            elif args.command == "explain":
                from tgv.explain import render_explanation

                explanation = service.explain(args.request)
                print(
                    json.dumps(explanation, indent=2, sort_keys=True)
                    if args.json
                    else render_explanation(explanation)
                )
            elif args.command == "prepare":
                bundle = service.prepare(args.request)
                command = service.handoff(
                    bundle,
                    args.output,
                    dry_run=args.dry_run,
                    mode=HandoffMode(args.mode) if args.mode else None,
                )
                prepare_result = {
                    "output": str(args.output.resolve()),
                    "command": command,
                    "dry_run": args.dry_run,
                }
                print(
                    json.dumps(prepare_result, indent=2)
                    if args.json or args.dry_run
                    else prepare_result["output"]
                )
            elif args.command == "profile":
                profile_result = service.profile(args.request, retrieve=not args.initial_only)
                print(profile_result.model_dump_json(indent=2))
            elif args.command == "retrieve":
                print(
                    json.dumps(
                        [item.model_dump() for item in service.retrieve(args.request)], indent=2
                    )
                )
            elif args.command == "outcomes":
                if args.action == "complete":
                    if not args.task_id or args.accepted == args.rejected:
                        raise TGVError(
                            "outcomes complete requires TASK_ID and exactly one of "
                            "--accepted/--rejected"
                        )
                    accepted = bool(args.accepted)
                    service.outcomes.record_execution(args.task_id, status="completed")
                    service.outcomes.record_human_feedback(
                        args.task_id, accepted=accepted, rework_required=not accepted
                    )
                    print(
                        json.dumps(
                            {"task_id": args.task_id, "completed": True, "accepted": accepted}
                        )
                    )
                else:
                    records = service.outcomes.query(limit=args.limit)
                    if args.action == "list" or args.format == "json":
                        print(service.outcomes.export_json(records))
                    else:
                        print(service.outcomes.export_csv(records), end="")
            elif args.command == "scoreboard":
                profile_result = service.profile(args.request)
                recommendation = service.scoreboard.recommend(
                    profile_result,
                    repository_id=service.outcomes.repository_id(service.repository.root),
                )
                print(recommendation.model_dump_json(indent=2))
            elif args.command == "benchmark":
                from tgv.evaluation import (
                    default_cases_path,
                    run_benchmark,
                    run_outcome_benchmark,
                )

                if args.action == "outcomes":
                    repository_id = service.outcomes.repository_id(service.repository.root)
                    records = service.outcomes.query(
                        repository_id=repository_id, limit=args.limit
                    )
                    report = run_outcome_benchmark(
                        records,
                        minimum_verified_samples=args.minimum_verified_samples,
                        minimum_verified_success_rate=args.minimum_success_rate,
                        maximum_safety_violation_rate=args.maximum_safety_violation_rate,
                    )
                    report["repository_id"] = repository_id
                else:
                    report = run_benchmark(args.cases or default_cases_path())
                print(
                    json.dumps(report, indent=2, sort_keys=True) if args.json else report["summary"]
                )
                if args.enforce and not report["release_gate"]["passed"]:
                    raise SystemExit(1)
            elif args.command == "search":
                print(
                    json.dumps(
                        [item.model_dump() for item in service.repository.search(args.query)],
                        indent=2,
                    )
                )
    except ConfigurationError as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except RepositoryAccessError as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except ProviderUnavailableError as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(4) from exc
    except CommandExecutionError as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(5) from exc
    except ValidationFailure as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(6) from exc
    except TGVError as exc:
        print(f"tgv: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
