from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tgv.bench.routing import default_cases_path, run_benchmark
from tgv.config import default_config
from tgv.models import RepositoryConfig
from tgv.providers import CommandHandoffProvider
from tgv.repository import Repository, extract_python_symbols


def test_routing_benchmark_regression() -> None:
    report = run_benchmark(default_cases_path())
    assert report["total_cases"] >= 75
    assert report["acceptable_route_accuracy"] >= 0.90
    assert report["high_risk_violations"] == []


def test_python_symbol_extraction() -> None:
    symbols = extract_python_symbols(
        "import os\nfrom pathlib import Path\nVALUE = 1\nclass Worker:\n    def run(self): pass\n    async def wait(self): pass\ndef helper(): pass\nasync def fetch(): pass\n",  # noqa: E501
        "worker.py",
    )
    pairs = {(item["kind"], item["name"], item.get("parent")) for item in symbols}
    assert ("class", "Worker", None) in pairs
    assert ("method", "run", "Worker") in pairs
    assert ("async_method", "wait", "Worker") in pairs
    assert ("function", "helper", None) in pairs
    assert ("async_function", "fetch", None) in pairs
    assert ("assignment", "VALUE", None) in pairs
    assert ("import", "os", None) in pairs
    assert ("import", "pathlib", None) in pairs


def test_syntax_error_falls_back_without_symbols(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    repo = Repository(tmp_path, RepositoryConfig(), [])
    index = repo.build_index()
    assert index["broken.py"]["symbols"] == []
    assert repo.search("broken")[0].path == "broken.py"


def test_symbol_match_boost_and_explanation(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("class PaymentProcessor:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# PaymentProcessor is mentioned here\n", encoding="utf-8")
    results = Repository(tmp_path, RepositoryConfig(), []).search("PaymentProcessor")
    assert results[0].path == "a.py"
    assert results[0].matched_symbols == ["PaymentProcessor"]
    assert "exact symbol match" in results[0].ranking_reasons


def test_provider_lifecycle_and_result_ingestion(tmp_path: Path) -> None:
    result = tmp_path / "agent-result.json"
    code = "import json,sys;json.dump({'summary':'done','changed_files':['a.py'],'suggested_next_steps':['test'],'command_status':'complete'},open(sys.argv[1],'w'))"  # noqa: E501
    provider = CommandHandoffProvider(
        [sys.executable, "-c", code, str(result)], provider_name="codex", result_path=result
    )
    metadata = provider.invoke_with_metadata(str(tmp_path / "h.md"), str(tmp_path / "b.json"))
    assert metadata.provider_name == "codex"
    assert metadata.exit_code == 0
    assert metadata.result_summary == "done"
    assert metadata.changed_files == ["a.py"]


def test_nested_symlink_and_secret_omission(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-v017"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
    nested = tmp_path / "pkg"
    nested.mkdir()
    try:
        (nested / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    (tmp_path / ".npmrc").write_text("//registry:_authToken=SECRET", encoding="utf-8")
    config = default_config()
    repo = Repository(tmp_path, config.repository, config.privacy.deny_patterns)
    assert repo.search("TOP_SECRET SECRET") == []
    assert repo.omitted_summary().unsafe_symlink >= 1
    assert repo.omitted_summary().denied >= 1


def test_cli_benchmarks_json(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("class Router: pass\n", encoding="utf-8")
    env = {"PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    routing = subprocess.run(
        [sys.executable, "-m", "tgv", "bench", "routing", "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert routing.returncode == 0
    assert json.loads(routing.stdout)["total_cases"] >= 75
    index = subprocess.run(
        [sys.executable, "-m", "tgv", "bench", "index", "--repo", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert index.returncode == 0
    assert json.loads(index.stdout)["symbols_extracted"] == 1


def test_assignment_tuple_and_non_directory_root(tmp_path: Path) -> None:
    symbols = extract_python_symbols("A, B = (1, 2)\nC: int = 3\n", "assign.py")
    assert {item["name"] for item in symbols} == {"A", "B", "C"}
    file_root = tmp_path / "file.txt"
    file_root.write_text("x", encoding="utf-8")
    from tgv.exceptions import RepositoryAccessError

    with pytest.raises(RepositoryAccessError):
        Repository(file_root, RepositoryConfig(), [])


def test_cold_diagnostics_and_omitted_summary(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha(): pass\n", encoding="utf-8")
    repo = Repository(tmp_path, RepositoryConfig(), [])
    assert repo.diagnostics().symbols_extracted == 1
    repo2 = Repository(tmp_path, RepositoryConfig(), [])
    assert repo2.omitted_summary().denied == 0


def test_provider_missing_and_malformed_result(tmp_path: Path) -> None:
    missing = CommandHandoffProvider(
        [sys.executable, "-c", "pass"], result_path=tmp_path / "missing.json"
    )
    assert (
        missing.invoke_with_metadata(str(tmp_path / "h"), str(tmp_path / "j")).result_path is None
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    malformed = CommandHandoffProvider([sys.executable, "-c", "pass"], result_path=bad)
    assert malformed.invoke_with_metadata(
        str(tmp_path / "h"), str(tmp_path / "j")
    ).result_path == str(bad.resolve())


def test_handoff_markdown_provider_metadata(tmp_path: Path) -> None:
    from tgv.compiler import ContextCompiler
    from tgv.models import ContextBundle, HandoffMode, ProviderLifecycle, Route, RouteDecision

    bundle = ContextBundle(
        request="x",
        repository_root=str(tmp_path),
        decision=RouteDecision(route=Route.LOCAL_FAST, confidence=0.9, rationale="ok"),
        evidence=[],
        handoff_markdown="",
        provider=ProviderLifecycle(
            provider_name="claude-code",
            mode=HandoffMode.COMMAND,
            command=["claude"],
            dry_run=True,
            started_at="2026-01-01T00:00:00+00:00",
            command_status="dry-run",
        ),
    )
    text = ContextCompiler._handoff(bundle)
    assert "## Provider lifecycle" in text
    assert "claude-code" in text


def test_cli_bench_direct_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tgv.cli import main

    main(["bench", "routing"])
    assert "acceptable-route accuracy" in capsys.readouterr().out
    main(["bench", "routing", "--json"])
    assert json.loads(capsys.readouterr().out)["total_cases"] == 76
    (tmp_path / "x.py").write_text("class X: pass\n", encoding="utf-8")
    main(["bench", "index", "--repo", str(tmp_path)])
    assert "symbols_extracted" in capsys.readouterr().out
    main(["bench", "index", "--repo", str(tmp_path), "--json"])
    assert json.loads(capsys.readouterr().out)["symbols_extracted"] == 1
