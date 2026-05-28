"""CLI entrypoint for the NCP package."""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import json

import click
from rich import box
from rich.console import Console
from rich.table import Table

import ncp
from ncp.config import NCPConfig
from ncp.stores.base import BaseStore
from ncp.stores.base import NCPStoreUnavailableError
from ncp.stores.factory import create_store
from ncp.types import Whisper

console = Console()


CLAUDE_MD_TEMPLATE = """# NCP Conventions

- Call `ncp_get_context` at the start of each turn once the MCP server exists.
- Write durable memory with `ncp_write_memory` at the end of each turn.
- Keep context bounded and prefer recent refs over full-history replay.
"""


def _load_config_template() -> str:
    return resources.files("ncp").joinpath("templates/config.toml.example").read_text()


def _resolve_runtime_store(config: NCPConfig) -> BaseStore:
    try:
        return create_store(config)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    except NotImplementedError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_reporting_store(config: NCPConfig, command_name: str, capability: str) -> BaseStore:
    store = _resolve_runtime_store(config)
    if not callable(getattr(store, capability, None)):
        raise click.ClickException(
            f"`ncp {command_name}` is not supported by the configured {config.store_type} backend yet."
        )
    return store


def _store_display(config: NCPConfig) -> str:
    if config.store_type == "sqlite":
        return str(config.store_path)
    dsn = config.pgvector_dsn
    split = urlsplit(dsn)
    if split.password is None:
        return dsn
    username = split.username or ""
    host = split.hostname or ""
    if split.port is not None:
        host = f"{host}:{split.port}"
    auth = f"{username}:***@{host}" if username else host
    return urlunsplit((split.scheme, auth, split.path, split.query, split.fragment))


def _run_handoff_command(
    *,
    cwd: Path,
    agent_id: str,
    pipeline_id: str | None,
    max_items: int,
    min_confidence: float,
    instruction: str | None,
    emit_to: str | None,
    emit_type: str,
    emit_confidence: float,
    max_payload_chars: int,
    timeout_seconds: float,
    runner: str,
) -> str:
    from ncp.agent_handoff import (
        acknowledge_handoffs,
        emit_follow_up_whisper,
        load_handoffs,
        parse_json_review,
        run_claude_partner,
        run_opencode_reviewer,
        truncate_whisper_payload,
    )

    try:
        store, handoffs = load_handoffs(
            cwd=cwd,
            agent_id=agent_id,
            pipeline_id=pipeline_id,
            max_items=max_items,
            min_confidence=min_confidence,
        )
    except NotImplementedError as exc:
        raise click.ClickException(str(exc)) from exc
    if not handoffs:
        raise click.ClickException(f"No pending NCP handoffs for {agent_id}.")

    try:
        if runner == "claude":
            response = run_claude_partner(
                cwd=cwd,
                agent_id=agent_id,
                handoffs=handoffs,
                instruction=instruction,
                timeout_seconds=timeout_seconds,
            )
        else:
            response = run_opencode_reviewer(
                cwd=cwd,
                agent_id=agent_id,
                handoffs=handoffs,
                instruction=instruction,
                timeout_seconds=timeout_seconds,
            )
            parse_json_review(response)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if emit_to:
        emit_follow_up_whisper(
            cwd=cwd,
            from_agent=agent_id,
            target=emit_to,
            pipeline_id=pipeline_id or handoffs[0].pipeline_id,
            payload=truncate_whisper_payload(response, max_chars=max_payload_chars),
            whisper_type=emit_type,
            confidence=emit_confidence,
        )
    acknowledge_handoffs(store, handoffs)
    return response


