from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_VERSION_TOKEN = re.compile(r"\b0\.(\d+)\b")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def versions() -> dict[str, str]:
    """Exact-match version markers: every one of these must equal the
    current release version."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = re.search(
        r'__version__ = "([^"]+)"', (ROOT / "src/tgv/__init__.py").read_text(encoding="utf-8")
    )
    readme = re.search(r"Version ([0-9.]+)", (ROOT / "README.md").read_text(encoding="utf-8"))
    release = re.search(
        r"# TGV ([0-9.]+) release notes", (ROOT / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    )
    changelog = re.search(r"## \[([0-9.]+)\]", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    examples = re.search(
        r"# v([0-9.]+) routing and handoff examples",
        (ROOT / "examples" / "requests.txt").read_text(encoding="utf-8"),
    )
    audit = re.search(
        r"## TGV ([0-9.]+)", (ROOT / "docs" / "dependency-audit.md").read_text(encoding="utf-8")
    )
    found = {
        "package_metadata": project["project"]["version"],
        "package": package.group(1) if package else None,
        "readme": readme.group(1) if readme else None,
        "release_notes": release.group(1) if release else None,
        "changelog_latest": changelog.group(1) if changelog else None,
        "examples": examples.group(1) if examples else None,
        "dependency_audit": audit.group(1) if audit else None,
    }
    missing = [name for name, value in found.items() if value is None]
    if missing:
        raise RuntimeError(f"Could not locate version markers for: {missing}")
    return found  # type: ignore[return-value]


def forward_drift(current: str) -> dict[str, list[str]]:
    """SECURITY.md and ROADMAP.md legitimately reference past version
    numbers (e.g. 'Delivered in 0.15') as historical anchors, so they
    are not exact-match checked. Instead, flag any version token in
    these files that is *newer* than the current release, which would
    indicate a forgotten bump or a doc drafted against the wrong branch.
    """
    current_key = _version_key(current)
    drift: dict[str, list[str]] = {}
    for relative_path in ("SECURITY.md", "ROADMAP.md"):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        newer = sorted(
            {
                match.group(0)
                for match in _VERSION_TOKEN.finditer(text)
                if _version_key(match.group(0).removeprefix("v")) > current_key
            }
        )
        if newer:
            drift[relative_path] = newer
    return drift


def main() -> None:
    values = versions()
    if len(set(values.values())) != 1:
        raise SystemExit(f"Version mismatch: {values}")
    current = next(iter(values.values()))

    drift = forward_drift(current)
    if drift:
        raise SystemExit(
            f"Found version references newer than current release ({current}): {drift}"
        )

    print(current)


if __name__ == "__main__":
    main()
