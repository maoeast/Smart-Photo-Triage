from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import smart_photo_triage.database as database_module
from smart_photo_triage.database import (
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    connect_database,
)


class CommitFailsOnceConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.remaining_failures = 1

    @property
    def in_transaction(self) -> bool:
        return self.connection.in_transaction

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, parameters)

    def rollback(self) -> None:
        self.connection.rollback()

    def commit(self) -> None:
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise sqlite3.OperationalError("injected commit failure")
        self.connection.commit()


def schema_snapshot(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


def test_t_a_004_database_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "state.sqlite3"
    with closing(connect_database(database_path)) as connection:
        first_version = apply_migrations(connection)
        first_schema = schema_snapshot(connection)
        first_history = connection.execute(
            "SELECT version, name FROM schema_migration ORDER BY version"
        ).fetchall()

        second_version = apply_migrations(connection)
        second_schema = schema_snapshot(connection)
        second_history = connection.execute(
            "SELECT version, name FROM schema_migration ORDER BY version"
        ).fetchall()

    assert first_version >= 1
    assert second_version == first_version
    assert second_schema == first_schema
    assert second_history == first_history
    assert len(first_history) == first_version


def test_database_rejects_newer_unknown_schema(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "future.sqlite3")) as connection:
        connection.execute("PRAGMA user_version = 999")

        with pytest.raises(MigrationError, match="newer than supported"):
            apply_migrations(connection)


def test_database_rejects_migration_history_drift(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "drift.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("DELETE FROM schema_migration")
        connection.commit()

        with pytest.raises(MigrationError, match="history"):
            apply_migrations(connection)


def test_migration_failure_rolls_back_ddl_ledger_and_version_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(connect_database(tmp_path / "atomic.sqlite3")) as connection:
        assert apply_migrations(connection) == MIGRATIONS[-1].version
        next_version = MIGRATIONS[-1].version + 1
        broken = Migration(
            version=next_version,
            name="atomic_probe",
            statements=("CREATE TABLE atomic_probe (id INTEGER PRIMARY KEY)", "INVALID SQL"),
        )
        monkeypatch.setattr(database_module, "MIGRATIONS", (*database_module.MIGRATIONS, broken))

        with pytest.raises(sqlite3.Error):
            apply_migrations(connection)

        assert connection.execute("PRAGMA user_version").fetchone() == (MIGRATIONS[-1].version,)
        assert connection.execute(
            "SELECT version, name FROM schema_migration ORDER BY version"
        ).fetchall() == [(migration.version, migration.name) for migration in MIGRATIONS]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'atomic_probe'"
            ).fetchone()
            is None
        )

        repaired = Migration(
            version=next_version,
            name="atomic_probe",
            statements=("CREATE TABLE atomic_probe (id INTEGER PRIMARY KEY)",),
        )
        monkeypatch.setattr(
            database_module, "MIGRATIONS", (*database_module.MIGRATIONS[:-1], repaired)
        )
        assert apply_migrations(connection) == next_version


def test_commit_failure_rolls_back_transaction_and_same_connection_retries(
    tmp_path: Path,
) -> None:
    with closing(connect_database(tmp_path / "commit-failure.sqlite3")) as connection:
        proxy = CommitFailsOnceConnection(connection)

        with pytest.raises(sqlite3.OperationalError, match="injected commit failure"):
            apply_migrations(proxy)  # type: ignore[arg-type]

        assert connection.in_transaction is False
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            == []
        )
        assert apply_migrations(proxy) == MIGRATIONS[-1].version  # type: ignore[arg-type]


def test_database_rejects_ledger_name_drift(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "ledger-name.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("UPDATE schema_migration SET name = 'renamed' WHERE version = 1")
        connection.commit()

        with pytest.raises(MigrationError, match="ledger"):
            apply_migrations(connection)


def test_database_rejects_missing_critical_schema_object(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "missing-table.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("DROP TABLE workspace_metadata")
        connection.commit()

        with pytest.raises(MigrationError, match="schema"):
            apply_migrations(connection)


def test_database_rejects_critical_table_shape_drift(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "wrong-shape.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("DROP TABLE workspace_metadata")
        connection.execute(
            "CREATE TABLE workspace_metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        connection.commit()

        with pytest.raises(MigrationError, match="schema mismatch"):
            apply_migrations(connection)


def test_database_rejects_missing_ledger_name_uniqueness(tmp_path: Path) -> None:
    with closing(connect_database(tmp_path / "ledger-constraint.sqlite3")) as connection:
        apply_migrations(connection)
        connection.execute("ALTER TABLE schema_migration RENAME TO old_schema_migration")
        connection.execute(
            """
            CREATE TABLE schema_migration (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO schema_migration(version, name, applied_at)
            SELECT version, name, applied_at FROM old_schema_migration
            """
        )
        connection.execute("DROP TABLE old_schema_migration")
        connection.commit()

        with pytest.raises(MigrationError, match="uniqueness"):
            apply_migrations(connection)