def _format_ts(value: float | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def _build_explain_payload(
    *,
    store_path: str,
    pipeline_id: str | None,
    status_detail: dict[str, object],
    cost_detail: dict[str, object],
) -> dict[str, object]:
    overview = status_detail["overview"]
    layer_counts = status_detail["layer_counts"]
    recent_pipelines = status_detail["recent_pipelines"]
    summary = cost_detail["summary"]
    by_agent = cost_detail["by_agent"]
    by_model = cost_detail["by_model"]

    warnings: list[str] = []
    if int(overview["chunk_count"]) == 0:
        warnings.append("No persisted chunks yet; the store is initialized but has not captured durable memory.")
    if int(overview["whisper_count"]) > 10:
        warnings.append("Whisper backlog is high; agents may not be draining transient signals quickly enough.")
    if int(summary["entry_count"]) == 0:
        warnings.append("No cost telemetry recorded yet; ncp cost will stay empty until provider turns are logged.")
    if len(layer_counts) == 1 and int(overview["chunk_count"]) >= 5:
        dominant_layer = next(iter(layer_counts))
        warnings.append(
            f"Memory is concentrated in the {dominant_layer} layer; consider whether other chunk types are being under-used."
        )
    if pipeline_id is None and not recent_pipelines:
        warnings.append("No named pipelines found yet; cross-host sharing may still be happening on the global/default path.")

    if int(overview["chunk_count"]) == 0:
        headline = "NCP is initialized but still cold."
    elif int(overview["turn_record_count"]) == 0:
        headline = "Memory exists, but turn records have not been built up yet."
    else:
        headline = "NCP is actively recording bounded context and recent turn state."

    if int(summary["entry_count"]) > 0:
        headline += (
            f" Cost telemetry covers {summary['entry_count']} turns for "
            f"{float(summary['cost_usd_total']):.4f} USD total."
        )

    top_agent = by_agent[0]["agent_id"] if by_agent else None
    top_model = by_model[0]["model"] if by_model else None

    return {
        "store_path": store_path,
        "pipeline_id": pipeline_id,
        "headline": headline,
        "warnings": warnings,
        "facts": {
            "chunk_count": overview["chunk_count"],
            "whisper_count": overview["whisper_count"],
            "turn_record_count": overview["turn_record_count"],
            "pipeline_count": overview["pipeline_count"],
            "last_activity_at": overview["last_activity_at"],
            "dominant_layers": layer_counts,
            "top_agent": top_agent,
            "top_model": top_model,
            "cost_usd_total": summary["cost_usd_total"],
        },
        "status": status_detail,
        "cost": cost_detail,
    }


@click.group()
def main() -> None:
    """Neural Context Protocol CLI."""


@main.command("init")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
def init_command(cwd: Path) -> None:
    """Initialize `.ncp/config.toml` and a minimal `CLAUDE.md`."""

    config_dir = cwd / ".ncp"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.toml"
    if not config_path.exists():
        config_path.write_text(_load_config_template())
    claude_path = cwd / "CLAUDE.md"
    if not claude_path.exists():
        claude_path.write_text(CLAUDE_MD_TEMPLATE)
    console.print(f"Initialized NCP in {cwd}")


@main.command("status")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None, help="Optional pipeline filter for richer status detail.")
@click.option("--json-output", is_flag=True, help="Emit machine-readable JSON instead of tables.")
def status_command(cwd: Path, pipeline_id: str | None, json_output: bool) -> None:
    """Show rich NCP store status."""

    try:
        config = ncp.configure(cwd=cwd)
        store = _resolve_reporting_store(config, "status", "status_detail")
        detail = store.status_detail(pipeline_id=pipeline_id)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "store_path": _store_display(config),
        "pipeline_id": pipeline_id,
        **detail,
    }
    if json_output:
        console.print_json(data=payload)
        return

    overview = detail["overview"]
    table = Table(title="NCP Status", box=box.SIMPLE_HEAVY)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Store", _store_display(config))
    table.add_row("Pipeline filter", pipeline_id or "all")
    table.add_row("Chunks", str(overview["chunk_count"]))
    table.add_row("Tombstones", str(overview["tombstone_count"]))
    table.add_row("Whispers", str(overview["whisper_count"]))
    table.add_row("Turn records", str(overview["turn_record_count"]))
    table.add_row("Conscious snapshots", str(overview["conscious_snapshot_count"]))
    table.add_row("Cost entries", str(overview["cost_entry_count"]))
    table.add_row("Pipelines", str(overview["pipeline_count"]))
    table.add_row("Cost USD", f"{float(overview['cost_usd_total']):.4f}")
    table.add_row("Last activity", _format_ts(overview["last_activity_at"]))  # type: ignore[arg-type]
    console.print(table)

    layer_counts = detail["layer_counts"]
    if layer_counts:
        layer_table = Table(title="Chunk Layers", box=box.MINIMAL_DOUBLE_HEAD)
        layer_table.add_column("Layer")
        layer_table.add_column("Chunks", justify="right")
        for layer, count in layer_counts.items():
            layer_table.add_row(str(layer), str(count))
        console.print(layer_table)

    recent_pipelines = detail["recent_pipelines"]
    if recent_pipelines and pipeline_id is None:
        pipeline_table = Table(title="Recent Pipelines", box=box.MINIMAL_DOUBLE_HEAD)
        pipeline_table.add_column("Pipeline")
        pipeline_table.add_column("Chunks", justify="right")
        pipeline_table.add_column("Last chunk")
        for row in recent_pipelines:
            pipeline_table.add_row(
                str(row["pipeline_id"]),
                str(row["chunk_count"]),
                _format_ts(float(row["last_chunk_at"])),
            )
        console.print(pipeline_table)


