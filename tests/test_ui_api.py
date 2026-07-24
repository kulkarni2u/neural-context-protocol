"""Tests for the read-only memory-visualization HTTP layer: /ui and /api/*."""

from __future__ import annotations

from contextlib import closing
import http.client
import socket
import threading
from pathlib import Path

import httpx
import pytest

from ncp.mcp.server import create_http_server
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk, TurnRecord, Whisper


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _chunk(
    *,
    chunk_id: str,
    content: str = "some content",
    layer: str = "semantic",
    zone: str = "working",
    pipeline_id: str | None = "pipe1",
    src: str = "tool_result",
    written_by: str = "executor",
    base_trust: float = 0.7,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content=content,
        layer=layer,
        zone=zone,
        pipeline_id=pipeline_id,
        src=src,
        written_by=written_by,
        base_trust=base_trust,
    )


class _RunningServer:
    """Context manager: start an HTTP server in a background thread."""

    def __init__(self, tmp_path: Path, **kwargs: object) -> None:
        self.port = _free_port()
        self.server = create_http_server(host="127.0.0.1", port=self.port, cwd=tmp_path, **kwargs)  # type: ignore[arg-type]

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server._shutdown_event.set()
        self.server.shutdown()
        self.thread.join(timeout=5)

    @property
    def store(self) -> SQLiteStore:
        return self.server.store  # type: ignore[return-value]

    def client(self, **kwargs: object) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=5.0, **kwargs)  # type: ignore[arg-type]


class TestApiStatus:
    def test_status_shape(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="c1"))
            running.store.write(_chunk(chunk_id="c2", pipeline_id="pipe2"))
            with running.client() as client:
                resp = client.get("/api/status")
                assert resp.status_code == 200
                assert resp.headers["content-type"] == "application/json"
                assert resp.headers["cache-control"] == "no-store"
                body = resp.json()
                assert set(body.keys()) == {"overview", "layer_counts", "recent_pipelines", "memoization"}
                assert body["overview"]["chunk_count"] == 2
                assert body["layer_counts"].get("semantic") == 2
                assert body["memoization"] == {
                    "hits": 0,
                    "misses": 0,
                    "entry_count": 0,
                    "estimated_tokens_saved": 0,
                }

    def test_status_pipeline_filter(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="c1", pipeline_id="pipe1"))
            running.store.write(_chunk(chunk_id="c2", pipeline_id="pipe2"))
            with running.client() as client:
                resp = client.get("/api/status", params={"pipeline_id": "pipe1"})
                assert resp.json()["overview"]["chunk_count"] == 1


