"""CLI entrypoint for the NCP package."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

import ncp
from ncp.stores.base import NCPStoreUnavailableError
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

console = Console()


CLAUDE_MD_TEMPLATE = """# NCP Conventions

- Call `ncp_get_context` at the start of each turn once the MCP server exists.
- Write durable memory with `ncp_write_memory` at the end of each turn.
- Keep context bounded and prefer recent refs over full-history replay.
"""


def _load_config_template() -> str:
    return resources.files("ncp").joinpath("templates/config.toml.example").read_text()


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
def status_command(cwd: Path) -> None:
    """Show basic SQLite-first NCP store status."""

    try:
        config = ncp.configure(cwd=cwd)
        store = SQLiteStore(config.store_path)
        status = store.status()
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    table = Table(title="NCP Status")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Store", str(config.store_path))
    table.add_row("Chunks", str(status["chunk_count"]))
    table.add_row("Whispers", str(status["whisper_count"]))
    table.add_row("Turn records", str(status["turn_record_count"]))
    table.add_row("Cost USD", f"{status['cost_usd_total']:.4f}")
    console.print(table)


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
    """Emit a manual whisper into the SQLite store."""

    try:
        config = ncp.configure(cwd=cwd)
        store = SQLiteStore(config.store_path)
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
    except NCPStoreUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    console.print("Whisper emitted.")


if __name__ == "__main__":
    main()
