"""Run the real cost-watchdog MCP server over stdio, backed by one scenario's World.

    python -m evals.mock_server <scenario-id>

Backend API calls are counted into the JSON file named by CW_EVAL_METER.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from .fake_cloud import Meter, installed
from .scenarios import BY_ID


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in BY_ID:
        sys.exit(f"usage: python -m evals.mock_server <{'|'.join(BY_ID)}>")
    today = datetime.now(timezone.utc).date()
    if os.getenv("CW_EVAL_TODAY"):
        today = datetime.fromisoformat(os.environ["CW_EVAL_TODAY"]).date()
    world = BY_ID[sys.argv[1]].world(today)
    with installed(world, Meter(os.getenv("CW_EVAL_METER"))) as server:
        server.mcp.run("stdio")


if __name__ == "__main__":
    main()
