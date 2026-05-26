"""CLI entrypoint for the NCP package."""

from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
@click.option("--emit-type", default="share", show_default=True)
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
@click.option("--emit-type", default="share", show_default=True)
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


if __name__ == "__main__":
    main()
