import asyncio
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")


def test_stdio_mcp_exposes_all_repository_tools(sample_repo: Path) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "tgv.mcp_server", "--repo", str(sample_repo)],
            env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.call_tool("list_repository_files", {"limit": 10})
            assert "src/auth.py" in str(listed.content)

            searched = await session.call_tool(
                "search_repository", {"query": "validate_token", "limit": 5}
            )
            assert "validate_token" in str(searched.content)

            excerpt = await session.call_tool(
                "read_file_excerpt",
                {"path": "src/auth.py", "start_line": 1, "line_count": 5},
            )
            assert "validate_token" in str(excerpt.content)

            prepared = await session.call_tool(
                "prepare_context_bundle", {"request": "Where is validate_token?"}
            )
            assert "local_fast" in str(prepared.content)

    asyncio.run(exercise())
