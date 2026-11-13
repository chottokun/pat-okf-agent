# Paper-Centric Batch Profile

Companion reference of the **KB Batch Collection Discipline** skill (`.agents/skills/kb-batch-discipline/SKILL.md`, which carries the operative summary of this profile). Read this full text before running a paper-centric batch.

Applies to batches whose primary sources are papers (peer-reviewed or preprint) for a paper-centric KB (e.g. `kb-ai-research`). This profile operationalizes the generic Phase-2 paper target for such KBs; candidate selection still follows `## Candidate Selection & Proposal` above, and settled conventions (evidence layout, tag tiers, version grounding, cross-KB references, gate forms) live in `.agents/skills/okf-design-conventions/SKILL.md` and are referenced here, not restated.

### Source Composition per Concept

- **3 papers is the baseline**: 1 defining original (the paper that defines the concept) + 2 follow-up / rival / application papers. Peer-reviewed and preprint papers count equally.
- Fewer is acceptable only when the literature is genuinely sparse; record the reason in the batch audit memo. Never pad a concept with unrelated papers to reach the count.
- **Web sources are limited to official repositories and official technical blogs** (guideline 0-2 per concept). Personal blogs, aggregator/summary articles, and news pieces never count as primary sources and never enter `sources:`; they may be used as discovery leads only.
- Candidate pool sources: (1) citation chains of surveys/reviews across the KB's strands, (2) the core paper's citation/reference graph, (3) official repository documentation and related-paper lists. Feed readers (e.g. the RSS-Bridge arXiv feeds) are entry points, not evidence.
- Confirm each paper's existence and version with `okf verify-source` before adoption (mechanics: `.agents/skills/source-verify/SKILL.md`).

### Two-Stage Grounding

Grounding is split in two stages. **The abstract confirms identity; the full text grounds substance.**