class TestApiChunks:
    def test_chunks_shape_and_tombstone_flag(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="c_live", content="alive content"))
            running.store.write(_chunk(chunk_id="c_dead", content="dead content"))
            # SQLiteStore.tombstone() hard-deletes the chunks row (it's a
            # forward-ref dead-link mechanism, not a soft-delete flag), so a
            # normally-tombstoned chunk never coexists with a chunks row. The
            # contract still asks list_chunks to flag any chunk_id present in
            # both tables (e.g. a chunk_id rewritten after tombstoning, which
            # leaves a stale tombstones row behind). Simulate that directly.
            with running.store._connect() as connection:
                connection.execute(
                    "INSERT INTO tombstones (chunk_id, forward_ref, tombstoned_at, expires_at)"
                    " VALUES (?, ?, ?, ?)",
                    ("c_dead", None, 1.0, 999999999999.0),
                )
            with running.client() as client:
                resp = client.get("/api/chunks", params={"pipeline_id": "pipe1"})
                assert resp.status_code == 200
                body = resp.json()
                assert body["total"] == 2
                by_id = {c["chunk_id"]: c for c in body["chunks"]}
                assert by_id["c_live"]["tombstoned"] is False
                assert by_id["c_dead"]["tombstoned"] is True
                # Embedding bytes must never leak into the response.
                for c in body["chunks"]:
                    assert "embedding" not in c
                expected_keys = {
                    "chunk_id", "pipeline_id", "scope", "zone", "layer", "chunk_type",
                    "content", "src", "written_by", "caused_by", "supersedes",
                    "superseded_by", "version", "base_trust", "created_at", "tombstoned",
                }
                assert set(by_id["c_live"].keys()) == expected_keys

    def test_chunks_filters(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="a", layer="semantic", written_by="executor"))
            running.store.write(_chunk(chunk_id="b", layer="episodic", written_by="reviewer"))
            with running.client() as client:
                resp = client.get("/api/chunks", params={"layer": "episodic"})
                body = resp.json()
                assert body["total"] == 1
                assert body["chunks"][0]["chunk_id"] == "b"

                resp = client.get("/api/chunks", params={"written_by": "executor"})
                body = resp.json()
                assert body["total"] == 1
                assert body["chunks"][0]["chunk_id"] == "a"

    def test_chunks_limit_offset_and_total_ignores_paging(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            for i in range(5):
                running.store.write(_chunk(chunk_id=f"c{i}", content=f"content {i}"))
            with running.client() as client:
                resp = client.get("/api/chunks", params={"limit": 2, "offset": 1})
                body = resp.json()
                assert body["total"] == 5
                assert len(body["chunks"]) == 2

    def test_chunks_ordered_created_at_desc(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="older", content="older"))
            running.store.write(_chunk(chunk_id="newer", content="newer"))
            with running.client() as client:
                body = client.get("/api/chunks").json()
                ids = [c["chunk_id"] for c in body["chunks"]]
                created_ats = [c["created_at"] for c in body["chunks"]]
                assert created_ats == sorted(created_ats, reverse=True)
                assert ids.index("newer") < ids.index("older")

    def test_chunks_invalid_limit_is_400(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                resp = client.get("/api/chunks", params={"limit": "not-a-number"})
                assert resp.status_code == 400
                assert resp.json()["error"] == "invalid_param"
                assert "detail" in resp.json()

    def test_chunks_limit_clamped(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            for i in range(3):
                running.store.write(_chunk(chunk_id=f"clamp{i}", content=f"clamp test content {i}"))
            with running.client() as client:
                # limit below 1 clamps up to 1, not an error.
                resp = client.get("/api/chunks", params={"limit": 0})
                assert resp.status_code == 200
                assert len(resp.json()["chunks"]) == 1


class TestApiTurns:
    def test_turns_chronological_order(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            for i, created_at in enumerate((100.0, 200.0, 300.0)):
                running.store.log_turn_record(TurnRecord(
                    turn_id=f"turn_{i}",
                    agent_id="executor",
                    pipeline_id="pipe1",
                    task="fix_bug",
                    slot="payment",
                    result=f"summary_{i}",
                    result_full=f"full_{i}",
                    created_at=created_at,
                    expires_at=created_at + 1000,
                ))
            with running.client() as client:
                resp = client.get("/api/turns", params={"pipeline_id": "pipe1"})
                assert resp.status_code == 200
                body = resp.json()
                turn_ids = [t["turn_id"] for t in body["turns"]]
                assert turn_ids == ["turn_0", "turn_1", "turn_2"]
                assert body["turns"][0]["result_full"] == "full_0"
                assert body["turns"][0]["agent_id"] == "executor"

    def test_turns_without_pipeline_spans_all_pipelines(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            for i, pipeline in enumerate(("pipe_a", "pipe_b")):
                running.store.log_turn_record(TurnRecord(
                    turn_id=f"turn_{i}",
                    agent_id="executor",
                    pipeline_id=pipeline,
                    task="fix_bug",
                    slot="payment",
                    result=f"summary_{i}",
                    result_full=f"full_{i}",
                    created_at=100.0 + i,
                    expires_at=2000.0,
                ))
            with running.client() as client:
                resp = client.get("/api/turns")
                assert resp.status_code == 200
                pipelines = {t["pipeline_id"] for t in resp.json()["turns"]}
                assert pipelines == {"pipe_a", "pipe_b"}


class TestApiWhispers:
    def test_whispers_default_excludes_expired(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.emit_whisper(Whisper(
                from_agent="executor", target="reviewer", whisper_type="nudge",
                payload="live", confidence=0.9, pipeline_id="pipe1", ttl_seconds=3600,
            ))
            running.store.emit_whisper(Whisper(
                from_agent="executor", target="reviewer", whisper_type="nudge",
                payload="expired", confidence=0.9, pipeline_id="pipe1",
                created_at=1.0, ttl_seconds=1,
            ))
            with running.client() as client:
                resp = client.get("/api/whispers", params={"pipeline_id": "pipe1"})
                assert resp.status_code == 200
                payloads = [w["payload"] for w in resp.json()["whispers"]]
                assert payloads == ["live"]

                resp = client.get(
                    "/api/whispers", params={"pipeline_id": "pipe1", "include_expired": "true"}
                )
                payloads = {w["payload"] for w in resp.json()["whispers"]}
                assert payloads == {"live", "expired"}
                by_payload = {w["payload"]: w for w in resp.json()["whispers"]}
                assert by_payload["expired"]["expired"] is True
                assert by_payload["live"]["expired"] is False

    def test_whispers_invalid_include_expired_is_400(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                resp = client.get("/api/whispers", params={"include_expired": "maybe"})
                assert resp.status_code == 400
                assert resp.json()["error"] == "invalid_param"


class TestApiGraph:
    def test_graph_matches_store_graph_data(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.write(_chunk(chunk_id="g1"))
            with running.client() as client:
                resp = client.get("/api/graph", params={"pipeline_id": "pipe1"})
                assert resp.status_code == 200
                body = resp.json()
                assert set(body.keys()) == {"nodes", "edges"}
                assert body["nodes"][0]["chunk_id"] == "g1"


class TestApiCost:
    def test_cost_summary_shape(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            running.store.log_cost_raw(
                agent_id="executor",
                model="claude-x",
                input_tokens=10,
                output_tokens=20,
                cost_usd=0.05,
                pipeline_id="pipe1",
                turn_id="turn_cost_1",
                latency_ms=120,
            )
            with running.client() as client:
                resp = client.get("/api/cost", params={"pipeline_id": "pipe1"})
                assert resp.status_code == 200
                body = resp.json()
                assert set(body.keys()) >= {"summary", "by_agent", "by_model"}
                assert body["summary"]["cost_usd_total"] == pytest.approx(0.05)
                assert body["by_agent"][0]["agent_id"] == "executor"


class TestApiAuth:
    def test_401_without_token_200_with(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path, auth_token="secret") as running:
            with running.client() as client:
                denied = client.get("/api/status")
                assert denied.status_code == 401
                assert denied.json() == {"error": "unauthorized"}

                allowed = client.get("/api/status", headers={"Authorization": "Bearer secret"})
                assert allowed.status_code == 200

    def test_ui_requires_no_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("<html>ui</html>")
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path, auth_token="secret") as running:
            with running.client() as client:
                resp = client.get("/ui/")
                assert resp.status_code == 200
                assert "text/html" in resp.headers["content-type"]
                assert resp.text == "<html>ui</html>"


class TestApiUnknownAndNotFound:
    def test_unknown_api_path_is_404(self, tmp_path: Path) -> None:
        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                resp = client.get("/api/nonexistent")
                assert resp.status_code == 404


class TestUiStaticServing:
    def test_ui_serves_index_html(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("<html>hello ui</html>")
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                # Bare /ui redirects so relative asset URLs resolve under /ui/.
                redirect = client.get("/ui")
                assert redirect.status_code == 301
                assert redirect.headers["location"] == "/ui/"

                resp = client.get("/ui/")
                assert resp.status_code == 200
                assert resp.text == "<html>hello ui</html>"
                assert "text/html" in resp.headers["content-type"]

    def test_ui_serves_nested_asset_with_correct_content_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("<html></html>")
        (static_root / "app.js").write_text("console.log('hi');")
        (static_root / "style.css").write_text("body { color: red; }")
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                js = client.get("/ui/app.js")
                assert js.status_code == 200
                assert "javascript" in js.headers["content-type"]
                assert js.text == "console.log('hi');"

                css = client.get("/ui/style.css")
                assert css.status_code == 200
                assert "text/css" in css.headers["content-type"]

    def test_ui_missing_file_is_404(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                resp = client.get("/ui/missing.html")
                assert resp.status_code == 404
                assert resp.json()["error"] == "not_found"

    def test_ui_disallowed_extension_is_404(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "secret.txt").write_text("nope")
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path) as running:
            with running.client() as client:
                resp = client.get("/ui/secret.txt")
                assert resp.status_code == 404

    def test_ui_path_traversal_is_404(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        static_root = tmp_path / "static"
        static_root.mkdir()
        (static_root / "index.html").write_text("<html></html>")
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        (secret_dir / "secret.html").write_text("top secret")
        import ncp.mcp.server as server_mod
        monkeypatch.setattr(server_mod, "_ui_static_root", lambda: static_root)

        with _RunningServer(tmp_path) as running:
            # httpx normalizes ".." dot-segments client-side before sending the
            # request, which would make this test pass without ever exercising
            # the server's own traversal guard. Use http.client directly so the
            # literal ".." reaches the server unmodified.
            conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
            try:
                conn.request("GET", "/ui/../secrets/secret.html")
                resp = conn.getresponse()
                assert resp.status == 404
                resp.read()
            finally:
                conn.close()

            # Percent-encoded dot-segments must be rejected too.
            conn2 = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
            try:
                conn2.request("GET", "/ui/%2e%2e/secrets/secret.html")
                resp2 = conn2.getresponse()
                assert resp2.status == 404
                resp2.read()
            finally:
                conn2.close()
