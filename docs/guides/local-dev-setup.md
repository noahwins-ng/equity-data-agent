# Local Development Setup

## Prerequisites
- macOS (M4)
- [uv](https://docs.astral.sh/uv/) installed
- SSH access to Hetzner (`Host hetzner` configured in `~/.ssh/config`)
- GitHub CLI (`gh`) installed and authenticated
- VS Code with REST Client extension (for `.http` files)

## First-time setup

```bash
git clone https://github.com/noahwins-ng/equity-data-agent.git
cd equity-data-agent
make setup    # installs hooks, syncs deps, creates .env
```

Edit `.env` with your actual values (Qdrant, Langfuse, Anthropic keys).

## Daily workflow

### Terminal 1: SSH tunnel
```bash
make tunnel
```
Keep this running. ClickHouse is now accessible at `localhost:8123`.

### Terminal 2: Dagster
```bash
make dev-dagster
```
Dagster UI at http://localhost:3000

### Terminal 3: FastAPI
```bash
make dev-api
```
FastAPI at http://localhost:8000, docs at http://localhost:8000/docs

### Terminal 4: Frontend (Phase 6+)
```bash
make dev-frontend
```
Next.js at http://localhost:3001

### Explore data
Open http://localhost:8123/play in a browser for ClickHouse's built-in SQL editor.

## Working with Claude Code

```bash
/resume                   # Start of session: restore context
/cycle-start              # Start of week: see sprint issues
make issue QNT=34         # Checkout branch
# ... build ...
/ship QNT-34              # Sanity check → PR → merge → Done
```