@main.command("cost")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None, help="Optional pipeline filter.")
@click.option("--limit", default=10, show_default=True, type=click.IntRange(1, 50))
@click.option("--json-output", is_flag=True, help="Emit machine-readable JSON instead of tables.")
def cost_command(cwd: Path, pipeline_id: str | None, limit: int, json_output: bool) -> None:
    """Show cost totals, rollups, and recent turn cost entries."""

    try:
        config = ncp.configure(cwd=cwd)
        store = _resolve_reporting_store(config, "cost", "cost_summary")
        detail = store.cost_summary(pipeline_id=pipeline_id, limit=limit)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {
        "store_path": _store_display(config),
        "pipeline_id": pipeline_id,
        **detail,
    }
    if json_output:
        console.print_json(data=payload)
        return

    summary = detail["summary"]
    summary_table = Table(title="NCP Cost", box=box.SIMPLE_HEAVY)
    summary_table.add_column("Metric")
    summary_table.add_column("Value", justify="right")
    summary_table.add_row("Store", _store_display(config))
    summary_table.add_row("Pipeline filter", pipeline_id or "all")
    summary_table.add_row("Cost USD", f"{float(summary['cost_usd_total']):.4f}")
    summary_table.add_row("Entries", str(summary["entry_count"]))
    summary_table.add_row("Input tokens", str(summary["input_tokens_total"]))
    summary_table.add_row("Output tokens", str(summary["output_tokens_total"]))
    summary_table.add_row("Cache read tokens", str(summary["cache_read_tokens_total"]))
    summary_table.add_row("Avg latency ms", f"{float(summary['avg_latency_ms']):.1f}")
    console.print(summary_table)

    by_agent = detail["by_agent"]
    if by_agent:
        agent_table = Table(title="Cost by Agent", box=box.MINIMAL_DOUBLE_HEAD)
        agent_table.add_column("Agent")
        agent_table.add_column("Turns", justify="right")
        agent_table.add_column("Cost USD", justify="right")
        for row in by_agent:
            agent_table.add_row(
                str(row["agent_id"]),
                str(row["turns"]),
                f"{float(row['cost_usd_total']):.4f}",
            )
        console.print(agent_table)

    by_model = detail["by_model"]
    if by_model:
        model_table = Table(title="Cost by Model", box=box.MINIMAL_DOUBLE_HEAD)
        model_table.add_column("Model")
        model_table.add_column("Turns", justify="right")
        model_table.add_column("Cost USD", justify="right")
        for row in by_model:
            model_table.add_row(
                str(row["model"]),
                str(row["turns"]),
                f"{float(row['cost_usd_total']):.4f}",
            )
        console.print(model_table)

    recent_entries = detail["recent_entries"]
    if recent_entries:
        recent_table = Table(title="Recent Cost Entries", box=box.MINIMAL_DOUBLE_HEAD)
        recent_table.add_column("Turn")
        recent_table.add_column("Agent")
        recent_table.add_column("Model")
        recent_table.add_column("In", justify="right")
        recent_table.add_column("Out", justify="right")
        recent_table.add_column("USD", justify="right")
        recent_table.add_column("Logged")
        for row in recent_entries:
            recent_table.add_row(
                str(row["turn_id"]),
                str(row["agent_id"]),
                str(row["model"]),
                str(row["input_tokens"]),
                str(row["output_tokens"]),
                f"{float(row['cost_usd']):.4f}",
                _format_ts(float(row["logged_at"])),
            )
        console.print(recent_table)


@main.command("explain")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None, help="Optional pipeline filter.")
@click.option("--limit", default=5, show_default=True, type=click.IntRange(1, 20))
@click.option("--json-output", is_flag=True, help="Emit machine-readable JSON instead of a narrative summary.")
def explain_command(cwd: Path, pipeline_id: str | None, limit: int, json_output: bool) -> None:
    """Explain the current NCP store state in a human-readable way."""

    try:
        config = ncp.configure(cwd=cwd)
        store = _resolve_reporting_store(config, "explain", "status_detail")
        if not callable(getattr(store, "cost_summary", None)):
            raise click.ClickException(
                f"`ncp explain` is not supported by the configured {config.store_type} backend yet."
            )
        status_detail = store.status_detail(pipeline_id=pipeline_id)
        cost_detail = store.cost_summary(pipeline_id=pipeline_id, limit=limit)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    payload = _build_explain_payload(
        store_path=_store_display(config),
        pipeline_id=pipeline_id,
        status_detail=status_detail,
        cost_detail=cost_detail,
    )
    if json_output:
        console.print_json(data=payload)
        return

    console.print(f"[bold]NCP Explain[/bold]  store={_store_display(config)}")
    if pipeline_id:
        console.print(f"Pipeline filter: [bold]{pipeline_id}[/bold]")
    console.print(payload["headline"])

    facts = payload["facts"]
    console.print(
        f"- chunks={facts['chunk_count']} whispers={facts['whisper_count']} "
        f"turn_records={facts['turn_record_count']} pipelines={facts['pipeline_count']}"
    )
    console.print(
        f"- total_cost_usd={float(facts['cost_usd_total']):.4f} "
        f"last_activity={_format_ts(facts['last_activity_at'])}"
    )
    if facts["top_agent"] is not None:
        console.print(f"- highest-cost agent so far: {facts['top_agent']}")
    if facts["top_model"] is not None:
        console.print(f"- highest-cost model so far: {facts['top_model']}")
    if facts["dominant_layers"]:
        layer_line = ", ".join(
            f"{layer}={count}" for layer, count in list(facts["dominant_layers"].items())[:5]
        )
        console.print(f"- layer distribution: {layer_line}")

    warnings = payload["warnings"]
    if warnings:
        console.print("[bold]Warnings[/bold]")
        for warning in warnings:
            console.print(f"- {warning}")
    else:
        console.print("[bold]Warnings[/bold]")
        console.print("- none")


