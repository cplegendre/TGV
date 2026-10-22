from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tgv.service import TGVService


def create_server(repo: Path, config: Path | None = None) -> Any:
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("MCP support requires: pip install 'tgv-agent[mcp]'") from exc

    service = TGVService.create(repo, config, read_only=True)
    mcp = FastMCP("TGV", json_response=True)

    @mcp.tool()
    def list_repository_files(limit: int = 20) -> list[str]:
        """List eligible repository files, bounded by policy."""
        return service.repository.list_files(limit=min(max(limit, 1), 200))

    @mcp.tool()
    def search_repository(query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search repository text and return exact source excerpts."""
        results = service.repository.search(query, limit=min(max(limit, 1), 100))
        return [item.model_dump() for item in results]

    @mcp.tool()
    def read_file_excerpt(path: str, start_line: int = 1, line_count: int = 120) -> str:
        """Read a bounded, line-numbered text excerpt inside the repository."""
        return service.repository.excerpt(path, start_line, min(max(line_count, 1), 500))

    @mcp.tool()
    def prepare_context_bundle(request: str) -> dict[str, Any]:
        """Route a request and prepare an in-memory evidence-backed context bundle."""
        return service.prepare(request).model_dump()

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tgv-mcp")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    create_server(args.repo, args.config).run(transport="stdio")


if __name__ == "__main__":
    main()
