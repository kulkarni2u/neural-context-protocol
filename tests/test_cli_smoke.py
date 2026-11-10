"""CliRunner smoke tests against a temporary SQLite store.

Covers commands that previously had no CLI test (viz, consolidate, refine show,
identity list, migrate check) plus --version, doctor, --json aliases and the
Authorization header written into generated agent configs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import ncp
from ncp.cli import main


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NCP_AUTH_TOKEN", "NCP_HOST", "NCP_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert ncp.__version__ in result.output


def test_viz_renders_and_emits_json(project: Path) -> None:
    runner = CliRunner()
    text = runner.invoke(main, ["viz", "--cwd", str(project)])
    assert text.exit_code == 0, text.output
    assert "NCP Viz" in text.output

    for flag in ("--json", "--json-output"):
        result = runner.invoke(main, ["viz", "--cwd", str(project), flag])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "chunk_distribution" in data and "whisper_queue" in data


def test_consolidate_dry_run_and_json(project: Path) -> None:
    runner = CliRunner()
    text = runner.invoke(main, ["consolidate", "--cwd", str(project), "--dry-run"])
    assert text.exit_code == 0, text.output
    assert "Consolidation Report" in text.output

    result = runner.invoke(main, ["consolidate", "--cwd", str(project), "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["dry_run"] is True and data["merged"] == 0


def test_refine_show_roundtrip_and_missing_key(project: Path) -> None:
    runner = CliRunner()
    missing = runner.invoke(main, ["refine", "show", "nope", "--cwd", str(project)])
    assert missing.exit_code != 0
    assert "no procedure ingested" in missing.output

    ingest = runner.invoke(
        main, ["refine", "ingest", "deploy", "--content", "run the tests first", "--cwd", str(project)]
    )
    assert ingest.exit_code == 0, ingest.output
    shown = runner.invoke(main, ["refine", "show", "deploy", "--cwd", str(project)])
    assert shown.exit_code == 0, shown.output
    assert "run the tests first" in shown.output
    history = runner.invoke(main, ["refine", "show", "deploy", "--history", "--cwd", str(project)])
    assert history.exit_code == 0, history.output


def test_identity_list_table_and_json(project: Path) -> None:
    runner = CliRunner()
    created = runner.invoke(main, ["identity", "create", "--cwd", str(project), "--label", "ci"])
    assert created.exit_code == 0, created.output
    identity_id = created.output.strip()

    table = runner.invoke(main, ["identity", "list", "--cwd", str(project)])
    assert table.exit_code == 0, table.output

    for flag in ("--json", "--json-output"):
        result = runner.invoke(main, ["identity", "list", "--cwd", str(project), flag])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert [r["identity_id"] for r in rows] == [identity_id]
        assert rows[0]["label"] == "ci"


def test_json_alias_accepted_wherever_json_output_is(project: Path) -> None:
    runner = CliRunner()
    for cmd in (["status"], ["cost"], ["explain"], ["reputation"], ["trust-drift"]):
        for flag in ("--json", "--json-output"):
            result = runner.invoke(main, [*cmd, "--cwd", str(project), flag])
            assert result.exit_code == 0, (cmd, flag, result.output)
            json.loads(result.output)


def test_migrate_check_without_driver_fails_clearly(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".ncp" / "config.toml").write_text('[store]\ntype = "pgvector"\n')
    monkeypatch.setitem(sys.modules, "psycopg", None)  # makes `import psycopg` raise ImportError

    result = CliRunner().invoke(main, ["migrate", "check", "--cwd", str(project)])

    assert result.exit_code == 1
    assert "psycopg (v3) is not installed" in result.output
    assert "psycopg2" not in result.output


def test_migrate_check_json_with_fake_driver(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only exercised against a fake psycopg module; no real Postgres is involved."""
    (project / ".ncp" / "config.toml").write_text('[store]\ntype = "pgvector"\n')

    class FakeCursor:
        def execute(self, *args: object, **kwargs: object) -> None:
            pass

        def fetchall(self) -> list:
            return []  # nothing applied

        def close(self) -> None:
            pass

    class FakeConn:
        closed = False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    conns: list[FakeConn] = []

    def fake_connect(dsn: str, **kwargs: object) -> FakeConn:
        conns.append(FakeConn())
        return conns[-1]

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect))

    result = CliRunner().invoke(main, ["migrate", "check", "--cwd", str(project), "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["applied"] == [] and data["mismatches"] == []
    assert data["pending"], "packaged migrations should be pending against an empty database"
    assert conns and conns[0].closed


# ── doctor ────────────────────────────────────────────────────────────────────


def _doctor(project: Path, *extra: str) -> tuple[int, dict]:
    result = CliRunner().invoke(
        main, ["doctor", "--cwd", str(project), "--port", "1", "--json", *extra]
    )
    return result.exit_code, json.loads(result.output)


def test_doctor_sqlite_default_has_no_failures(project: Path) -> None:
    code, report = _doctor(project)
    assert code == 0
    assert report["ok"] is True
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["config"]["status"] == "ok"
    assert by_name["store (sqlite)"]["status"] == "ok"
    assert by_name["server"]["status"] == "warn"  # nothing listens on port 1
    assert "not running" in by_name["server"]["detail"]
    assert by_name["server"]["fix"]
    assert by_name["auth"]["status"] == "ok"
    assert {"embeddings", "optional extras"} <= set(by_name)


def test_doctor_text_output_and_missing_config(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["doctor", "--cwd", str(tmp_path), "--port", "1"])
    assert result.exit_code == 0, result.output
    assert "WARN config" in result.output
    assert "fix:" in result.output


def test_doctor_fails_on_broken_config(project: Path) -> None:
    (project / ".ncp" / "config.toml").write_text("this is [not toml")
    code, report = _doctor(project)
    assert code == 1
    assert report["ok"] is False
    assert report["checks"][0]["status"] == "fail"


def test_doctor_fails_on_corrupt_sqlite_file(project: Path) -> None:
    (project / ".ncp" / "store.db").write_bytes(b"not a sqlite database" * 50)
    code, report = _doctor(project)
    assert code == 1
    assert any(c["name"] == "store (sqlite)" and c["status"] == "fail" for c in report["checks"])


def test_doctor_pgvector_without_driver_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".ncp" / "config.toml").write_text('[store]\ntype = "pgvector"\n')
    monkeypatch.setitem(sys.modules, "psycopg", None)
    code, report = _doctor(project)
    assert code == 1
    store = next(c for c in report["checks"] if c["name"] == "store (pgvector)")
    assert store["status"] == "fail" and "pgvector" in store["fix"]


def test_doctor_warns_when_token_set_but_mcp_json_lacks_header(project: Path) -> None:
    (project / ".ncp" / "config.toml").write_text('[server]\nauth_token = "s3cret"\n')
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"ncp": {"type": "http", "url": "http://127.0.0.1:4242/mcp"}}})
    )
    code, report = _doctor(project)
    auth = next(c for c in report["checks"] if c["name"] == "auth")
    assert code == 0 and auth["status"] == "warn" and "401" in auth["detail"]
    assert "s3cret" not in json.dumps(report)

    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"ncp": {"headers": {"Authorization": "Bearer ${NCP_AUTH_TOKEN}"}}}})
    )
    _, report = _doctor(project)
    assert next(c for c in report["checks"] if c["name"] == "auth")["status"] == "ok"