@main.command("serve")
@click.option("--cwd", type=click.Path(path_type=Path), default=None,
              help="Project root used to resolve .ncp/config.toml when the MCP host launches from another directory.")
@click.option("--store-path", type=click.Path(path_type=Path), default=None,
              help="Path to the NCP store. Defaults to .ncp/store.db from config.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host interface for HTTP/SSE mode.")
@click.option("--port", default=4242, show_default=True, type=int,
              help="Port for HTTP/SSE mode.")
def serve_command(
    cwd: Path | None,
    store_path: Path | None,
    host: str,
    port: int,
) -> None:
    """Start the MCP server over HTTP POST plus SSE discovery."""

    from ncp.mcp.server import serve_http

    serve_http(host=host, port=port, store_path=store_path, cwd=cwd)


@main.command("serve-stdio", hidden=True)
@click.option("--cwd", type=click.Path(path_type=Path), default=None)
@click.option("--store-path", type=click.Path(path_type=Path), default=None)
def serve_stdio_command(cwd: Path | None, store_path: Path | None) -> None:
    """Internal compatibility transport used by tests and dogfood."""

    from ncp.mcp.server import serve as mcp_serve

    mcp_serve(store_path=store_path, cwd=cwd)


@main.command("dogfood")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--store-path", type=click.Path(path_type=Path), default=None)
@click.option("--pipeline-id", default="pipe_dogfood_mcp", show_default=True)
@click.option("--planner-provider", default="claude", show_default=True)
@click.option("--executor-provider", default="opencode", show_default=True)
@click.option("--critic-provider", default="codex", show_default=True)
@click.option("--continuation-adapter", default=None,
              help="Optional adapter continuation mode: local, claude-cli, codex-cli, opencode-cli, anthropic, openai, ollama, gemini, mistral, cohere.")
@click.option("--attempts", default=1, show_default=True, type=click.IntRange(1, None),
              help="Repeat the continuation adapter run N times and print a compact summary artifact.")
@click.option("--adapter-timeout-seconds", default=None, type=float,
              help="Override the per-call timeout for CLI-backed continuation adapters.")
@click.option("--transport", type=click.Choice(["http", "stdio"]), default="http", hidden=True)
def dogfood_command(
    *,
    cwd: Path,
    store_path: Path | None,
    pipeline_id: str,
    planner_provider: str,
    executor_provider: str,
    critic_provider: str,
    continuation_adapter: str | None,
    attempts: int,
    adapter_timeout_seconds: float | None,
    transport: str,
) -> None:
    """Run the canonical MCP dogfood loop or a continuation repeatability pass."""

    from ncp.dogfood import (
        load_dogfood_adapter,
        run_adapter_continuation_dogfood_loop,
        run_canonical_dogfood_loop,
        run_canonical_http_dogfood_loop,
        run_live_adapter_continuation_attempt,
        run_repeatability_dogfood_loop,
    )

    config = ncp.configure(cwd=cwd)
    common_kwargs = {
        "store_path": store_path or config.store_path,
        "cwd": Path(__file__).resolve().parents[1],
        "pipeline_id": pipeline_id,
        "provider_roles": {
            "planner": planner_provider,
            "executor": executor_provider,
            "critic": critic_provider,
        },
    }
    if attempts > 1 and not continuation_adapter:
        raise click.UsageError("--attempts requires --continuation-adapter so the run targets one provider path.")
    if continuation_adapter:
        normalized_adapter = continuation_adapter.strip().lower()
        if attempts > 1:
            artifact = run_repeatability_dogfood_loop(
                normalized_adapter,
                attempts=attempts,
                adapter_timeout_seconds=adapter_timeout_seconds,
                transport=transport,
                **common_kwargs,
            )
        elif normalized_adapter == "local":
            artifact = run_adapter_continuation_dogfood_loop(
                adapter=load_dogfood_adapter(
                    normalized_adapter,
                    timeout_seconds=adapter_timeout_seconds,
                ),
                transport=transport,
                **common_kwargs,
            )
        else:
            artifact = run_live_adapter_continuation_attempt(
                normalized_adapter,
                adapter_timeout_seconds=adapter_timeout_seconds,
                transport=transport,
                **common_kwargs,
            )
    else:
        if transport == "http":
            artifact = run_canonical_http_dogfood_loop(**common_kwargs)
        else:
            artifact = run_canonical_dogfood_loop(**common_kwargs)
    console.print_json(data=artifact)


