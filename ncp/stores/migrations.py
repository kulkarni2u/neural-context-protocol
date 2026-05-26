"""pgvector schema migration runner."""

from __future__ import annotations

import hashlib
import re
import struct
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass
class MigrationFile:
    version: int
    name: str
    path: Path
    checksum: str  # SHA-256 of raw file bytes (pre-substitution)


@dataclass
class MigrationStatus:
    applied: list[dict[str, Any]] = field(default_factory=list)
    pending: list[MigrationFile] = field(default_factory=list)
    mismatches: list[dict[str, Any]] = field(default_factory=list)


class MigrationError(Exception):
    pass


_DOWN_RE = re.compile(r"^\s*--\s*DOWN\s*$", re.MULTILINE | re.IGNORECASE)
_UP_RE = re.compile(r"^\s*--\s*UP\s*$", re.MULTILINE | re.IGNORECASE)


class MigrationRunner:
    def __init__(
        self,
        conn: Any,
        schema: str = "ncp",
        prefix: str = "ncp_",
        migrations_dir: Path | None = None,
    ) -> None:
        self._conn = conn
        self._schema = schema
        self._prefix = prefix
        self._migrations_dir = migrations_dir

    # ── internal ──────────────────────────────────────────────────────────────

    def _sub(self, sql: str) -> str:
        return sql.replace("{schema}", self._schema).replace("{prefix}", self._prefix)

    def _vtable(self) -> str:
        return f"{self._schema}.{self._prefix}schema_versions"

    def _lock_key(self) -> int:
        digest = hashlib.sha256(f"{self._schema}.{self._prefix}migrations".encode()).digest()
        return struct.unpack(">q", digest[:8])[0]

    def _discover(self) -> list[MigrationFile]:
        if self._migrations_dir is not None:
            paths = sorted(self._migrations_dir.glob("*.sql"))
        else:
            pkg = resources.files("ncp.migrations")
            paths = sorted(
                Path(str(f)) for f in pkg.iterdir() if str(f).endswith(".sql")
            )
        files: list[MigrationFile] = []
        for path in paths:
            stem = path.stem
            version_str = stem.split("_")[0]
            if not version_str.isdigit():
                continue
            raw = path.read_bytes()
            files.append(MigrationFile(
                version=int(version_str),
                name=stem,
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
            ))
        return sorted(files, key=lambda f: f.version)

    # ── public: SQL parsing ────────────────────────────────────────────────────

    @staticmethod
    def parse_sections(sql: str) -> tuple[str, str]:
        """Split SQL into (up_sql, down_sql). Strips -- UP / -- DOWN markers."""
        down_match = _DOWN_RE.search(sql)
        up_marker_full = _UP_RE.search(sql)
        if down_match and up_marker_full and up_marker_full.start() > down_match.start():
            raise MigrationError("-- DOWN marker appears before -- UP marker in migration file")
        if down_match:
            up_raw = sql[: down_match.start()]
            down = sql[down_match.end() :].strip()
        else:
            up_raw = sql
            down = ""
        up_marker = _UP_RE.search(up_raw)
        up = up_raw[up_marker.end() :].strip() if up_marker else up_raw.strip()
        return up, down

    # ── public: lifecycle ─────────────────────────────────────────────────────

    def bootstrap(self) -> None:
        """Create the schema_versions tracking table if absent."""
        sql = self._sub(
            "CREATE TABLE IF NOT EXISTS {schema}.{prefix}schema_versions ("
            "    version INTEGER PRIMARY KEY,"
            "    name TEXT NOT NULL,"
            "    checksum TEXT NOT NULL,"
            "    applied_at DOUBLE PRECISION NOT NULL"
            ");"
        )
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            self._conn.commit()
        finally:
            cur.close()

    def applied_versions(self) -> dict[int, dict[str, Any]]:
        """Return {version: {name, checksum, applied_at}} for all applied migrations."""
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT version, name, checksum, applied_at"
                f" FROM {self._vtable()} ORDER BY version"
            )
            return {
                row[0]: {"name": row[1], "checksum": row[2], "applied_at": row[3]}
                for row in cur.fetchall()
            }
        finally:
            cur.close()

    def pending(self) -> list[MigrationFile]:
        """Return migrations not yet applied, sorted by version ascending."""
        applied = set(self.applied_versions().keys())
        return [f for f in self._discover() if f.version not in applied]

    def check(self) -> MigrationStatus:
        """Return applied list, pending list, and checksum mismatches."""
        all_files = {f.version: f for f in self._discover()}
        applied = self.applied_versions()
        status = MigrationStatus()
        for version, info in applied.items():
            status.applied.append({
                "version": version,
                "name": info["name"],
                "applied_at": info["applied_at"],
            })
            if version in all_files and all_files[version].checksum != info["checksum"]:
                status.mismatches.append({
                    "version": version,
                    "name": info["name"],
                    "stored_checksum": info["checksum"],
                    "file_checksum": all_files[version].checksum,
                })
        applied_set = set(applied.keys())
        status.pending = [f for f in self._discover() if f.version not in applied_set]
        return status

    def apply_all(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        """Apply all pending migrations in version order."""
        pending = self.pending()
        if not pending:
            return []
        if dry_run:
            return [
                {
                    "version": mf.version,
                    "name": mf.name,
                    "sql": self._sub(self.parse_sections(mf.path.read_text())[0]),
                }
                for mf in pending
            ]
        lock_key = self._lock_key()
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            if not cur.fetchone()[0]:
                raise MigrationError(
                    "Could not acquire advisory lock — another migration may be running"
                )
            applied: list[dict[str, Any]] = []
            try:
                for mf in pending:
                    up_sql, _ = self.parse_sections(mf.path.read_text())
                    if not up_sql:
                        raise MigrationError(
                            f"Migration v{mf.version} ({mf.name}) has an empty UP section"
                        )
                    cur.execute(self._sub(up_sql))
                    cur.execute(
                        f"INSERT INTO {self._vtable()}"
                        " (version, name, checksum, applied_at) VALUES (%s, %s, %s, %s)",
                        (mf.version, mf.name, mf.checksum, time.time()),
                    )
                    self._conn.commit()
                    applied.append({"version": mf.version, "name": mf.name})
            except MigrationError:
                self._conn.rollback()
                raise
            except Exception as exc:
                self._conn.rollback()
                raise MigrationError(
                    f"Migration v{mf.version} ({mf.name}) failed after"
                    f" {len(applied)} applied: {exc}"
                ) from exc
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                    self._conn.commit()
                except Exception:
                    pass  # lock released automatically when connection closes
            return applied
        finally:
            cur.close()

    def rollback(self, version: int, *, dry_run: bool = False) -> dict[str, Any]:
        """Roll back the specified version. Must be the highest applied version."""
        applied = self.applied_versions()
        if version not in applied:
            raise MigrationError(f"Version {version} is not applied")
        max_applied = max(applied.keys())
        if version != max_applied:
            raise MigrationError(
                f"Can only roll back the highest applied version ({max_applied}), not {version}"
            )
        all_files = {f.version: f for f in self._discover()}
        if version not in all_files:
            raise MigrationError(f"Migration file for version {version} not found on disk")
        mf = all_files[version]
        _, down_sql = self.parse_sections(mf.path.read_text())
        if not down_sql:
            raise MigrationError(f"Migration {version} ({mf.name}) has no DOWN section")
        substituted = self._sub(down_sql)
        if dry_run:
            return {"version": version, "name": mf.name, "sql": substituted}
        lock_key = self._lock_key()
        cur = self._conn.cursor()
        try:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            if not cur.fetchone()[0]:
                raise MigrationError(
                    "Could not acquire advisory lock — another migration may be running"
                )
            try:
                cur.execute(substituted)
                cur.execute(f"DELETE FROM {self._vtable()} WHERE version = %s", (version,))
                self._conn.commit()
            except Exception as exc:
                self._conn.rollback()
                raise MigrationError(f"Rollback of v{version} failed: {exc}") from exc
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                    self._conn.commit()
                except Exception:
                    pass  # lock released automatically when connection closes
            return {"version": version, "name": mf.name, "rolled_back": True}
        finally:
            cur.close()
