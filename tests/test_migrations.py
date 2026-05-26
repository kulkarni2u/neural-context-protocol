"""Unit and integration tests for ncp.stores.migrations."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ncp.stores.migrations import MigrationError, MigrationRunner


# ── helpers ───────────────────────────────────────────────────────────────────

def _sql_file(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


def _mock_conn(fetchall_rows: list | None = None, advisory_lock_ok: bool = True) -> MagicMock:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchone.return_value = (advisory_lock_ok,)
    cur.fetchall.return_value = fetchall_rows or []
    return conn


def _runner(conn: MagicMock, tmp_path: Path) -> MigrationRunner:
    return MigrationRunner(conn, schema="ncp", prefix="ncp_", migrations_dir=tmp_path)


# ── parse_sections ────────────────────────────────────────────────────────────

def test_parse_sections_up_and_down() -> None:
    sql = "-- UP\nCREATE TABLE foo ();\n-- DOWN\nDROP TABLE foo;"
    up, down = MigrationRunner.parse_sections(sql)
    assert "CREATE TABLE foo" in up
    assert "DROP TABLE foo" in down
    assert "-- UP" not in up
    assert "-- DOWN" not in up


def test_parse_sections_up_only() -> None:
    sql = "-- UP\nCREATE TABLE foo ();"
    up, down = MigrationRunner.parse_sections(sql)
    assert "CREATE TABLE foo" in up
    assert down == ""


def test_parse_sections_no_markers() -> None:
    sql = "CREATE TABLE foo ();"
    up, down = MigrationRunner.parse_sections(sql)
    assert up == "CREATE TABLE foo ();"
    assert down == ""


def test_parse_sections_case_insensitive_markers() -> None:
    sql = "-- up\nCREATE TABLE foo ();\n-- down\nDROP TABLE foo;"
    up, down = MigrationRunner.parse_sections(sql)
    assert "CREATE TABLE foo" in up
    assert "DROP TABLE foo" in down


def test_parse_sections_raises_if_down_before_up() -> None:
    sql = "-- DOWN\nDROP TABLE foo;\n-- UP\nCREATE TABLE foo ();"
    with pytest.raises(MigrationError, match="DOWN.*before.*UP"):
        MigrationRunner.parse_sections(sql)


# ── substitution ──────────────────────────────────────────────────────────────

def test_sub_replaces_schema_and_prefix() -> None:
    runner = MigrationRunner(MagicMock(), schema="myschema", prefix="my_")
    result = runner._sub("SELECT * FROM {schema}.{prefix}chunks;")
    assert result == "SELECT * FROM myschema.my_chunks;"


def test_sub_leaves_json_braces_untouched() -> None:
    runner = MigrationRunner(MagicMock(), schema="ncp", prefix="ncp_")
    sql = "meta JSONB DEFAULT '{}'::jsonb"
    assert runner._sub(sql) == sql  # no placeholders match


# ── discovery ─────────────────────────────────────────────────────────────────

def test_discover_finds_numbered_sql_files(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_initial.sql", "-- UP\nCREATE TABLE a ();")
    _sql_file(tmp_path, "002_add_col.sql", "-- UP\nALTER TABLE a ADD COLUMN b TEXT;")
    _sql_file(tmp_path, "README.md", "not sql")  # should be ignored

    runner = _runner(_mock_conn(), tmp_path)
    files = runner._discover()
    assert len(files) == 2
    assert [f.version for f in files] == [1, 2]


def test_discover_sorted_by_version(tmp_path: Path) -> None:
    _sql_file(tmp_path, "003_third.sql", "-- UP\nSELECT 1;")
    _sql_file(tmp_path, "001_first.sql", "-- UP\nSELECT 1;")
    _sql_file(tmp_path, "002_second.sql", "-- UP\nSELECT 1;")

    runner = _runner(_mock_conn(), tmp_path)
    files = runner._discover()
    assert [f.version for f in files] == [1, 2, 3]


def test_discover_checksum_is_sha256_of_raw_bytes(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE x ();"
    _sql_file(tmp_path, "001_x.sql", content)
    expected = hashlib.sha256(content.encode()).hexdigest()

    runner = _runner(_mock_conn(), tmp_path)
    files = runner._discover()
    assert files[0].checksum == expected


# ── bootstrap ─────────────────────────────────────────────────────────────────

def test_bootstrap_creates_schema_versions_table(tmp_path: Path) -> None:
    conn = _mock_conn()
    runner = _runner(conn, tmp_path)
    runner.bootstrap()

    cur = conn.cursor.return_value
    executed_sql = cur.execute.call_args[0][0]
    assert "schema_versions" in executed_sql
    assert "CREATE TABLE IF NOT EXISTS" in executed_sql
    conn.commit.assert_called_once()


# ── applied_versions ──────────────────────────────────────────────────────────

def test_applied_versions_returns_empty_when_none_applied(tmp_path: Path) -> None:
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    assert runner.applied_versions() == {}


def test_applied_versions_returns_dict_keyed_by_version(tmp_path: Path) -> None:
    rows = [(1, "001_initial_schema", "abc123", 1700000000.0)]
    conn = _mock_conn(fetchall_rows=rows)
    runner = _runner(conn, tmp_path)
    result = runner.applied_versions()
    assert 1 in result
    assert result[1]["name"] == "001_initial_schema"
    assert result[1]["checksum"] == "abc123"


# ── pending ───────────────────────────────────────────────────────────────────

def test_pending_returns_all_when_none_applied(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nSELECT 1;")
    _sql_file(tmp_path, "002_b.sql", "-- UP\nSELECT 2;")
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    assert [f.version for f in runner.pending()] == [1, 2]


def test_pending_excludes_applied(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nSELECT 1;")
    _sql_file(tmp_path, "002_b.sql", "-- UP\nSELECT 2;")
    rows = [(1, "001_a", "checksum_a", 1700000000.0)]
    conn = _mock_conn(fetchall_rows=rows)
    runner = _runner(conn, tmp_path)
    pending = runner.pending()
    assert len(pending) == 1
    assert pending[0].version == 2


# ── check ─────────────────────────────────────────────────────────────────────

def test_check_reports_mismatch_when_checksum_differs(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE x ();"
    _sql_file(tmp_path, "001_x.sql", content)
    rows = [(1, "001_x", "stale_checksum_does_not_match", 1700000000.0)]
    conn = _mock_conn(fetchall_rows=rows)
    runner = _runner(conn, tmp_path)
    status = runner.check()
    assert len(status.mismatches) == 1
    assert status.mismatches[0]["version"] == 1


def test_check_no_mismatch_when_checksum_matches(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE x ();"
    _sql_file(tmp_path, "001_x.sql", content)
    correct_checksum = hashlib.sha256(content.encode()).hexdigest()
    rows = [(1, "001_x", correct_checksum, 1700000000.0)]
    conn = _mock_conn(fetchall_rows=rows)
    runner = _runner(conn, tmp_path)
    status = runner.check()
    assert status.mismatches == []


# ── apply_all ─────────────────────────────────────────────────────────────────

def test_apply_all_dry_run_returns_sql_without_executing(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();\n-- DOWN\nDROP TABLE a;")
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    results = runner.apply_all(dry_run=True)
    assert len(results) == 1
    assert "CREATE TABLE a" in results[0]["sql"]
    # no actual execute beyond the applied_versions query
    execute_calls = [str(c) for c in conn.cursor.return_value.execute.call_args_list]
    assert not any("pg_try_advisory_lock" in c for c in execute_calls)


def test_apply_all_returns_empty_when_nothing_pending(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE a ();"
    _sql_file(tmp_path, "001_a.sql", content)
    checksum = hashlib.sha256(content.encode()).hexdigest()
    conn = _mock_conn(fetchall_rows=[(1, "001_a", checksum, 1700000000.0)])
    runner = _runner(conn, tmp_path)
    assert runner.apply_all() == []


def test_apply_all_acquires_and_releases_advisory_lock(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();")
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    runner.apply_all()
    cur = conn.cursor.return_value
    executed = [str(c) for c in cur.execute.call_args_list]
    assert any("pg_try_advisory_lock" in c for c in executed)
    assert any("pg_advisory_unlock" in c for c in executed)


def test_apply_all_raises_when_lock_not_acquired(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();")
    conn = _mock_conn(fetchall_rows=[], advisory_lock_ok=False)
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="advisory lock"):
        runner.apply_all()


def test_apply_all_raises_on_empty_up_section(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\n   \n-- DOWN\nDROP TABLE a;")
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="empty UP section"):
        runner.apply_all()


def test_apply_all_wraps_db_error_with_partial_state(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();")
    _sql_file(tmp_path, "002_b.sql", "-- UP\nCREATE TABLE b ();")
    conn = _mock_conn(fetchall_rows=[])

    def side_effect(sql, params=None):
        if "CREATE TABLE b" in str(sql):
            raise Exception("simulated DB failure")

    conn.cursor.return_value.execute.side_effect = side_effect
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="002_b.*failed after 1 applied"):
        runner.apply_all()


# ── rollback ──────────────────────────────────────────────────────────────────

def test_rollback_dry_run_returns_down_sql(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE a ();\n-- DOWN\nDROP TABLE a;"
    _sql_file(tmp_path, "001_a.sql", content)
    checksum = hashlib.sha256(content.encode()).hexdigest()
    conn = _mock_conn(fetchall_rows=[(1, "001_a", checksum, 1700000000.0)])
    runner = _runner(conn, tmp_path)
    result = runner.rollback(1, dry_run=True)
    assert "DROP TABLE a" in result["sql"]
    assert result["version"] == 1


def test_rollback_raises_if_version_not_applied(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();\n-- DOWN\nDROP TABLE a;")
    conn = _mock_conn(fetchall_rows=[])
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="not applied"):
        runner.rollback(1)


def test_rollback_raises_if_not_highest_version(tmp_path: Path) -> None:
    _sql_file(tmp_path, "001_a.sql", "-- UP\nCREATE TABLE a ();\n-- DOWN\nDROP TABLE a;")
    _sql_file(tmp_path, "002_b.sql", "-- UP\nCREATE TABLE b ();\n-- DOWN\nDROP TABLE b;")
    rows = [
        (1, "001_a", "checksum_a", 1700000000.0),
        (2, "002_b", "checksum_b", 1700000001.0),
    ]
    conn = _mock_conn(fetchall_rows=rows)
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="highest applied"):
        runner.rollback(1)


def test_rollback_raises_if_no_down_section(tmp_path: Path) -> None:
    content = "-- UP\nCREATE TABLE a ();"
    _sql_file(tmp_path, "001_a.sql", content)
    checksum = hashlib.sha256(content.encode()).hexdigest()
    conn = _mock_conn(fetchall_rows=[(1, "001_a", checksum, 1700000000.0)])
    runner = _runner(conn, tmp_path)
    with pytest.raises(MigrationError, match="no DOWN section"):
        runner.rollback(1)


# ── bundled 001 migration file ────────────────────────────────────────────────

def test_bundled_001_migration_has_up_and_down() -> None:
    from importlib import resources
    pkg = resources.files("ncp.migrations")
    sql_files = sorted(f for f in pkg.iterdir() if str(f).endswith(".sql"))
    assert len(sql_files) >= 1, "ncp/migrations/ must contain at least one .sql file"
    sql = Path(str(sql_files[0])).read_text()
    up, down = MigrationRunner.parse_sections(sql)
    assert "CREATE TABLE" in up
    assert "DROP TABLE" in down


def test_bundled_001_substitution_produces_no_placeholders() -> None:
    from importlib import resources
    pkg = resources.files("ncp.migrations")
    sql_files = sorted(str(f) for f in pkg.iterdir() if str(f).endswith(".sql"))
    runner = MigrationRunner(MagicMock(), schema="testschema", prefix="test_")
    sql = Path(sql_files[0]).read_text()
    up, _ = MigrationRunner.parse_sections(sql)
    substituted = runner._sub(up)
    assert "{schema}" not in substituted
    assert "{prefix}" not in substituted
    assert "testschema" in substituted


# ── integration (requires live Postgres) ─────────────────────────────────────

INTEGRATION = pytest.mark.skipif(
    not os.environ.get("NCP_RUN_PGVECTOR_INTEGRATION"),
    reason="set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live Postgres tests",
)


@INTEGRATION
def test_migration_apply_and_rollback_live(tmp_path: Path) -> None:
    import uuid
    import psycopg2

    dsn = os.environ["NCP_PGVECTOR_DSN"]
    schema = f"ncp_test_{uuid.uuid4().hex[:8]}"
    prefix = "ncp_"

    content = (
        f"-- UP\nCREATE SCHEMA IF NOT EXISTS {schema};\n"
        f"CREATE TABLE IF NOT EXISTS {schema}.{prefix}mig_test (id SERIAL PRIMARY KEY);\n"
        f"-- DOWN\nDROP TABLE IF EXISTS {schema}.{prefix}mig_test CASCADE;\n"
        f"DROP SCHEMA IF EXISTS {schema} CASCADE;\n"
    )
    _sql_file(tmp_path, "001_mig_test.sql", content)

    conn = psycopg2.connect(dsn)
    runner = MigrationRunner(conn, schema=schema, prefix=prefix, migrations_dir=tmp_path)
    try:
        runner.bootstrap()
        applied = runner.apply_all()
        assert len(applied) == 1
        assert applied[0]["version"] == 1

        # idempotent: applying again is a no-op
        assert runner.apply_all() == []

        # check shows it applied
        status = runner.check()
        assert len(status.applied) == 1
        assert status.pending == []

        # rollback
        result = runner.rollback(1)
        assert result["rolled_back"] is True
        assert runner.applied_versions() == {}
    finally:
        conn.close()