@main.group("handoff")
def handoff_group() -> None:
    """Run repo-bound Claude/OpenCode handoff consumers on pending whispers."""


@handoff_group.command("claude")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None)
@click.option("--max-items", default=3, show_default=True, type=click.IntRange(1, 10))
@click.option("--min-confidence", default=0.60, show_default=True, type=float)
@click.option("--instruction", default=None, help="Optional extra instruction for Claude.")
@click.option("--emit-to", default=None, help="Optional follow-up whisper target.")
@click.option("--emit-type", default="nudge", show_default=True)
@click.option("--emit-confidence", default=0.90, show_default=True, type=float)
@click.option("--max-payload-chars", default=600, show_default=True, type=click.IntRange(1, 600))
@click.option("--timeout-seconds", default=90.0, show_default=True, type=float)
def handoff_claude_command(
    cwd: Path,
    pipeline_id: str | None,
    max_items: int,
    min_confidence: float,
    instruction: str | None,
    emit_to: str | None,
    emit_type: str,
    emit_confidence: float,
    max_payload_chars: int,
    timeout_seconds: float,
) -> None:
    """Consume pending whispers for Claude and optionally emit a follow-up whisper."""

    response = _run_handoff_command(
        cwd=cwd,
        agent_id="claude",
        pipeline_id=pipeline_id,
        max_items=max_items,
        min_confidence=min_confidence,
        instruction=instruction,
        emit_to=emit_to,
        emit_type=emit_type,
        emit_confidence=emit_confidence,
        max_payload_chars=max_payload_chars,
        timeout_seconds=timeout_seconds,
        runner="claude",
    )
    console.print(response)


@handoff_group.command("opencode")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None)
@click.option("--max-items", default=3, show_default=True, type=click.IntRange(1, 10))
@click.option("--min-confidence", default=0.60, show_default=True, type=float)
@click.option("--instruction", default=None, help="Optional extra instruction for OpenCode.")
@click.option("--emit-to", default=None, help="Optional follow-up whisper target.")
@click.option("--emit-type", default="nudge", show_default=True)
@click.option("--emit-confidence", default=0.90, show_default=True, type=float)
@click.option("--max-payload-chars", default=600, show_default=True, type=click.IntRange(1, 600))
@click.option("--timeout-seconds", default=45.0, show_default=True, type=float)
def handoff_opencode_command(
    cwd: Path,
    pipeline_id: str | None,
    max_items: int,
    min_confidence: float,
    instruction: str | None,
    emit_to: str | None,
    emit_type: str,
    emit_confidence: float,
    max_payload_chars: int,
    timeout_seconds: float,
) -> None:
    """Consume pending whispers for OpenCode and require a JSON review payload."""

    response = _run_handoff_command(
        cwd=cwd,
        agent_id="opencode",
        pipeline_id=pipeline_id,
        max_items=max_items,
        min_confidence=min_confidence,
        instruction=instruction,
        emit_to=emit_to,
        emit_type=emit_type,
        emit_confidence=emit_confidence,
        max_payload_chars=max_payload_chars,
        timeout_seconds=timeout_seconds,
        runner="opencode",
    )
    console.print(response)


@main.command("emit")
@click.option("--from-agent", "from_agent", required=True)
@click.option("--to", "target", required=True)
@click.option("--type", "whisper_type", required=True)
@click.option("--payload", required=True)
@click.option("--confidence", default=1.0, type=float)
@click.option("--pipeline-id", default=None)
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
def emit_command(
    *,
    from_agent: str,
    target: str,
    whisper_type: str,
    payload: str,
    confidence: float,
    pipeline_id: str | None,
    cwd: Path,
) -> None:
    """Emit a manual whisper into the configured runtime store."""

    try:
        config = ncp.configure(cwd=cwd)
        store = _resolve_runtime_store(config)
        ncp.emit(
            Whisper(
                from_agent=from_agent,
                target=target,
                whisper_type=whisper_type,
                payload=payload,
                confidence=confidence,
                pipeline_id=pipeline_id,
            ),
            store=store,
        )
    except NotImplementedError as exc:
        raise click.ClickException(str(exc)) from exc
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print("Whisper emitted.")


