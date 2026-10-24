from scripts.check_version import forward_drift, versions

from tgv import __version__
from tgv.evaluation import default_cases_path, run_benchmark


def test_release_versions_are_consistent() -> None:
    values = versions()
    assert set(values.values()) == {"0.39"}


def test_no_forward_version_drift_in_security_or_roadmap() -> None:
    current = next(iter(versions().values()))
    assert forward_drift(current) == {}


def test_benchmark_reports_current_package_version() -> None:
    assert run_benchmark(default_cases_path())["benchmark_version"] == __version__
