"""Minimal first-run example for the SQLite-first NCP path."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile

import ncp
from ncp.adapters.local import LocalAdapter
from ncp.stores.sqlite import SQLiteStore


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ncp_quickstart_") as tmp:
        project_root = Path(tmp)
        (project_root / ".git").mkdir()
        ncp.configure(cwd=project_root)
        store = SQLiteStore(project_root / ".ncp" / "store.db")

        agent = ncp.agent(
            id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="quickstart_demo",
            slot="first_turn",
            intent="show_bounded_context",
        )
        response = ncp.run(
            agent=agent,
            turn="Summarize why bounded context helps multi-agent work.",
            adapter=LocalAdapter(),
            store=store,
        )

        print(
            json.dumps(
                {
                    "store_path": str(store.path),
                    "response_first_line": response.content.splitlines()[0],
                    "turn_records": store.status()["turn_record_count"],
                    "cost_usd_total": store.status()["cost_usd_total"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
