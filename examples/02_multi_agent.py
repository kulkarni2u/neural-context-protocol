"""Sequential multi-agent handoff example for the SQLite-first NCP path."""

from __future__ import annotations

from pathlib import Path
import json
import tempfile

import ncp
from ncp.adapters.local import LocalAdapter
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, Whisper


PIPELINE_ID = "pipe_example_handoff"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ncp_multi_agent_") as tmp:
        project_root = Path(tmp)
        (project_root / ".git").mkdir()
        ncp.configure(cwd=project_root)
        store = SQLiteStore(project_root / ".ncp" / "store.db")
        adapter = LocalAdapter()

        planner = ncp.agent(
            id="planner",
            role="plan",
            owns=["planning"],
            must_not=["shipping"],
            task="handoff_demo",
            slot="outline",
            intent="prepare_executor",
            pipeline_id=PIPELINE_ID,
        )
        executor = ncp.agent(
            id="executor",
            role="build",
            owns=["implementation"],
            must_not=["planning"],
            task="handoff_demo",
            slot="execute",
            intent="use_shared_context",
            pipeline_id=PIPELINE_ID,
        )
        critic = ncp.agent(
            id="critic",
            role="review",
            owns=["review"],
            must_not=["shipping"],
            task="handoff_demo",
            slot="review",
            intent="check_handoff_quality",
            pipeline_id=PIPELINE_ID,
        )

        planner_response = ncp.run(
            agent=planner,
            turn="Create a tiny implementation plan.",
            adapter=adapter,
            store=store,
        )
        ncp.write_memory(
            SubconsciousChunk(
                chunk_id="sub_example_plan",
                layer="semantic",
                content="handoff_demo execute planner decided to hand the task to executor with one bounded step",
                src="synthesis",
                written_by="planner",
                pipeline_id=PIPELINE_ID,
                relevance=0.95,
            ),
            store=store,
        )
        ncp.emit(
            Whisper(
                from_agent="planner",
                target="executor",
                whisper_type="nudge",
                payload="reuse_planner_memory",
                confidence=0.9,
                pipeline_id=PIPELINE_ID,
            ),
            store=store,
        )

        executor_context = ncp.get_context(agent=executor, store=store)
        executor_response = ncp.run(
            agent=executor,
            turn="Implement the single bounded step.",
            adapter=adapter,
            store=store,
        )
        critic_response = ncp.run(
            agent=critic,
            turn="Review whether the handoff stayed bounded.",
            adapter=adapter,
            store=store,
        )

        print(
            json.dumps(
                {
                    "planner_first_line": planner_response.content.splitlines()[0],
                    "executor_first_line": executor_response.content.splitlines()[0],
                    "critic_first_line": critic_response.content.splitlines()[0],
                    "executor_context_has_plan": "planner decided to hand the task" in executor_context,
                    "executor_context_has_whisper": "reuse_planner_memory" in executor_context,
                    "turn_records": store.status()["turn_record_count"],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
