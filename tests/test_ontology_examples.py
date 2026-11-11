"""Smoke test: examples/13_ontology seed + generic MCP client against an
ephemeral loopback ``ncp serve`` HTTP server (no external network)."""

from __future__ import annotations

import importlib
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ncp.mcp.server import create_http_server

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "13_ontology"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def server_url(tmp_path: Path) -> Iterator[str]:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".ncp").mkdir()
    config = (EXAMPLE / "config.toml").read_text(encoding="utf-8")
    # Generous time budgets keep the smoke test independent of machine load.
    config = config.replace("trigger_max_ms = 10", "trigger_max_ms = 1000").replace(
        "graph_timeout_ms = 50", "graph_timeout_ms = 5000")
    (root / ".ncp" / "config.toml").write_text(config, encoding="utf-8")
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(EXAMPLE))
    for name in ("mcp_http", "seed_graph", "mcp_client"):
        monkeypatch.delitem(__import__("sys").modules, name, raising=False)
    return importlib.import_module("seed_graph"), importlib.import_module("mcp_client")


def test_seed_and_generic_client(server_url: str, example, capsys) -> None:
    seed_graph, mcp_client = example
    assert seed_graph.main(["--url", server_url]) == 0
    assert seed_graph.main(["--url", server_url]) == 0  # idempotent replay
    capsys.readouterr()

    report = mcp_client.run(mcp_client.MCPClient(server_url))
    out = capsys.readouterr().out
    assert "ncp_ontology_query" in report["ontology_tools"]
    # Automatic mode: an unchanged ncp_get_context call gets the graph block.
    assert report["auto"]["triggered"] is True
    assert report["auto"]["has_ontology_block"] is True
    assert "Service:checkout -DEPENDS_ON-> Service:payments" in out
    # The candidate relation never reaches automatic context or verified queries.
    assert "Service:edge-gateway -DEPENDS_ON-> Service:payments" not in out
    assert "<-DEPENDS_ON- Service:edge-gateway" in out  # the verified edge-gateway edge
    # Explicit mode.
    assert report["query"]["resolved"] is True and report["query"]["paths"] > 0
    assert report["explain"]["verified_and_supported"] is True


def test_seed_reports_disabled_server(tmp_path: Path, example, capsys) -> None:
    seed_graph, _ = example
    root = tmp_path / "plain"
    (root / ".git").mkdir(parents=True)
    port = _free_port()
    server = create_http_server(host="127.0.0.1", port=port, cwd=root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert seed_graph.main(["--url", f"http://127.0.0.1:{port}/mcp"]) == 1
    finally:
        server._shutdown_event.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert "ontology is disabled" in capsys.readouterr().out


def test_example_config_is_valid() -> None:
    import tomllib

    from ncp.ontology.policy import OntologyConfig

    data = tomllib.loads((EXAMPLE / "config.toml").read_text(encoding="utf-8"))
    config = OntologyConfig.model_validate(data["ontology"])
    assert config.enabled and config.retrieval_mode == "auto"
