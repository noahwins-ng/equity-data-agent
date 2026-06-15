from pathlib import Path

import pytest

from scripts.migrate_clickhouse import MigrationError, run_migrations, split_sql_statements


class FakeClickHouse:
    def __init__(self, applied: set[str] | None = None, fail_on: str | None = None) -> None:
        self.applied = applied or set()
        self.fail_on = fail_on
        self.executed: list[str] = []

    def execute(self, sql: str) -> str:
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise MigrationError("boom")
        if "SELECT filename FROM schema_migrations" in sql:
            return "\n".join(sorted(self.applied))
        if sql.startswith("INSERT INTO schema_migrations"):
            filename = sql.split("VALUES ('", 1)[1].split("'", 1)[0]
            self.applied.add(filename)
        return ""


def write_migration(tmp_path: Path, filename: str, sql: str) -> None:
    (tmp_path / filename).write_text(sql)


def test_split_sql_statements_keeps_semicolons_inside_strings() -> None:
    sql = "SELECT 'one;two';\nSELECT \"three;four\";"

    assert split_sql_statements(sql) == ["SELECT 'one;two'", 'SELECT "three;four"']


def test_run_migrations_skips_already_applied_files(tmp_path: Path) -> None:
    write_migration(tmp_path, "001_first.sql", "SELECT 1;")
    write_migration(tmp_path, "002_second.sql", "SELECT 2;")
    client = FakeClickHouse(applied={"001_first.sql"})

    applied_count, skipped_count = run_migrations(tmp_path, client)  # type: ignore[arg-type]

    assert (applied_count, skipped_count) == (1, 1)
    assert not any(sql == "SELECT 1" for sql in client.executed)
    assert "SELECT 2" in client.executed
    assert "002_second.sql" in client.applied


def test_run_migrations_stops_on_first_failure(tmp_path: Path) -> None:
    write_migration(tmp_path, "001_first.sql", "SELECT broken;")
    write_migration(tmp_path, "002_second.sql", "SELECT 2;")
    client = FakeClickHouse(fail_on="broken")

    with pytest.raises(MigrationError):
        run_migrations(tmp_path, client)  # type: ignore[arg-type]

    assert "002_second.sql" not in client.applied
    assert not any(sql == "SELECT 2" for sql in client.executed)
