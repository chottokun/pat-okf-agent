---
name: OKF Wiki Operations
description: Operate an OKF v0.2 Markdown knowledge base safely with progressive disclosure, bounded context, weighted traversal, validation and relative links.
trigger_conditions:
  - okf
  - OKF
  - LLM-Wiki
  - 知識ベース
  - wiki検索
  - 文脈解決
  - リンク切れ
  - 索引更新
---

# OKF Wiki Operations

Use this skill for reading, searching, validating or updating an OKF v0.2
Markdown wiki. It is a **tool-first** skill: deterministic work belongs to the
`okf` CLI, while this document covers judgement, order and safety.

## Non-negotiables

- Markdown is the source of truth. Never make a vector store or SQLite index
  authoritative.
- One topic = one Markdown file.
- Every concept has YAML frontmatter with a mandatory `type`.
- Internal links are GitHub-style relative Markdown links:
  `[label](../path/to/note.md)`. Never use `[[WikiLink]]`; validation warns.
- Never hand-edit generated `index.md`, `index-2.md`, or `.okf/` files.
- Preserve existing frontmatter and user-authored prose unless the task
  explicitly asks for a rewrite.
- Run validation after mutations and regenerate indexes with `okf index`.

## Phase 1: orientation only

Run commands from the repository root. Read the nearest `index.md` first. Do
not load concept bodies yet.

```bash
uv run okf stats <wiki> --json
uv run okf context <wiki> "<query>" --json
```

`okf context` is the preferred entry point. It performs seed search, weighted
graph expansion, Gotchas pruning and token budgeting in one deterministic step.

## Phase 2: targeted discovery

Use `search` and `traverse` only when context is insufficient.

```bash
# Full-text seeds (Pattern A/B; auto selects B above 500 nodes)
uv run okf search <wiki> "<query>" --limit 10 --json

# Explicit graph expansion around a known node
uv run okf traverse <wiki> <seed> \
  --depth 2 --query "<query>" --direction out --json

# Inspect all incoming links
uv run okf traverse <wiki> <seed> \
  --depth 1 --direction in --json
```

Interpret results:

- `node`: repository-relative Markdown path
- `depth`: hop distance from a seed
- `score`: path-product score with heading-scope weight and damping
- `pruned`: nodes removed by an incompatible Gotchas constraint

## Phase 3: load only selected bodies

Open the top-ranked notes and their direct neighbours. Stay within the task
budget:

| Phase | Budget | Content |
| --- | --- | --- |
| 1 | < 1,000 tokens | root/category indexes only |
| 2 | < 2,500 tokens | frontmatter and headings |
| 3 | < 4,000 tokens | selected bodies and direct neighbours |

Use `okf context <wiki> "<query>" --phases --json` to inspect all three phases.
Discard bodies when the task is complete.

## Search backends

```bash
# Pattern A: Markdown scan + optional ripgrep (default under 501 nodes)
uv run okf search <wiki> "<query>" --backend local

# Pattern B: SQLite FTS5 + edge table
uv run okf sqlite <wiki>
uv run okf search <wiki> "<query>" --backend sqlite

# Auto: Pattern B above 500 concepts
uv run okf search <wiki> "<query>" --backend auto

# Synonym expansion (bounded; original query remains first)
uv run okf search <wiki> "<query>" --expand --json

# Optional Pattern C: Qdrant + HTTP/Gemma embeddings
uv run okf qdrant <wiki>
uv run okf search <wiki> "<query>" --backend qdrant
```

Pattern B's database defaults to `<wiki>/.okf/index.sqlite3`. It is derived and
must be gitignored. If the tokenizer changes, delete `.okf/` and rebuild.

Pattern C is opt-in (`--backend qdrant`); `auto` stays on B above 500 nodes.
It needs `QDRANT_URL` and an `OKF_EMBEDDING_*` endpoint, or explicit CLI flags.
The Qdrant collection is derived data: rebuild it with `okf qdrant <wiki>` or
`--force`; never edit it by hand.  If infrastructure is unavailable, fall back
to `--backend local`.

