# pat-okf-agent

Python 3.12+ toolkit combining cache-first **EPO OPS / EPS** and **USPTO Open Data Portal (ODP)** patent clients with an **OKF v0.2 Markdown knowledge base**, graph traversal, context compiler, and MCP server (`okf-mcp`).

## Features & Repository Layout

- `src/pat_okf_agent/epo.py` — EPO OPS v3.2 + EPS client and CLI (`epo`; stdlib-only)
- `src/pat_okf_agent/uspto.py` — USPTO Open Data Portal client and CLI (`uspto`; stdlib-only)
- `src/pat_okf_agent/uspto_bulk.py` — USPTO bulk product / grant XML downloader & parser
- `src/pat_okf_agent/wiki/` — OKF v0.2 Markdown knowledge base (Pattern A/B; optional Pattern C)
- `src/pat_okf_agent/wiki/tooling.py` — deterministic JSON tool contracts + path-bounded dispatcher
- `src/pat_okf_agent/wiki/mcp_server.py` — MCP server (`okf-mcp`; stdio and HTTP/SSE transports)
- `src/pat_okf_agent/wiki/mcp_http.py` — stdlib HTTP/SSE transport with loopback/Bearer auth guard
- `src/pat_okf_agent/wiki/embeddings.py` — HTTP / Gemma embedding adapters
- `src/pat_okf_agent/wiki/qdrant_backend.py` — Qdrant REST adapter
- `src/pat_okf_agent/wiki/synonyms.py` — bounded query expansion + synonym evaluation
- `src/pat_okf_agent/wiki/evolution.py` — deterministic `EvolutionLoop` + non-destructive proposals
- `.agents/skills/` — project skills (OKF operations, patent ingestion, source verification, batch discipline, skill authoring)
- `examples/wiki/` — synthetic demo wiki (patents + workflow/gotcha/synergy nodes)
- `scripts/release_smoke.py` — offline CLI/MCP release smoke test
- `scripts/check_dist.py` — wheel/sdist secret and CLI-content guard
- `scripts/check_public_repo.py` — tracked-tree secret/path guard
- `docs/epo_api.md` — EPO OPS/EPS usage notes (JP)
- `docs/uspto_api.md` — USPTO ODP usage notes (JP)
- `docs/okf_wiki.md` — OKF knowledge base design and CLI/MCP reference (JP)
- `docs/kb_building_guide.md` — 第三者向け OKF ナレッジベース構築実践ガイド (JP)
- `docs/design_rationales.md` — 設計判断の理由・古典的検索の完成・運用の落とし穴・開発メモ (JP)
- `docs/release_checklist.md` — release gates, smoke tests and known limitations
- `docs/public_release_audit.md` — public-push audit and owner checklist

## Fair Use & Security Notes

1. **Credentials stay out of Git.** Place EPO OPS credentials in `.secrets/ops_credentials.json` (or `EPO_OPS_CONSUMER_KEY` / `EPO_OPS_CONSUMER_SECRET`) and USPTO ODP keys in `.secrets/uspto_credentials.json` (or `USPTO_API_KEY`). Both `.secrets/` and `data/external/` are gitignored and blocked by CI guards.
2. **Cache-first & rate-limited by default.** Minimum 3 s spacing between EPO OPS requests is shared across processes (`data/external/ops_rate_state.json`, override with `EPO_OPS_RATE_STATE`), search windows are capped at 25 results (`MAX_SEARCH_RESULTS`), and responses are served from the local disk cache before any network call.
3. **Requirements.** Python **3.12 or newer** (tested on 3.12, 3.13, and 3.14). Optional features (e.g. PDF text extraction) are available via extras (`--extra pdf`).

## Quick Start with `uv` (Recommended)

