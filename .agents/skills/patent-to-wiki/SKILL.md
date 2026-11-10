---
name: Patent to Wiki Ingestion
description: Orchestrate cache-first EPO patent retrieval into an OKF wiki, then validate, index and smoke-test the resulting Markdown graph.
trigger_conditions:
  - ingest patent
  - patent to wiki
  - 特許取り込み
  - 特許Wiki
  - EPO取り込み
  - family stubs
  - ingest-epo
---

# Patent to Wiki Ingestion

Use this skill when patent data must become durable OKF knowledge. It composes
the **EPO Patent Fetch** skill with **OKF Wiki Operations**; never duplicate
their low-level logic.

## Preconditions

- Run commands from the repository root.
- Use the bundled `epo`/`okf` CLI; an external EPO skill is optional.
- The target wiki already has `index.md`, or run:

```bash
uv run okf init <wiki>
```

## Pipeline

### 1. Preflight

```bash
uv run okf stats <wiki> --json
uv run okf validate <wiki> --json
```

Record the concept count and current warnings before changing anything.

### 2. Cache-first retrieval

Start offline. The bundled client uses its disk cache before any network call.

```bash
# Cache-first; no network when the publication is already cached.
uv run okf ingest-epo <PUBLICATION> --wiki <wiki> --offline --json
```

If the result is `uncached`, rerun without `--offline` for that publication.
Respect the OPS free plan: one request at a time, at least 3 seconds apart, and
never more than 25 search results.

### 3. Ingest into Markdown

```bash
uv run okf ingest-epo <PUBLICATION> \
  --wiki <wiki> --offline --json
```

For a search-based batch:

```bash
uv run okf ingest-epo \
  --query '<CQL>' --max-results 10 --wiki <wiki> --offline --json
```

Query tip: Expand CQL queries using the target concept's YAML `tags:` and synonyms. Combine material aliases with application terms across title and abstract (e.g. `(ti="<term>" or ti="<alias>" or ab="<term>") and (ti="<app>" or ab="<app>")`) instead of unconstrained single `ti=` queries.

The ingester:

- writes one `patents/<PUBLICATION>.md` node with `basis: external`, `sources` (`patent-biblio:<PUB> via=epo-ops` and `patent-claims:<PUB> via=epo-ops|epo-eps`), and `verified_at: YYYY-MM-DD`
- creates family stubs only when no full node exists, with `basis: external` and `sources: ["patent-family:<PARENT>"]`
- writes relative links for family members
- appends `log.md`
- regenerates progressive indexes

### 4. Verify the result

Read the JSON summary:

- `claims` and `claims_source`: records whether OPS or EPS supplied claims
- `family` and `stubs`: expected family linkage
- `warnings`: missing claims/family are explicit, never inferred
- `index_files`: regenerated indexes

Then run:

```bash
uv run okf validate <wiki>
uv run okf index <wiki>
uv run okf context <wiki> "<title or alloy>" \
  --max-nodes 5 --json
```

Check that:

- `broken_links` is zero and no provenance warnings (`[P1]`–`[P8]`) are reported
- the new patent is reachable from the expected workflow or family
- incompatible Gotchas nodes are pruned rather than used
- the context token count stays within the requested budget

### 5. Scale check

```bash
uv run okf stats <wiki> --json
```

- `pattern: A`: no action
- `pattern: B`: build `okf sqlite <wiki>`
- `pattern: C`: consider the optional embedding adapter; do not enable it
  unless an embedder is configured and evaluated

## Non-negotiables

- Do not fabricate claims. If OPS/EPS both fail, write “claims unavailable” and
  report the warning.
- Do not overwrite a non-stub concept blindly; inspect it first.
- Do not put credentials in Markdown, logs, skill files, or tool arguments.
- Do not bypass the EPO fair-use gate with ad-hoc HTTP calls.
- Do not hand-edit generated `index.md` files.

## Failure handling

| Symptom | Action |
| --- | --- |
| `uncached` offline | fetch only that publication with the EPO skill |
| `claims unavailable` | try EPS for EP publications; otherwise record warning |
| `family unavailable` | keep the patent node; do not invent members |
| broken family link | create the stub or remove the link; validate again |
| `BackendUnavailable` | fall back to Pattern A local search |
| validation error | stop and fix frontmatter/links before indexing |
