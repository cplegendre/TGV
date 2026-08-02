from scripts.check_version import forward_drift, versions


def test_release_versions_are_consistent() -> None:
    values = versions()
    assert set(values.values()) == {"0.29"}


def test_no_forward_version_drift_in_security_or_roadmap() -> None:
    current = next(iter(versions().values()))
    assert forward_drift(current) == {}
