---
name: Source Verification and Web Collection
description: Verify academic papers, preprints, manufacturer datasheets, and web sources deterministically using okf verify-source, ensuring objective evidence and provenance grounding.
trigger_conditions:
  - verify-source
  - 出典検証
  - DOI
  - arXiv
  - データシート
  - Web収集
  - check-evidence
  - 証跡監査
---

# Source Verification and Web Collection

Use this skill when gathering or validating external sources (DOI, arXiv, datasheets, or web pages) before adding them to an OKF knowledge base.

## Core Principle: Separation of Mechanism and Policy

- **Tool's Responsibility (Mechanism)**: `okf verify-source` provides objective evidence only (ledger check, publisher, issue date, text matching, and SHA-256 snapshot). It never outputs trust scores or confidence numbers.
- **Agent's Responsibility (Policy)**: The agent decides whether a source is acceptable, primary vs. secondary, and how differences in values are resolved.

## Verification Hierarchy

1. **Crossref DOI**: Major international publishers. Checked against Crossref Works API.
2. **JaLC / DataCite DOI**: Japanese journals (J-STAGE) and technical repositories. Fallback from Crossref 404 via `doi.org/ra` to JaLC REST API or `doi.org` CSL-JSON content negotiation.
3. **arXiv ID**: Preprints checked against arXiv Atom API. Version of Record DOI is recorded if present.
4. **Identifier-less Web URLs (Tier 4 / Datasheets)**: Official manufacturer datasheets, product pages, or university bulletins without DOI. Captured with HTML/PDF snapshot, SHA-256 sidecar, and publisher extraction. Recorded as `kind=web` (or `datasheet`, `product-page`).

## Discovery & Pre-Filtering Discipline

Filter candidates before fetching to avoid wasting rate quotas and creating orphan snapshots:

1. **Pre-Fetch Material-Class Check**: Before running `okf verify-source <url>` or fetching a DOI, verify from the search title/snippet that the candidate matches the exact material or device class of the concept (e.g. Cu sintering vs. Ag sintering; bulk W-Cu alloy vs. W-coated diamond/Cu; electronics TIM vs. building/battery PCM). Do not fetch known mismatches.
2. **Crossref Discovery Pre-Filtering**: When querying Crossref, prefer `filter=type:journal-article` (and `type:proceedings-article` where apt). Up front, skip component DOIs with supplemental suffixes (e.g. `.s001`, `.s002`), authorless magazine/news items, and preprints/dissertations from SSRN or thesis repositories as non-adoptable by default.
3. **Web Discovery (PDF-First & Fallback)**:
   - Prefer direct manufacturer datasheet PDFs (often on a CDN, search via `filetype:pdf datasheet <material> "thermal conductivity"`) over JS-rendered product landing pages that frequently lack extractable data.
   - If an official product page fails fetch/extraction (HTTP 403, 404, or empty tables), search for the product's direct PDF datasheet before abandoning the source or concept.
4. **Query Expansion & Bilingual Fallback**:
   - Inspect the target concept note's YAML `tags:` and KB synonyms (or `synonyms.json`) to formulate query variants combining material aliases with application or process terms (e.g. `"tungsten copper" AND ("heat sink" OR "power electronics" OR "sintering")`).
   - For sparse materials where English Crossref results yield fewer than 3 hits, run bilingual queries against J-STAGE (`site:jstage.jst.go.jp <Japanese tag from YAML> 熱伝導率`) and verify the resulting DOIs via `okf verify-source` (resolved natively via JaLC).

## CLI Usage Examples

```bash
# Verify DOI (Crossref -> JaLC / DataCite fallback)
uv run okf verify-source 10.1111/jace.14748 \
  --expect-title "Aluminum Nitride" \
  --check-claim "180 W/m" \
  --json

# Verify arXiv preprint
uv run okf verify-source arXiv:2401.12345 --expect-author "Smith" --json

# Verify Web datasheet or Tier-4 bulletin and save snapshot
uv run okf verify-source "datasheet:https://example.com/aln-datasheet.pdf" \
  --issued 2024-05-01 \
  --json

# Record orphan / rejection reason for an unadopted snapshot
uv run okf verify-source "https://example.com/unadopted.html" \
  --orphan-reason "数値なし（定性的記述のみ）のため不採用"

# Audit evidence snapshots and provenance consistency during batch completion
uv run okf validate <wiki> --strict-provenance --check-evidence --json
```

### Exit Codes

- `0` (`verified`): Source confirmed and claims matched. Agent inspects `metadata.publisher` and records `okf_source` in the note.
- `1` (`not_found` / `mismatch` / `unverified`): Verification failed (missing ID, title/author/year mismatch, paywalled, or claim not in abstract/body). **Do not cite this source**.
- `2` (`error`): Rate limit, network error, or invalid arguments. Pause and wait before retrying.

## Agent Policy Guidelines

1. **Primary vs. Secondary**: Prefer manufacturer official datasheets, peer-reviewed journals, and institutional repositories. Reject blogs, summary sites, and secondary citations.
2. **Block-Local Grounding**: Ground claims in note sections. When multiple sources agree, cite them together. When values diverge, record both with measurement conditions in a note footnote.
3. **Tier-4 Attribution**: For papers without DOI/arXiv, record `web:<url> [issued=YYYY-MM-DD] retrieved=YYYY-MM-DD via=web` and explicitly name the publisher or bulletin in the note text so readers can assess authority.
4. **Recording Rejection Reasons (Orphan Triage)**: When a fetched snapshot is not adopted into any note's `sources:` (e.g. because of qualitative-only descriptions or lack of quantitative figures), record an objective `orphan_reason` in `.meta.json` using `okf verify-source <url> --orphan-reason "<reason>"`. This prevents unexplained orphan warnings (`[E2]`) during `okf validate --check-evidence`.
5. **Evidence Audit Gate**: At the end of any knowledge base update or ingestion batch, run `okf validate <wiki> --strict-provenance --check-evidence` as the completion gate to ensure all cited web sources have active snapshots (`[E1]`) and all unreferenced snapshots are documented (`[E2]`). For multi-concept batches, orchestrate staging, evidence gates, and staged delegation via `.agents/skills/kb-batch-discipline/SKILL.md`.