@main.command("viz")
@click.option("--cwd", type=click.Path(path_type=Path), default=Path.cwd)
@click.option("--pipeline-id", default=None, help="Optional pipeline scope filter.")
def viz_command(cwd: Path, pipeline_id: str | None) -> None:
    """Show operator view: chunk distribution, age brackets, top chunks, pipelines, whispers."""

    from rich.panel import Panel

    try:
        config = ncp.configure(cwd=cwd)
        store = _resolve_reporting_store(config, "viz", "viz_data")
        data = store.viz_data(pipeline_id=pipeline_id)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(f"[bold]NCP Viz[/bold]  store={_store_display(config)}"
                  + (f"  pipeline={pipeline_id}" if pipeline_id else ""))

    # Panel 1: Chunk distribution
    dist_table = Table(title="Chunk Distribution", box=box.SIMPLE_HEAVY)
    dist_table.add_column("Layer")
    dist_table.add_column("Zone")
    dist_table.add_column("Count", justify="right")
    for row in data["chunk_distribution"]:
        dist_table.add_row(str(row["layer"]), str(row["zone"]), str(row["count"]))
    if not data["chunk_distribution"]:
        dist_table.add_row("[dim]-[/dim]", "[dim]-[/dim]", "[dim]0[/dim]")
    console.print(dist_table)

    # Panel 2: Age brackets
    age_table = Table(title="Age Brackets", box=box.MINIMAL_DOUBLE_HEAD)
    age_table.add_column("Bracket")
    age_table.add_column("Count", justify="right")
    age_table.add_column("Avg Trust", justify="right")
    age_table.add_column("Top Layer")
    for row in data["age_brackets"]:
        age_table.add_row(
            str(row["bracket"]),
            str(row["count"]),
            f"{float(row['avg_trust']):.3f}",
            str(row["top_layer"]),
        )
    if not data["age_brackets"]:
        age_table.add_row("[dim]-[/dim]", "[dim]0[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
    console.print(age_table)

    # Panel 3: Top chunks
    top_table = Table(title="Top 5 Chunks (by trust)", box=box.MINIMAL_DOUBLE_HEAD)
    top_table.add_column("ID (16)")
    top_table.add_column("Layer")
    top_table.add_column("Zone")
    top_table.add_column("Pipeline")
    top_table.add_column("Trust", justify="right")
    top_table.add_column("Age (s)", justify="right")
    for row in data["top_chunks"]:
        top_table.add_row(
            str(row["chunk_id"]),
            str(row["layer"]),
            str(row["zone"]),
            str(row["pipeline_id"]) if row["pipeline_id"] is not None else "-",
            f"{float(row['base_trust']):.3f}",
            str(row["age_seconds"]),
        )
    if not data["top_chunks"]:
        top_table.add_row("[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]", "[dim]-[/dim]")
    console.print(top_table)

    # Panel 4: Pipeline summary (only show when pipelines present)
    pipeline_summary = data["pipeline_summary"]
    if pipeline_summary:
        pipe_table = Table(title="Pipeline Summary", box=box.MINIMAL_DOUBLE_HEAD)
        pipe_table.add_column("Pipeline")
        pipe_table.add_column("Chunks", justify="right")
        pipe_table.add_column("Last Activity")
        for row in pipeline_summary:
            pipe_table.add_row(
                str(row["pipeline_id"]),
                str(row["chunk_count"]),
                _format_ts(float(row["last_activity"])),
            )
        console.print(pipe_table)

    # Panel 5: Whisper queue
    wq = data["whisper_queue"]
    wq_total = int(wq["total"])  # type: ignore[arg-type]
    by_type = wq["by_type"]
    wq_lines = [f"Total pending: {wq_total}"]
    if isinstance(by_type, dict) and by_type:
        wq_lines.append("By type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))  # type: ignore[union-attr]
    else:
        wq_lines.append("(queue empty)")
    console.print(Panel("\n".join(wq_lines), title="Whisper Queue", expand=False))


@main.command("consolidate")
@click.option("--cwd", default=None, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--pipeline-id", default=None, help="Scope consolidation to one pipeline.")
@click.option("--dry-run", is_flag=True, default=False, help="Preview merges without writing.")
@click.option("--similarity-threshold", default=None, type=float, help="Override config similarity threshold.")
def consolidate_command(
    cwd: Path | None,
    pipeline_id: str | None,
    dry_run: bool,
    similarity_threshold: float | None,
) -> None:
    """Merge redundant chunks and clean up tombstones."""
    from ncp.config import load_config

    config = load_config(cwd=cwd or Path.cwd())
    threshold = similarity_threshold if similarity_threshold is not None else config.consolidation_similarity_threshold
    trust_floor = config.consolidation_trust_floor

    try:
        store = create_store(config)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        report = store.consolidate(
            pipeline_id=pipeline_id,
            dry_run=dry_run,
            similarity_threshold=threshold,
            trust_floor=trust_floor,
        )
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    table = Table(title="Consolidation Report", box=box.SIMPLE)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Mode", "dry-run" if report.dry_run else "live")
    table.add_row("Pipeline", report.pipeline_id or "all")
    table.add_row("Clusters scanned", str(report.clusters_scanned))
    table.add_row("Groups merged", str(report.merged))
    table.add_row("Chunks tombstoned", str(report.tombstoned))
    table.add_row("Chunks skipped", str(report.skipped))
    table.add_row("Duration", f"{report.duration_seconds:.3f}s")
    console.print(table)

    if report.dry_run and report.merged > 0:
        console.print(f"[yellow]Dry run: {report.merged} merge(s) would be committed.[/yellow]")
    elif report.merged == 0:
        console.print("[dim]Nothing to consolidate.[/dim]")
    else:
        console.print(f"[green]Consolidated {report.merged} group(s), {report.tombstoned} chunk(s) tombstoned.[/green]")


@main.command("calibrate")
@click.option("--cwd", default=None, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--pipeline-id", default=None, help="Scope batch decay to one pipeline.")
@click.option("--chunk-id", default=None, help="Pinpoint chunk to override (manual mode).")
@click.option("--trust", default=None, type=float, help="Explicit trust value for manual override (required with --chunk-id).")
@click.option("--decay-factor", default=0.85, show_default=True, type=float, help="Multiplicative decay applied in batch mode.")
@click.option("--dry-run", is_flag=True, default=False, help="Preview changes without writing.")
def calibrate_command(
    cwd: Path | None,
    pipeline_id: str | None,
    chunk_id: str | None,
    trust: float | None,
    decay_factor: float,
    dry_run: bool,
) -> None:
    """Re-score base_trust on existing chunks without touching the database manually."""
    from ncp.config import load_config

    if chunk_id is not None and trust is None:
        raise click.UsageError("--trust is required when --chunk-id is provided.")

    config = load_config(cwd=cwd or Path.cwd())

    try:
        store = create_store(config)
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        report = store.calibrate(
            pipeline_id=pipeline_id,
            chunk_id=chunk_id,
            trust=trust,
            dry_run=dry_run,
            decay_factor=decay_factor,
        )
    except (NCPStoreUnavailableError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    table = Table(title="Calibration Report", box=box.SIMPLE)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Mode", "dry-run" if report.dry_run else "live")
    table.add_row("Pipeline", report.pipeline_id or "all")
    table.add_row("Adjusted", str(report.adjusted))
    table.add_row("Protected (user_verified)", str(report.protected))
    table.add_row("Skipped", str(report.skipped))
    table.add_row("Duration", f"{report.duration_seconds:.3f}s")
    console.print(table)

    if report.dry_run and report.adjusted > 0:
        console.print(f"[yellow]Dry run: {report.adjusted} chunk(s) would be adjusted.[/yellow]")
    elif report.adjusted == 0:
        console.print("[dim]Nothing to calibrate.[/dim]")
    else:
        console.print(f"[green]Calibrated {report.adjusted} chunk(s).[/green]")


@main.command("batch")
@click.argument("input_file", type=click.Path(path_type=Path), required=False)
@click.option("--output", type=click.Path(path_type=Path), default=None, help="Write results to file instead of stdout.")
@click.option("--cwd", type=click.Path(path_type=Path, exists=True, file_okay=False), default=Path.cwd)
@click.option("--dry-run", is_flag=True, default=False, help="Pass dry_run=True to all ops that support it, skip writes.")
@click.option("--stop-on-error", is_flag=True, default=False, help="Halt on first failed op.")
def batch_command(
    input_file: Path | None,
    output: Path | None,
    cwd: Path,
    dry_run: bool,
    stop_on_error: bool,
) -> None:
    """Process a JSONL file of NCP operations against the local store.

    Positional INPUT can be a path or omit/use `-` to read from stdin.
    """

    from ncp.batch import run_batch
    from ncp.config import load_config
    from ncp.stores.factory import create_store

    config = load_config(cwd=cwd)
    store = create_store(config)

    if input_file is None:
        raw = __import__("sys").stdin.read()
    else:
        raw = input_file.read_text()

    lines = raw.strip().splitlines()
    operations: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            operations.append(json.loads(line))
        except json.JSONDecodeError as exc:
            err = {"op": "unknown", "ok": False, "error": str(exc)}
            if stop_on_error:
                __import__("sys").stdout.write(json.dumps(err) + "\n")
                return
            operations.append(err)

    results = run_batch(operations, store, dry_run=dry_run, stop_on_error=stop_on_error)
    output_lines = "\n".join(json.dumps(r) for r in results) + "\n"

    if output:
        output.write_text(output_lines)
    else:
        __import__("sys").stdout.write(output_lines)


# ── migrate ───────────────────────────────────────────────────────────────────

@main.group("migrate")
def migrate_group() -> None:
    """pgvector schema migration commands."""


def _migration_runner(cwd: Path, migrations_dir: Path | None) -> tuple:
    from ncp.config import load_config
    from ncp.stores.migrations import MigrationRunner

    try:
        import psycopg2
    except ImportError:
        console.print("[red]psycopg2 not installed — pip install neural-context-protocol[pgvector][/red]")
        raise SystemExit(1)

    config = load_config(cwd=cwd)
    if config.store_type != "pgvector":
        console.print("[red]migrate commands require store.type = pgvector in .ncp/config.toml[/red]")
        raise SystemExit(1)

    conn = psycopg2.connect(config.pgvector_dsn)
    runner = MigrationRunner(
        conn,
        schema=config.pgvector_schema,
        prefix=config.pgvector_table_prefix,
        migrations_dir=migrations_dir,
    )
    runner.bootstrap()
    return conn, runner


@migrate_group.command("check")
@click.option("--cwd", type=click.Path(path_type=Path, exists=True, file_okay=False), default=Path.cwd)
@click.option("--migrations-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), default=None, help="Custom migrations directory.")
def migrate_check(cwd: Path, migrations_dir: Path | None) -> None:
    """Show applied, pending, and checksum-mismatched migrations."""
    from ncp.stores.migrations import MigrationStatus

    conn, runner = _migration_runner(cwd, migrations_dir)
    try:
        status: MigrationStatus = runner.check()
    finally:
        conn.close()

    t_applied = Table("Version", "Name", "Applied At", title="Applied", box=box.SIMPLE)
    for row in status.applied:
        ts = datetime.fromtimestamp(row["applied_at"]).strftime("%Y-%m-%d %H:%M:%S")
        t_applied.add_row(str(row["version"]), row["name"], ts)
    console.print(t_applied)

    if status.pending:
        t_pending = Table("Version", "Name", title="Pending", box=box.SIMPLE)
        for mf in status.pending:
            t_pending.add_row(str(mf.version), mf.name)
        console.print(t_pending)
    else:
        console.print("[green]No pending migrations.[/green]")

    if status.mismatches:
        console.print("[red bold]Checksum mismatches detected:[/red bold]")
        for m in status.mismatches:
            console.print(f"  v{m['version']} {m['name']}: stored={m['stored_checksum'][:12]}… file={m['file_checksum'][:12]}…")
        raise SystemExit(1)


@migrate_group.command("apply")
@click.option("--cwd", type=click.Path(path_type=Path, exists=True, file_okay=False), default=Path.cwd)
@click.option("--dry-run", is_flag=True, default=False, help="Print SQL without executing.")
@click.option("--migrations-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), default=None)
def migrate_apply(cwd: Path, dry_run: bool, migrations_dir: Path | None) -> None:
    """Apply all pending migrations."""
    from ncp.stores.migrations import MigrationError

    conn, runner = _migration_runner(cwd, migrations_dir)
    try:
        applied = runner.apply_all(dry_run=dry_run)
    except MigrationError as exc:
        console.print(f"[red]{exc}[/red]")
        conn.close()
        raise SystemExit(1)
    finally:
        conn.close()

    if not applied:
        console.print("[dim]Nothing to apply.[/dim]")
        return
    if dry_run:
        for item in applied:
            console.print(f"[yellow]-- v{item['version']} {item['name']} (dry run)[/yellow]")
            console.print(item["sql"])
    else:
        for item in applied:
            console.print(f"[green]Applied v{item['version']} {item['name']}[/green]")


@migrate_group.command("rollback")
@click.argument("version", type=int)
@click.option("--cwd", type=click.Path(path_type=Path, exists=True, file_okay=False), default=Path.cwd)
@click.option("--dry-run", is_flag=True, default=False, help="Print DOWN SQL without executing.")
@click.option("--migrations-dir", type=click.Path(path_type=Path, exists=True, file_okay=False), default=None)
def migrate_rollback(version: int, cwd: Path, dry_run: bool, migrations_dir: Path | None) -> None:
    """Roll back a specific migration version (must be the highest applied)."""
    from ncp.stores.migrations import MigrationError

    conn, runner = _migration_runner(cwd, migrations_dir)
    try:
        result = runner.rollback(version, dry_run=dry_run)
    except MigrationError as exc:
        console.print(f"[red]{exc}[/red]")
        conn.close()
        raise SystemExit(1)
    finally:
        conn.close()

    if dry_run:
        console.print(f"[yellow]-- v{result['version']} {result['name']} rollback (dry run)[/yellow]")
        console.print(result["sql"])
    else:
        console.print(f"[green]Rolled back v{result['version']} {result['name']}[/green]")


if __name__ == "__main__":
    main()