```bash
uv sync                                          # creates .venv from pyproject.toml
uv run python -m unittest discover -s tests -v   # offline unit test suite
uv run python scripts/release_smoke.py           # offline CLI & MCP release smoke
uv run epo check                                 # OPS credential resolution (no network)
uv run uspto check                               # ODP API key resolution (no network)
uv run epo token                                 # acquire/refresh bearer token once
uv run epo search 'pa="EXAMPLE CORP"' --end 25
uv run epo biblio WO9999999A1
uv run epo eps-info EP9999999B1

# OKF knowledge base (Markdown + relative-link graph)
uv run okf init mywiki \
  --skills-dir .agents/skills \
  --skills-dir .opencode/skills
uv run okf skills mywiki --check --json
uv run okf schema --json
uv run okf ingest-epo EP9999999B1 --wiki mywiki
uv run okf search mywiki "amorphous alloy"
uv run okf traverse mywiki EP9999999B1 --depth 2
uv run okf context mywiki "アモルファス クレーム"
uv run okf sqlite mywiki                         # Pattern B (SQLite-FTS5)
uv run okf validate mywiki
uv run okf links mywiki --json                   # diagnose zero-inbound / isolated concepts & components
uv run okf verify-source --file ./local.pdf --cache-dir mywiki/evidence --json
uv run okf search mywiki "metallic glass" --expand --json
uv run okf eval-synonyms mywiki --check
uv run okf evolve mywiki --history .agents/flow_history.jsonl
uv run okf evolve mywiki --history .agents/flow_history.jsonl --propose
uv run okf-mcp --root .                          # stdio MCP server for LLM clients

# Optional Pattern C (requires a Qdrant server + embedding endpoint)
# export QDRANT_URL=http://localhost:6333
# export OKF_EMBEDDING_URL=http://localhost:8080/v1/embeddings
# export OKF_EMBEDDING_MODEL=gemma-embedding-2
uv run okf qdrant mywiki
uv run okf search mywiki "アモルファス合金" --backend qdrant

# HTTP MCP transport (loopback default; token required off-loopback)
OKF_MCP_TOKEN=change-me uv run okf-mcp --root . --transport http --port 8765
```

Pattern C is opt-in: `auto` uses Pattern B above 500 nodes. Qdrant, ONNX Runtime and remote embedding servers are optional infrastructure; if they are absent the CLI/library raises `BackendUnavailable` and Markdown stays authoritative.

## USPTO (US Patents)

USPTO uses the Open Data Portal API and requires a free API key (<https://data.uspto.gov/apikey>). Set `USPTO_API_KEY` or `.secrets/uspto_credentials.json`, then:

```bash
uv run uspto check
uv run uspto search 'graphene' --limit 10
uv run uspto metadata 18999999

# download + parse (ODP files need the key; public URLs do not)
uv run uspto download "<ODP file URL>" -o data/external/uspto_raw/
uv run uspto parse data/external/uspto_raw/<file>.xml --claims
uv run uspto parse <weekly>.xml --stream   # iterate multi-doc files
uv run uspto parse <weekly>.zip            # parse a downloaded zip (no key)
uv run uspto extract <weekly>.zip -o <dir> # safe unzip (traversal rejected)

# year-level bulk (weekly products => all files whose data range is that year)
uv run uspto product PTGRXML --year 2024
uv run uspto year PTGRXML 2024 -o data/external/uspto/2024 --dry-run
uv run uspto year PTGRXML 2024 -o data/external/uspto/2024
```

See `docs/uspto_api.md` for details.

## Quick Start without `uv` (Standard Library `python`)

```bash
export PYTHONPATH=src
python3 -m pat_okf_agent.epo check
python3 -m pat_okf_agent.epo token

python3 - <<'PY'
from pat_okf_agent.epo import OpsClient, EpsClient
ops = OpsClient()
print(ops.search('pa="EXAMPLE CORP" and ti="atomiz"').total_count)
eps = EpsClient()
print(eps.info("EP9999999B1")["date_publ"])
PY
```
