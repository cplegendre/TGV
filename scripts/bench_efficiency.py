#!/usr/bin/env python3
"""Benchmark estimated frontier token and cost savings from TGV routing.

This benchmark uses deterministic character-based token estimates (4 characters/token)
and configurable per-million-token prices. It does not transmit repository content.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tgv.config import default_config
from tgv.exceptions import RepositoryAccessError
from tgv.repository import Repository
from tgv.router import Router


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=Path, default=Path("examples/requests.txt"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--frontier-eur-per-million", type=float, default=10.0)
    args = parser.parse_args()

    config = default_config()
    router = Router(config.routing)
    repository = Repository(args.repo, config.repository, config.privacy.deny_patterns)
    requests = [
        line.strip()
        for line in args.requests.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    repo_tokens = 0
    for path in repository.list_files(200):
        try:
            repo_tokens += estimate_tokens(repository.read_text(path))
        except RepositoryAccessError:
            continue
    always_frontier = 0
    tgv_frontier = 0
    routes: dict[str, int] = {}
    for request in requests:
        request_tokens = estimate_tokens(request)
        frontier_tokens = request_tokens + repo_tokens
        always_frontier += frontier_tokens
        decision = router.route(request)
        routes[decision.route.value] = routes.get(decision.route.value, 0) + 1
        if decision.route.value == "frontier":
            tgv_frontier += frontier_tokens
    saved = always_frontier - tgv_frontier
    result = {
        "requests": len(requests),
        "routes": routes,
        "repository_tokens_estimated": repo_tokens,
        "always_frontier_tokens": always_frontier,
        "tgv_frontier_tokens": tgv_frontier,
        "tokens_saved": saved,
        "savings_percent": round(saved / always_frontier * 100, 2) if always_frontier else 0.0,
        "estimated_cost_saved_eur": round(saved / 1_000_000 * args.frontier_eur_per_million, 4),
        "method": "deterministic 4-characters-per-token estimate; input-side frontier cost only",
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
