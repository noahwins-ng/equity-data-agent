# Security scanner waivers

This file tracks every active waiver across the QNT-160 scanner suite. Add an
entry **before** suppressing a finding in scanner config; reviewers check this
log when a previously-failing finding suddenly disappears.

A waiver is appropriate when:

1. The finding is a documented false positive (placeholder credential in a
   doc, internal table-name interpolation that can't take user input).
2. The fix requires a breaking change we've decided not to take yet (with a
   tracked Linear ticket for the eventual fix).

A waiver is **not** appropriate as a way to ship a real vulnerability. If the
fix is non-trivial but the finding is real, file a Linear ticket and waive
with the ticket number in the rationale.

---

## Active waivers

### bandit

**Severity gate set to HIGH.** Bandit reports 24 medium findings on the current
codebase, all of rule `B608: hardcoded_sql_expressions`. Bandit's
medium-confidence detector can't distinguish "f-string with a literal" from
"f-string with attacker-controlled input"; it flags every f-string-built
SQL query. The 24 hits fall into three patterns, all reviewed and confirmed
not user-reachable as of 2026-05-04:

1. **Constant table-name interpolation** — `{table}` or `{_TABLE_NAME}`
   resolves to a module-level string constant. The `ClickHouseResource`
   driver doesn't support identifier substitution, so identifier
   interpolation is unavoidable.
   - `packages/dagster-pipelines/src/dagster_pipelines/asset_checks/technical_indicators_checks.py:49,74,108,206,214,222`
   - `packages/dagster-pipelines/src/dagster_pipelines/asset_checks/news_raw_checks.py:57,84,104,125,157`
   - `packages/dagster-pipelines/src/dagster_pipelines/asset_checks/news_embeddings_checks.py:112,199`
   - `packages/dagster-pipelines/src/dagster_pipelines/asset_checks/fundamental_summary_checks.py:40,66,98,145`

2. **Constant-list interpolation** — values come from a tuple/list of
   string constants joined into an `IN (...)` clause; not user input.
   - `packages/dagster-pipelines/src/dagster_pipelines/asset_checks/fundamentals_checks.py:45` —
     `valid_list = ",".join(f"'{pt}'" for pt in _VALID_PERIOD_TYPES)`.

3. **Mixed: ticker-validated + constant interpolation** — query uses
   `%(ticker)s` parameterisation for the user-supplied ticker (validated
   against `ALL_OHLCV_TICKERS`/`TICKERS` allowlist before reaching SQL);
   the f-string only injects constants like timeframe-keyed table names
   from a hardcoded enum lookup or numeric `WINDOW_DAYS`.
   - `packages/api/src/api/routers/data.py:238,301,412` — ticker
     validated at line 234 (`if ticker not in ALL_OHLCV_TICKERS: 404`),
     `{table}`/`{date_col}` from `_TIMEFRAME_QUERY` enum lookup.
   - `packages/api/src/api/templates/fundamental.py:87` and
     `packages/api/src/api/templates/technical.py:181` — same pattern.
   - `packages/dagster-pipelines/src/dagster_pipelines/assets/news_embeddings.py:60` —
     `{WINDOW_DAYS}` is an `int` constant; ticker via `%(ticker)s`.

The right fix would be either (a) parameterise table names via the ClickHouse
client (the driver doesn't support identifier substitution), or (b) silence
each via `# nosec B608` with a per-line rationale. Sprinkling 24 nosec
comments adds noise without adding safety; gating at HIGH instead means we
catch the real categories (eval/exec, shell injection, weak crypto, hardcoded
passwords, jinja2 autoescape off) without flap on the false positives.

**Decision**: gate at HIGH (`bandit -lll`), document mediums here. **Before
adding a new B608 finding to the waivable set, audit it against the three
patterns above** — if a new f-string interpolates anything that could come
from user input without prior allowlist validation, fix the code; do not
extend this waiver. Re-audit on schema change or when public chat (QNT-75)
adds new query paths.

**Skipped rules** (see `pyproject.toml` `[tool.bandit] skips`):

- `B101` — `assert_used`. We exclude `tests/` already; the remaining hits
  are module-level invariant assertions in agent/api code that we want to
  keep.

### gitleaks

**Path allowlist** (see `.gitleaks.toml`):

- `\.claude/skills/.*\.md` — Claude Code agent skill markdown. Contains
  example curl commands with placeholder credentials (`"your-password"`,
  `"X-ClickHouse-Key: your-token"`) that match the `curl-auth-header` rule.
  These are documentation, not real secrets.
- `\.env\.sops` — SOPS-encrypted dotenv ciphertext. The plaintext `.env`
  is gitignored; the encrypted file is the deploy-time secret store and
  is safe to commit.
- `frontend/\.next/.*` — Next.js build output. Generates per-build preview
  encryption keys that match `generic-api-key`. Path is gitignored, so
  this only matters for `--no-git` filesystem scans.
- `.*node_modules/.*` — vendor code; not our concern, often contains test
  fixtures with synthetic secrets.

### npm audit

**Severity gate set to HIGH.** `npm audit` currently reports 2 moderate
findings: `postcss <8.5.10` (XSS via unescaped `</style>`), reachable only
via Next.js's bundled copy. `npm audit fix --force` mechanically suggests
`next@9.3.3` — that's npm's resolver picking the oldest version in the
range without the vulnerable postcss, not a serious upgrade path; the
suggestion would regress us seven major versions of Next. We're not taking
it. `postcss` is build-time only (compile-time CSS transformation; not
shipped to the browser), so the XSS gadget isn't reachable from the public
chat surface (QNT-75). Gating at HIGH while tracking the moderate is the
right posture.

**Decision**: gate at HIGH (`--audit-level=high`), revisit when Next.js
ships a release whose bundled postcss is ≥8.5.10. Re-audit when public chat
(QNT-75) adds any user-supplied content rendered through a CSS pipeline.

### pip-audit

No active waivers. `langsmith==0.7.30` (GHSA-rr7j-v2q5-chgv) was bumped to
0.8.0 as part of QNT-160 to clear the CVE before the gate landed.

---

## How to add a waiver

1. Run the relevant scanner locally and confirm the finding is genuinely a
   false positive or has a documented mitigation reason.
2. Add the waiver to the scanner's config:
   - **bandit**: `pyproject.toml` `[tool.bandit] skips` (rule-level) or
     `# nosec BXXX` comment (line-level, with rationale).
   - **gitleaks**: `.gitleaks.toml` `[allowlist] paths` or `regexes` or
     fingerprints.
   - **pip-audit**: `--ignore-vuln <GHSA-id>` in `.github/workflows/ci.yml`.
   - **npm audit**: `package.json` `overrides` or, for moderate-only
     findings, raise the `--audit-level` gate.
3. Append an entry to this file with the rule/CVE id, the file/path, the
   reason, and (if the fix is real but deferred) the tracking Linear
   ticket.
4. Commit waiver config + this file in the same PR. Reviewer should
   confirm the waiver is justified.
