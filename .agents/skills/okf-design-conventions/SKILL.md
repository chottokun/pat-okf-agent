---
name: OKF Design Conventions
description: Apply the settled OKF design conventions — controlled value domains, freshness by verified_at and deprecation, KB-internal evidence, two-tier tags, version grounding, cross-KB references, and correct gate invocations — when designing a KB, adding controlled values, or running validation gates.
trigger_conditions:
  - design conventions
  - 設計規約
  - new KB design
  - 新規KB設計
  - basis value
  - 値域
  - evidence layout
  - freshness rule
  - tag tiers
---

# OKF Design Conventions

## Purpose & Scope

These conventions were decided and measured on 2026-11-12–13 across the
semiconductor KB, kb-shana-ops, PR #19 and PR #20. Read this skill before
designing a new KB, adding a controlled value (basis, source kind, Tier A tag),
placing evidence, or running closing gates. The conventions state where
judgement lives; they do not replace the workflow skills (okf-wiki for note
mechanics, kb-batch-discipline for batch execution, source-verify for web
snapshots).

## 1. Controlled Value Domains (basis, source kinds)

- `basis` and source `kind` are contract fields. ADDING a value deliberately is
  legitimate extension (basis `ops-record`, PR #19; source kind `ops-record`,
  PR #20). Spelling drift across notes is never tolerated.
- Tools report unknown values as facts: unknown basis → `[P9]` warning,
  unknown source kind → `[P7]` warning. Whether an unknown value fails a run is
  the CALLER's decision (`--strict-provenance` escalates P9; candidate-check
  uses `--fail-on <kind>`). Severity is never baked into the vocabulary or the
  tool.
- Standing principle: a value carries no intrinsic weight; weight belongs to
  the decision layer (skill / owner). See the AGENTS.md convention
  「事実に重みを焼き込まない」.

## 2. Freshness = verified_at + deprecated

- Freshness is two facts: `verified_at` (the date the note was last confirmed
  against its source) and a `deprecated` judgement when an item is superseded.
- No age threshold ("stale after N days") is ever baked into a tool or a gate.
  Readers judge staleness from the date plus the deprecation mark.
- A superseded item is marked deprecated and kept visible; it is never silently
  rewritten as if it had always been current.
- When a note's `verified_at` is refreshed, refresh its evidence snapshot in
  the same pass (snapshot drift is recorded, in the note's history memo).

## 3. Evidence Layout (KB-internal, self-contained)

- Snapshots live inside the KB: `<wiki>/evidence/snapshots/<key>.txt` with a
  `<key>.meta.json` sidecar (sha256; for record snapshots also source_path,
  section, copied_at; orphan_reason whenever unreferenced).
- Snapshot bodies are NEVER `.md`: `okf validate` recursively reads every
  `.md` under a KB root as a note, so a `.md` snapshot becomes a structural
  error. Use `.txt`.
- `check-evidence` must ALWAYS receive the target KB's `--evidence-dir`
  explicitly. Without it, the tool defaults to `data/external/source_verify`
  relative to the CWD and silently audits the wrong cache — measured failure in
  the kb-shana-ops main-batch audit (2026-11-13): all ops-record sources
  reported `[E1]` missing against the semiconductor cache.
- Exception by design: the semiconductor KB's web snapshots live in the shared
  repo cache `data/external/source_verify`; run its gates from the repository
  root (or pass that path explicitly). Ops-record and paper-centric KBs use
  their KB-internal `evidence/`.

## 4. Two-Tier Tags

- Tier A (canonical; no drift; additions are deliberate extensions only): tags
  carrying synonym / ownership / aggregation meaning — manual synonym-group
  words, title-derived abbreviations and formal variants, the concept slug
  itself, explicit `tier_a_tags`, and namespaced classification tags such as
  `strand:<name>` in kb-ai-research.
- Tier B: free descriptive tags; variation is acceptable.
- Tag collisions are facts reported for post-batch cleanup (see
  kb-batch-discipline); they are never selection-time failures and are never
  silenced at write time.

## 5. Grounding to the Version Read

- Claims ground to the version actually read: arXiv → ID + version number;
  when a conference version exists it is canonical and the difference from the
  arXiv version is noted in the history memo; repositories → name the
  release/commit whenever a claim depends on it; web sources → `retrieved=`
  date is mandatory (P6).
- Living canonical documents (e.g. operational records such as TOOLS.md) are
  the current procedure; the snapshot in `evidence/` is the record of what was
  grounded at copy time. Drift between them is recorded, not hidden.

## 6. Cross-KB References

- Reference another KB's concept by explicit slug mention (plus a `sources`
  entry where grounding requires it), never by relative Markdown link:
  cross-KB links sit outside index / link checking and break silently.

## 7. Gate Invocation Quick Reference

| KB type | validate with evidence | index |
| :--- | :--- | :--- |
| Patent-type, shared web cache (semiconductor) | `uv run okf validate <wiki> --strict-provenance --check-evidence` from repo root (default evidence dir `data/external/source_verify`); synonyms eval: `uv run okf eval-synonyms <wiki> --json` | `uv run okf index <wiki>` then `uv run okf index <wiki> --check` |
| Ops-record type (kb-shana-ops) | `uv run okf validate <wiki> --strict-provenance --check-evidence --evidence-dir <wiki>/evidence` (ops-record sources resolve into `<wiki>/evidence/snapshots/` and are sha256-verified as `[E1]`) | same |
| Paper-centric type (kb-ai-research) | Same explicit `--evidence-dir <wiki>/evidence` form; `verify-source` outputs are directed into the KB's own evidence dir | same |

- Pass criteria (all types): validate exits 0 with 0 errors and 0 unexplained
  orphans; `index --check` exits 0.
- Over the Drive mount, run gates detached and poll the log (cold validate
  takes ~100–350 s over the mount vs <1 s local).

## Verification

```bash
uv run okf skills . --check --json
```

(expect `"ok": true`; this skill raises the count to 7.)
