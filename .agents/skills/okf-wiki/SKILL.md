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

# Synonym expansion: inline 0.5x auxiliary terms (no variant merge; original query dominates)
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

### Search scoring, filters and query craft

- Scores weight where a term matches, never what a note is: per term title 20 / tags 15 / description 10 / headings+lead 5 / body 1 per occurrence; whole-query phrase bonus title +50 / description +20 / headings+lead +15; headings+lead and body totals are capped at 10.
- Match factors: exact x1.0, stem x0.8, wildcard prefix x0.7, synonym/correction auxiliary terms x0.5; deprecated notes x0.3.
- Filters are facts, not weights: `--type`, `--scope`, `--no-admin` (admin notes are excluded by default). If patent notes bury the concept note, re-query with `--type concept` instead of fighting the ranking.
- Operators: uppercase `AND` / `OR` (`AND` is the default gate for ASCII queries: every term must match somewhere), prefix wildcard `term*`.
- Zero hits on an ASCII query trigger a vocabulary typo fallback: `--json` hits then carry `is_fallback` and `query_corrections`; prefer re-querying with the corrected term for a clean ranking.
- Read the breakdown before re-querying: `--json` hits carry `breakdown` (per-term kind/field/points, phrase, field subtotals, adjustments, gate matched/missed, total == score). The missed list and the field points show why a note ranked where it did; change the query or the filter, not your trust in the corpus. `eval-synonyms` failure rows carry `failure_detail` with the same breakdowns.
- Informational link counts: `--json` hits carry `outgoing_links` and `backlinks` (distinct non-special internal notes in the same KB linked to and from, attached purely for the reader; never enter scoring, ranking, or breakdown totals).

### Synonym sync and quality gate

`<wiki>/synonyms.json` contains alias groups synchronized from `concepts/*.md`
`tags:` (plus optional `manual_groups`).  `<wiki>/eval_queries.json` contains
labelled expectations.  Sync and evaluate before changing the search pattern:

```bash
uv run okf sync-synonyms <wiki>
uv run okf sync-synonyms <wiki> --check
uv run okf eval-synonyms <wiki> --json
uv run okf eval-synonyms <wiki> --check
```

A failure rate above 15% is a Pattern C signal, not an automatic migration.
Do not add speculative aliases just to improve one query; keep groups small and
review the per-query baseline/expanded results.

## Create and update workflow

Follow these 5 steps when creating or updating notes:

1. **Avoid duplicates**: Search (`okf search`) before creating; prefer updating over creating a duplicate.
2. **Choose type and basis**: Determine `type` (e.g. `Concept`) and epistemic `basis` (`external`, `mixed`, `model`, or `ops-record`). Notes with `basis: ops-record` cite internal record snapshots using `sources: ['ops-record:<path>[#<section>] snapshot=<key>']`.
3. **Select skeleton**: Include 4 major H2 scopes (`Taxonomy`, `Workflow / Implementation`, `Synergistic Combinations`, `Gotchas / Anti-Pitfalls`).
4. **Isolate unverified metrics and back claims**: Place unverified unit metrics inside `> [!NOTE] 一般知識（未検証）` (or `General Knowledge (Unverified)`). For mixed tables, mark column headers with `（一般知識・未検証）` or `(Unverified)`. Back claims with links under `**代表公報（実データ）:**` (or `Representative Patents (Verified):`) and declare `sources`. Controlled H3 `### 出典に基づく整理` (`Based on Sources`) is transparent. Verify external papers/web sources with `okf verify-source` (see `.agents/skills/source-verify/SKILL.md`).
5. **Verify immediately**: Run `okf validate <wiki>` and `okf index <wiki>` to confirm zero `[P1]`–`[P9]` warnings. For multi-concept batches, orchestrate staging, closing gates, and delegation via `.agents/skills/kb-batch-discipline/SKILL.md`.

<!-- okf-concept-skeleton:start -->
```markdown
---
type: Concept
title: "窒化アルミニウム（AlN）フィラー"
description: "高熱伝導・電気絶縁性を兼ね備える窒化物セラミックスフィラー"
tags: [material, filler, nitride]
scope: taxonomy
basis: mixed
sources:
  - "patent-biblio:EP9999999B1 via=epo-ops"
---
# 窒化アルミニウム（AlN）フィラー

窒化アルミニウム（AlN）は、半導体封止材や放熱基板に用いられる代表的な高熱伝導・電気絶縁フィラーである。マトリックス樹脂との複合化設計については [EP9999999B1](../patents/EP9999999B1.md) を参照。

## Taxonomy

ウルツ鉱型結晶構造を持つ共有結合性セラミックスであり、フォノン伝導によって高い熱伝導性を示す。

> [!NOTE] 一般知識（未検証）
> 焼結体・単結晶の典型的な熱伝導率は約 150–230 W/m·K、線膨張係数（CTE）は約 4.5 ppm/K とされる（目安値、出典未照合）。

## Workflow / Implementation

高充填化では粒径分布の制御と表面処理（シランカップリング剤等）を組み合わせる。

**代表公報（実データ）:** [EP9999999B1](../patents/EP9999999B1.md)

## Synergistic Combinations

球状アルミナや窒化ホウ素（BN）との異種フィラー併用により、充填率と粘度特性のバランスを改善できる。関連手法は [EP9999999B1](../patents/EP9999999B1.md) を参照。

## Gotchas / Anti-Pitfalls

水分との反応（加水分解）によりアンモニアを生じ、界面熱抵抗の上昇や絶縁低下を招くため、耐湿処理の有無を確認する。
```
<!-- okf-concept-skeleton:end -->

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