### Synonym quality gate

`<wiki>/synonyms.json` contains alias groups.  `<wiki>/eval_queries.json`
contains labelled expectations.  Use the evaluation command before changing
the search pattern:

```bash
uv run okf eval-synonyms <wiki> --json
uv run okf eval-synonyms <wiki> --check
```

A failure rate above 15% is a Pattern C signal, not an automatic migration.
Do not add speculative aliases just to improve one query; keep groups small and
review the per-query baseline/expanded results.

## Update workflow

1. Read the target note and one or two neighbours.
2. Check whether a dedicated note already exists. Prefer updating over creating
   a duplicate.
3. If creating a note, write one topic, valid frontmatter and relative links.
4. Put links under standard headings so edge weights are meaningful:
   - `## Taxonomy` (1.0)
   - `## Workflow / Implementation` (1.2)
   - `## Synergistic Combinations` (1.5)
   - `## Gotchas / Anti-Pitfalls` (2.0)
5. Run:

```bash
uv run okf validate <wiki>
uv run okf index <wiki>
uv run okf context <wiki> "<smoke query>" --json
```

`okf validate` returns exit code 1 on structural errors. Warnings (including
orphans and unsupported WikiLink syntax) do not fail the command.

## Gotchas and constraints

`okf context` prunes a candidate when its `## Gotchas / Anti-Pitfalls` section
contains:

- `INCOMPATIBLE_EXPLICIT` — unconditional stop
- `INCOMPATIBLE_WITH: windows, gpu` — prune when the query matches a term
- `NOT_FOR: production` — prune when the query matches a term

Do not override an `INCOMPATIBLE_EXPLICIT` constraint merely because a node is
highly ranked. Report the pruned node and choose another path.

## EvolutionLoop

`okf evolve` reads append-only `flow_history.jsonl` observations, clusters
repeated step n-grams, and renders reviewable skill drafts.  It is a
deterministic baseline; no LLM mutation is implied.

```bash
# Default is a dry run: inspect the draft and its source tasks first.
uv run okf evolve <wiki> \
  --history .agents/flow_history.jsonl --dry-run --json
uv run okf evolve <wiki> \
  --history .agents/flow_history.jsonl --apply
uv run okf evolve <wiki> \
  --history .agents/flow_history.jsonl --propose --json
uv run okf evolve <wiki> \
  --history .agents/flow_history.jsonl --propose --force
```

- `--dry-run` and `--apply` are mutually exclusive.
- `--apply` writes only `.agents/skills/<slug>/SKILL.md`.
- `--propose` writes `.agents/skills/.proposals/<slug>.md` and never
  modifies the live skill.  The proposal directory is ignored by skill
  discovery because it has no `SKILL.md`.
- Proposals include the observed steps, existing steps, a step diff, a review
  checklist and a candidate draft.
- Existing proposals are preserved unless `--force` is explicit.  `--force`
  requires `--apply` or `--propose`.
- `--propose` and `--dry-run` are mutually exclusive.  Combine `--apply`
  and `--propose` to create new skills and propose updates in one pass.
- Existing skills are never replaced without `--force`.
- Clusters with the same task slug receive deterministic `-2`, `-3` suffixes
  instead of overwriting one another.
- `--learn` (requires `--apply`) appends a short summary below
  `<!-- LEARNED -->` in `AGENTS.md`.
- Review generated steps against current tools; delete stale drafts.
- Never put secrets or large tool outputs into the flow history.

## Failure handling

- `backend unavailable`: fall back to `--backend local`; Pattern A has no
  optional dependencies.  A missing Qdrant/ONNX/Gemma endpoint is expected to
  fail closed rather than silently changing semantics.
- `broken link`: fix the target path or add the missing note; do not hide it.
- `stale index`: run `okf index`; never patch index files manually.
- `seed not found`: use `okf search` to resolve a path, then retry.
- `ambiguous or unsupported syntax`: convert `[[...]]` to a relative link.
