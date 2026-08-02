from __future__ import annotations

import argparse
import json
from pathlib import Path

from tgv.config import default_config
from tgv.repository import Repository


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a local repository index/search diagnostic")
    parser.add_argument("repo", type=Path, nargs="?", default=Path.cwd())
    parser.add_argument("--query", default="router Repository")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = default_config()
    repository = Repository(args.repo, config.repository, config.privacy.deny_patterns)
    repository.build_index()
    results = repository.search(args.query)
    report = {**repository.diagnostics().model_dump(), "query": args.query, "results": len(results)}
    print(
        json.dumps(report, indent=2, sort_keys=True)
        if args.json
        else "\n".join(f"{key}: {value}" for key, value in report.items())
    )


if __name__ == "__main__":
    main()