- **Stage 1 - Identity (abstract)**: Verify each seed paper with `okf verify-source` (arXiv ID = tier 3, DOI = tier 1, identifier-less = tier 4 web). The verified ID + version, title, and abstract confirm the paper is the concept's seed. Use the abstract to decide adoption and to scope the note's 問い section. Only claims stated explicitly in the abstract may be written from the abstract alone (role table below).
- **Stage 2 - Substance (full text)**: The 手法 and 結果 sections are written from the full text. 主張 claims that do not appear in the abstract must be confirmed in the full text before writing. 限界と前提 comes from the paper itself (the authors' own limitation statements, experimental setup assumptions) - never inferred from the abstract.
- **Acquisition routes** (record the route used for each note in its 履歴メモ):
  - **Route A (arXiv, default)**: The arXiv HTML full text (`arxiv.org/html/<id>v<n>`) first; the PDF when no HTML exists. The grounding anchor is the arXiv ID + version (version grounding: okf-design-conventions section 5).
  - **Route B (no arXiv version)**: The proceedings / publisher page, taken as a web-tier source (fetch + content match + snapshot) with `retrieved=` in `sources:` (precedent: Orca for continuous-batching).
  - **Route C (official repositories / documentation)**: Web sources with `retrieved=`; when a claim depends on a specific release or commit, name that version in the note body and in `sources:` (okf-design-conventions section 5).
- If the version read differs from the version verified (e.g. a new arXiv version appeared after verification), re-ground to the version actually read and align `verified_at` and the 履歴メモ with it (okf-design-conventions section 5).

| Note section | Abstract suffices? | Full text required? |
| :--- | :--- | :--- |
| 問い | As scoping material | Confirm problem-setting details in the introduction |
| 主張 | Only claims stated explicitly in the abstract | Mandatory for any claim not in the abstract |
| 手法 | No - never paraphrase the abstract into a method | Mandatory |
| 結果 (numbers) | No in principle (exception below) | Mandatory; transcribe with conditions |
| 限界と前提 | No | Mandatory (from the authors' own text) |

- Numbers come from the full text in principle. A number that appears only in the abstract may be used exceptionally, and the note text must then state explicitly that the number is abstract-sourced.
- Every note declares its grounding depth and route in 履歴メモ (e.g. "全文 (arXiv v3) に接地", "要旨のみに接地"). A declared abstract-only grounding is not by itself a demotion (the kb-ai-research prototype notes are declared abstract-only). A mismatch between the declaration and the actual grounding is the heaviest audit return reason.

### Numeric Grounding Discipline

- A benchmark or result number is written **only together with its conditions** (target model, hardware, dataset, and the comparison baseline). A number without conditions is treated as a de-facto `[P4]` violation (see okf-wiki) and is returned at audit.
- Ground each number to the version actually read (arXiv ID + version). Never mix results from different versions or different experimental settings in one statement.
- A number may be written only if it is traceable to a retained full-text snapshot (see Evidence below).
- Positioning claims ("best", "state-of-the-art", ...) must distinguish the original paper's own claim from a synthesis informed by later work, and must carry a timepoint (which version / as-of comparison), because they age quickly.

### Note Form (Paper-Centric)

- Body: 7 sections - 問い / 主張 / 手法 / 結果 / 限界と前提 / 関連概念 / 履歴メモ. The 結果 section carries mandatory condition fields for every number (paired with the Numeric Grounding Discipline above).
- `basis`: `external` by default; `mixed` when the author's own synthesis, comparison, or experiment notes materially enter the note (declare this in 履歴メモ).
- Strand tags: exactly one primary Tier A `strand:` tag per note (okf-design-conventions section 4; in `kb-ai-research`: `strand:inference-serving`, `strand:agents`, `strand:bert-applications`). Secondary strand tags may be added; aggregation is by the primary tag.
- Declare the grounding route (A/B/C) and depth in 履歴メモ (see Two-Stage Grounding).
- References to other KBs' concepts are by slug mention only, never relative links (okf-design-conventions section 6).

### Intra-KB Link Rules

- Link, with a relative Markdown link, to notes that **exist at write time** - only where a real relation exists; never pad links to raise counts.
- Concepts not yet created are written as **bare slug text**, never as links (a dangling link fails `validate`).
- **End-of-stage link-conversion pass**: at each stage gate, scan existing notes (including prototype and earlier-stage notes) for bare slug mentions of concepts created in that stage; where the relation is real, convert the mention to a link. Apply the same check in the reverse direction (new note -> existing notes).
- Every link carries a one-line positioning phrase (why the two concepts relate); never leave a bare list of links.

### Evidence & check-evidence (Paper-Centric)

- Gate invocation form: okf-design-conventions section 7 (paper-centric row) - `okf validate` with `--check-evidence` always receives the KB's own `--evidence-dir <wiki>/evidence` explicitly.
- Direct `verify-source` output into the KB's own evidence dir with `--cache-dir <wiki>/evidence`, so snapshots land in the layout `check-evidence` scans (`evidence/arxiv/`, `evidence/snapshots/`).
- Pass criteria for this profile: `referenced` equals the number of distinct arXiv snapshots appearing across all notes' `sources:`; `missing` is 0; orphans are allowed only with an `orphan_reason`.
- Full-text snapshots (route A reading copies): if the tool's arXiv resolution covers them, count them the same way; if they fall outside the tool's scope, sample-verify their sidecar sha256 independently at audit time (kb-shana-ops precedent) and record in the audit record which of the two regimes applied. Never write a number whose full-text snapshot is not retained.
- Route B / C web evidence follows the standard snapshot + `.meta.json` handling in source-verify and is covered by `check-evidence` as usual.
- At batch preflight, confirm the tool's current evidence behaviour (sidecar format, resolution rules, count semantics) against okf-design-conventions sections 3 and 7; where the tool's settled spec differs from this profile, the tool spec wins and the discrepancy is recorded in the batch report.

### Freshness (Paper-Centric Notes)

- `verified_at` is the date the note was grounded against the original source - for this profile, the full-text grounding date - not the date the note was written (freshness model: okf-design-conventions section 2).
- When an arXiv version bump changes a paper's claims, update the note and record the change in 履歴メモ. A superseded positioning claim is marked deprecated, never silently rewritten (okf-design-conventions section 2). No new gate is created for this.

### Audit Points (Profile-Specific)

In addition to the Staged Delegation & Audit Protocol in the SKILL.md:

- Every claim is grounded in one of the note's `sources:` - above all, every number in 結果 carries its conditions.
- Every number is traceable to a retained full-text snapshot; abstract-sourced numbers are marked as such in the note text.
- No version mix-ups (arXiv version numbers; arXiv vs. conference version).
- No claim exceeds the abstract's scope without full-text confirmation; the declared grounding depth matches reality.
- The link rules above are followed: no links to not-yet-created concepts; no missed conversions in the end-of-stage pass.
- Strand tag values show no spelling drift.
