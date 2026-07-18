"""Apply ClickHouse SQL migrations with schema_migrations tracking."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
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
    # QNT-381: credentials travel as X-ClickHouse-* headers, not ?user=/
    # ?password= query params — ClickHouse's HTTP handler logs request URIs,
    # so a URL-borne password would land in server logs.
    user: str = ""
    password: str = ""

    def execute(self, sql: str) -> str:
        data = sql.encode("utf-8")
        headers: dict[str, str] = {}
        if self.user:
            headers["X-ClickHouse-User"] = self.user
        if self.password:
            headers["X-ClickHouse-Key"] = self.password
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MigrationError(detail.strip() or f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise MigrationError(str(exc.reason)) from exc


def load_env_credentials(env_path: Path = Path(".env")) -> None:
    """Fill CLICKHOUSE_USER / CLICKHOUSE_PASSWORD from the repo .env when absent.

    QNT-381: both callers of this script run without the .env loaded — CD runs
    it with the VPS system python3, `make migrate` runs it via uv from dev.
    Only the two credential keys are read: CLICKHOUSE_HOST must stay
    env-or-default `localhost` (the VPS .env says `clickhouse`, which resolves
    only inside the compose network, not on the host where this script runs).
    Process env vars always win over the file.

    Comment handling: a bare `# ...` value and a trailing ` # ...` are treated
    as comments (matches the old .env.example password line). Consequence: a
    password containing ` #` would be truncated — keep passwords to symbols
    without `#`, per the "leave lines bare" rule in .env.example.

    This script deliberately does NOT import shared.Settings: CD runs it with
    the bare VPS python3 (no venv, no shared package on sys.path).
    """
    if not env_path.exists():
        return
    wanted = {"CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in wanted or key in os.environ:
            continue
        # Tolerate the inline-comment style of older .env files
        # (`CLICKHOUSE_PASSWORD=    # empty OK ...`).
        value = value.strip()
        if value.startswith("#"):
            value = ""
        else:
            value = value.partition(" #")[0].rstrip()
        os.environ[key] = value.strip("'\"")


def clickhouse_client() -> ClickHouseHTTP:
    load_env_credentials()
    host = os.environ.get("CLICKHOUSE_HOST", "localhost")
    port = os.environ.get("CLICKHOUSE_PORT", "8123")
    return ClickHouseHTTP(
        url=f"http://{host}:{port}/",
        user=os.environ.get("CLICKHOUSE_USER", ""),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


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
    client = clickhouse_client()
    try:
        applied_count, skipped_count = run_migrations(args.migrations_dir, client)
    except MigrationError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1

    print(f"Done: {applied_count} applied, {skipped_count} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