def test_doctor_embeddings_check_follows_fastembed_availability(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ncp.cli._module_available", lambda name: name != "fastembed")
    _, report = _doctor(project)
    emb = next(c for c in report["checks"] if c["name"] == "embeddings")
    assert emb["status"] == "warn" and "local-embeddings" in emb["fix"]

    monkeypatch.setattr("ncp.cli._module_available", lambda name: True)
    _, report = _doctor(project)
    assert next(c for c in report["checks"] if c["name"] == "embeddings")["status"] == "ok"


# ── init: Authorization header in generated agent configs ─────────────────────


def _fake_tty_with_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ncp.cli._init_is_tty", lambda: True, raising=False)
    monkeypatch.setattr(
        "ncp.cli.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"claude", "opencode"} else None,
    )


def test_init_pgvector_writes_env_var_auth_header_not_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tty_with_hosts(monkeypatch)
    monkeypatch.setattr("ncp.cli.psycopg_available", lambda: True)

    result = CliRunner().invoke(
        main,
        ["init", "--cwd", str(tmp_path), "--store", "pgvector", "--infra", "byo"],
        input="\n\ny\ny\nn\n",  # DSN, Redis URL, Claude hooks, OpenCode hooks, skip migrations
    )

    token = next(
        line.split('"')[1] for line in (tmp_path / ".ncp" / "config.toml").read_text().splitlines()
        if line.startswith("auth_token")
    )
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["ncp"]["headers"] == {"Authorization": "Bearer ${NCP_AUTH_TOKEN}"}
    assert mcp["mcpServers"]["ncp"]["url"].endswith("/mcp")
    oc = json.loads((tmp_path / "opencode.json").read_text())
    assert oc["mcp"]["ncp"]["headers"] == {"Authorization": "Bearer {env:NCP_AUTH_TOKEN}"}
    for committed in (".mcp.json", "opencode.json"):
        assert token not in (tmp_path / committed).read_text()
    assert "NCP_AUTH_TOKEN" in result.output


def test_init_sqlite_has_no_auth_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tty_with_hosts(monkeypatch)
    result = CliRunner().invoke(
        main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"], input="y\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert "headers" not in json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["ncp"]


def test_init_does_not_modify_existing_mcp_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_tty_with_hosts(monkeypatch)
    monkeypatch.setattr("ncp.cli.psycopg_available", lambda: True)
    (tmp_path / ".mcp.json").write_text("{}\n")
    result = CliRunner().invoke(
        main,
        ["init", "--cwd", str(tmp_path), "--store", "pgvector", "--infra", "byo"],
        input="\n\ny\ny\nn\n",
    )
    assert (tmp_path / ".mcp.json").read_text() == "{}\n"
    assert "already exists" in result.output


def test_init_pgvector_without_driver_exits_nonzero_but_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    result = CliRunner().invoke(
        main, ["init", "--cwd", str(tmp_path), "--store", "pgvector", "--infra", "byo"]
    )
    assert result.exit_code == 1
    assert "psycopg (v3) is not installed" in result.output
    assert (tmp_path / ".ncp" / "config.toml").exists()


# ── psycopg v3 call sites (fake driver only) ──────────────────────────────────


def test_wait_for_pg_raises_when_driver_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from ncp.cli import _wait_for_pg
    from ncp.stores.migrations import MigrationError

    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(MigrationError, match="psycopg"):
        _wait_for_pg("postgresql://x/y", timeout=1)


def test_wait_for_pg_uses_psycopg3_connect_with_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from ncp.cli import _wait_for_pg

    calls: list[tuple] = []

    class Conn:
        def close(self) -> None:
            calls.append(("close",))

    def connect(dsn: str, **kwargs: object) -> Conn:
        calls.append((dsn, kwargs))
        return Conn()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    assert _wait_for_pg("postgresql://x/y", timeout=5) is True
    assert calls[0] == ("postgresql://x/y", {"connect_timeout": 3})


def test_wait_for_pg_times_out_when_connection_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from ncp.cli import _wait_for_pg

    def connect(dsn: str, **kwargs: object) -> None:
        raise OSError("refused")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    assert _wait_for_pg("postgresql://x/y", timeout=0) is False
