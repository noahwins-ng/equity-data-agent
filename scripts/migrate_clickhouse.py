"""Apply ClickHouse SQL migrations with schema_migrations tracking."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename String,
    applied_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(applied_at)
ORDER BY filename
"""


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied."""


@dataclass
class ClickHouseHTTP:
    url: str

    def execute(self, sql: str) -> str:
        data = sql.encode("utf-8")
        request = urllib.request.Request(self.url, data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MigrationError(detail.strip() or f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MigrationError(str(exc.reason)) from exc


def clickhouse_url() -> str:
    host = os.environ.get("CLICKHOUSE_HOST", "localhost")
    port = os.environ.get("CLICKHOUSE_PORT", "8123")
    user = os.environ.get("CLICKHOUSE_USER", "")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")

    query: dict[str, str] = {}
    if user:
        query["user"] = user
    if password:
        query["password"] = password

    encoded = urllib.parse.urlencode(query)
    suffix = f"?{encoded}" if encoded else ""
    return f"http://{host}:{port}/{suffix}"


def split_sql_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False

    for char in sql:
        current.append(char)
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in {"'", '"'}:
            quote = char
        elif char == ";":
            statement = "".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def applied_filenames(client: ClickHouseHTTP) -> set[str]:
    client.execute(TRACKING_TABLE_SQL)
    rows = client.execute("SELECT filename FROM schema_migrations FINAL FORMAT TabSeparated")
    return {line.strip() for line in rows.splitlines() if line.strip()}


def run_migrations(migrations_dir: Path, client: ClickHouseHTTP) -> tuple[int, int]:
    applied = applied_filenames(client)
    applied_count = 0
    skipped_count = 0

    for path in sorted(migrations_dir.glob("*.sql")):
        filename = path.name
        if filename in applied:
            print(f"Skipping {filename} (already applied)")
            skipped_count += 1
            continue

        print(f"Running {filename}...")
        statements = split_sql_statements(path.read_text())
        for statement in statements:
            client.execute(statement)
        client.execute(
            "INSERT INTO schema_migrations (filename, applied_at) "
            f"VALUES ({sql_literal(filename)}, now())"
        )
        print(f"Applied {filename}")
        applied_count += 1

    return applied_count, skipped_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--migrations-dir",
        default="migrations",
        type=Path,
        help="Directory containing *.sql migrations",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = ClickHouseHTTP(clickhouse_url())
    try:
        applied_count, skipped_count = run_migrations(args.migrations_dir, client)
    except MigrationError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {applied_count} applied, {skipped_count} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
