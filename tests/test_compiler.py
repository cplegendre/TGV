from pathlib import Path

from tgv.compiler import ContextCompiler
from tgv.config import default_config
from tgv.models import Route, RouteDecision
from tgv.repository import Repository


def test_context_bundle_preserves_request_and_evidence(sample_repo: Path, tmp_path: Path) -> None:
    config = default_config()
    repository = Repository(sample_repo, config.repository, config.privacy.deny_patterns)
    compiler = ContextCompiler(repository)
    request = "Where is validate_token implemented?"
    decision = RouteDecision(
        route=Route.LOCAL_FAST,
        confidence=0.9,
        rationale="Simple lookup",
        search_terms=["validate_token"],
    )
    bundle = compiler.compile(request, decision)
    assert bundle.request == request
    assert bundle.evidence
    output = tmp_path / "bundle"
    compiler.write(bundle, output)
    assert (output / "bundle.json").is_file()
    assert "Original request" in (output / "handoff.md").read_text(encoding="utf-8")


def test_frontier_handoff_paths(sample_repo, tmp_path) -> None:
    repository = Repository(sample_repo, default_config().repository, [])
    compiler = ContextCompiler(repository)
    handoff, bundle = compiler.frontier_handoff_paths(tmp_path)
    assert handoff == tmp_path / "handoff.md"
    assert bundle == tmp_path / "bundle.json"
