---
name: KB Batch Collection Discipline
description: Select candidate concepts and run, close, reflect on, and audit multi-concept OKF knowledge acquisition batches (patents, papers, web) with resumable work dirs, evidence gates, and staged delegation.
trigger_conditions:
  - batch collection
  - scale-up batch
  - multi-concept batch
  - candidate selection
  - next batch candidates
  - staged delegation
  - batch report
  - バッチ収集
  - 一括収集
  - フェーズ収集
  - 候補選定
  - 次バッチ候補
  - 委譲監査
---

# KB Batch Collection Discipline

## Purpose & Skill Composition

Use this skill whenever selecting next-batch concept candidates or running, supervising, and auditing a multi-concept knowledge collection batch (EPO patents, Crossref/arXiv papers, or web primary sources) across an OKF wiki.

For paper-centric KBs (papers as the primary sources, e.g. `kb-ai-research`), also apply the **Paper-Centric Batch Profile** section below; it operationalizes the Phase-2 paper target in `## Closing with Evidence` for that KB type.

This skill governs **candidate selection, batch orchestration, progress accounting, closing gates, reflection handoff, and staged delegation**. Delegate low-level mechanics to existing skills instead of duplicating them:
- Note structure, frontmatter, `[P1]`–`[P9]` provenance rules, and indexing → `.agents/skills/okf-wiki/SKILL.md`
- Single-publication / CQL EPO ingestion → `.agents/skills/patent-to-wiki/SKILL.md`
- DOI, arXiv, and web snapshot verification + `--orphan-reason` → `.agents/skills/source-verify/SKILL.md`
- Settled design conventions (value domains, freshness, evidence layout, tag tiers, version grounding, cross-KB references, gate invocation forms) → `.agents/skills/okf-design-conventions/SKILL.md` — read at batch start.

## Candidate Selection & Proposal (Phase 1 / New-Concept Batches)

Whenever proposing or selecting new concept candidates for a batch of $N$ concepts, never pick topics ad hoc. Execute this deterministic protocol **before** authoring `<work-dir>/prompt.md`:

1. **Read-Only Inventory & Primary-Source Scan**:
   - Read `<wiki>/concepts/index.md` (all existing slugs/titles), `<wiki>/SUMMARY.md` (domain boundary), `<wiki>/synonyms.json` (if present), and recent `<work-dir>/report.md` files (including any unmerged staging KBs under `.kb-*-work/*/`).
   - Scan existing `concepts/*.md` (`## Taxonomy`, `## Workflow / Implementation`, `## Synergistic Combinations`) and verified caches (`data/external/source_verify/` for the shared-cache semiconductor KB, or the target KB's own `<wiki>/evidence/`) for uncollected topics referenced in patents, paper abstracts, or vendor datasheets. If any listed input is absent from the workspace, name it in the proposal header (`Inputs missing: <path>`); never infer its contents.
   - Run deterministic mechanical inspection once the draft proposal is assembled: `uv run okf candidate-check <wiki> --proposal <work-dir>/proposal.md`. Read returned relations (the tool returns neutral relations only, without verdict or weights; use `--fail-on <kind>` if asserting) and ensure verbatim quoted mentions resolve.
   - **Gap-Axis Statement**: Before listing candidates, state in ≤ 3 sentences which gap axis or axes the pool follows (e.g. cooling devices, packaging/substrates, TIMs, composites), the in-KB evidence density behind that choice, and the strongest competing axis not chosen. The pool may span axes; never present it as the only valid gap analysis.
2. **Five Mandatory Evaluation Criteria `(a)`–`(e)`**:
   - **(a) Taxonomic Adjacency**: Does the candidate naturally extend an existing concept lineage (e.g. passive two-phase transport, phase-change cooling, metal-diamond/graphite composites, die-attach/substrates)? Must cite $\ge 1$ existing `concepts/<slug>.md` and the structural axis extended.
   - **(b) Primary-Source Emergence**: Is the topic already mentioned in collected primary sources (ingested patent titles/claims, verified paper titles/abstracts, or vendor `.extracted.txt` snapshots) without having its own concept note? Cite the concrete in-KB artifact (`concepts/<slug>.md`, `patents/<PUB>.md`, `DOI`, or snapshot URL) **and quote the candidate term (or a stated synonym) exactly as it appears there**. A citation counts only if the artifact itself names the topic: thematically adjacent artifacts (e.g. a generic "semiconductor package" patent title for an underfill candidate), site navigation/menu text, and a paper's reference-list entries do not count. Otherwise mark `taxonomy-gap` (proposed solely via `(a)`). At least $\ge 2/3$ of proposed candidates should have direct, quoted in-KB mentions. Drop any cited source whose own framing is off-domain under `(e)`.
   - **(c) Deduplication & Scope Boundary**: Inspect relations from `uv run okf candidate-check <wiki>`. `name_occupied` indicates physical file occupation (decide whether to rename candidate or expand existing note). `synonyms` membership and `tag_on` are factual provenances and NEVER rejection reasons: record them under `Relations:`, decide selection weighting, and resolve post-batch. State a 1-line scope boundary vs. the closest existing note; if that note already cites the same primary sources, say what it covers and what the candidate adds.
   - **(d) Portfolio Balance (`established` vs. `niche`)**: Classify each candidate as `established` (mature topic with abundant EPO patents $\ge 5$, papers $\ge 3$, and $\ge 2$ distinct vendor datasheets) or `niche` (specialized/exploratory material or mechanism with narrower vendor/literature density). Target a **`~2 : 1` (`established : niche`)** mix (e.g. **2 `established` + 1 `niche`** for $N=3$).
   - **(e) Domain Fit & Trap Guard**: Confirm the candidate sits inside the target KB domain (`<wiki>/SUMMARY.md` — e.g. semiconductor thermal materials & electronics thermal management) and state known off-domain query traps to exclude (e.g. building HVAC, solar thermal, EV battery packs).
3. **Over-Generation ($2N$ Pool) & Owner Selection**:
   - Present **approximately $2N$ candidates** ($\ge 2N$; e.g. **6 candidates for an $N=3$ batch**, maintaining the `~2 : 1` `established : niche` ratio across the pool) with an evaluation memo for every candidate, followed by a recommended $N$-concept subset. Wait for the owner to choose/approve the final $N$ concepts before launching the batch:
   ````markdown
   **Gap axis**: <axis/axes chosen; in-KB evidence density; strongest competing axis not chosen>
   ### Candidate Proposal Pool (Target: <N> concepts; Pool: <2N> candidates)
   1. **`<candidate-slug>`** — <Title JP（EN）> [`established` | `niche`]
      - **(a) Adjacency**: Extends `concepts/<existing-slug>.md` (<taxonomic axis>).
      - **(b) Primary-Source Emergence**: `"<quoted term>"` in `<concepts/x.md | patents/PUB.md | DOI | snapshot URL>` (<brief context>) — or `taxonomy-gap`.
      - **(c) Deduplication**: Hard-checked `concepts/index.md` + `synonyms.json`; distinct from `concepts/<closest>.md` (<scope boundary>). Tag collisions: <colliding note/tag, or `none`>.
      - **(d) Balance / Viability**: `<established | niche>` — <expected patent / paper / vendor datasheet availability>.
      - **(e) Domain Fit & Traps**: <semiconductor/electronics thermal role>; exclude <off-domain noise traps>.
   ...
   **Recommended <N>-Concept Set (2:1 mix)**: `<slug-1>` (`established`), `<slug-2>` (`established`), `<slug-3>` (`niche`) — deferred runner-ups: `<slug-4>`, `<slug-5>`, `<slug-6>`.
   ````

## Batch Setup & Resumable Execution

Every batch run must establish an isolated, resumable workspace before making any KB or network call:

1. **Work Directory**: Create `.kb-<project>-work/<phase>-<batch>/` (gitignored under `.kb-*`).
   - For graph-expanding patent discovery batches (Phase 1) that may pull noisy candidates, initialize a staging wiki at `.kb-<project>-work/<phase>-<batch>/wiki/` and merge into the live KB only after pruning and verification.
   - For bounded enrichment batches on existing concepts (Phases 2–3), operate on the target concept files in the live KB after taking a milestone folder backup.
2. **Prompt & Launch Log**: Save the exact worker instructions to `<work-dir>/prompt.md` (carrying forward lessons from the prior batch's Reflection). Capture worker stdout/stderr to `<work-dir>/run.log` and append an explicit `EXIT:<code>` footer when the process terminates so silent crashes or quota kills are never mistaken for clean completions.
3. **Preflight Reflection, Learned-Rule & Candidate Rationale Record**: At batch start (before running queries or editing concepts), the worker must read the `## Reflection` sections of previous batch `report.md` files under `.kb-<project>-work/*/` (at minimum the most recent completed batch, if any exist), the `<!-- LEARNED -->` block at the tail of `<wiki>/.agents/AGENTS.md` (if present), and `.agents/skills/okf-design-conventions/SKILL.md`. In the `## Preflight` section of `<work-dir>/report.md`, record:
   - One line per prior report read: `Prior Reflection consulted: <path>` (or `none (first batch)`), plus the carry-forward rules applied.
   - For new-concept batches, a `### Candidate Selection Rationale` block recording the approved candidates, their `(a)`–`(e)` selection basis, and the deferred runner-ups from the $2N$ proposal pool (for enrichment-only batches on existing concepts, record `Target selection: existing concepts assigned for enrichment (<reason>)`):
     ````markdown
     ### Candidate Selection Rationale
     - **Pool & Mix**: Selected <N> of <2N> proposed candidates (owner-approved YYYY-MM-DD; mix: <E> `established` + <M> `niche`; deferred runner-ups: `<slug-4>`, `<slug-5>`, `<slug-6>`).
     - **Tag collisions**: <candidate -> colliding note/tag, or `none`> (warnings only, never a rejection; resolve post-batch via `okf sync-synonyms` or tag reassignment).
     - **Selected concepts**:
       1. **`<slug-1>`** [`established` | `niche`] — (a) adjacent to `concepts/<adj>.md`; (b) surfaced in `<source-id/path>`; (c) distinct from `concepts/<closest>.md` (<scope boundary>); (e) semiconductor thermal role (<trap guard>).
     ````
4. **Incremental Per-Concept Status Lines**: Ensure `<work-dir>/report.md` exists at batch start (orchestrator or worker creates it if absent) and append/update one numbered status line **immediately after each concept finishes** (never batch all reporting to the very end). If a run dies mid-batch from quota or rate limits, restart from `<work-dir>/report.md` at the first uncompleted concept — never from scratch.
   - **Patent batch line format**:
     `N. <concept> — query: <CQL> — ingested: <PUB1, PUB2, ...> — note: yes/no — problems: <none | details>`
   - **Paper / Web batch line format**:
     `N. <concept> — adopted: <ID/URL, ...> — rejected: <none | ID/URL (reason)> — problems: <none | details>`

## Closing with Evidence (No Silent Drops)

A batch — or an individual concept within it — closes as `done` **only with objective evidence recorded in `<work-dir>/report.md`**:

1. **Required Gate Outputs**: Paste the exact command and output for all four end-of-batch gate commands (three gate types: `validate`, `index` + `index --check`, `stats`) into `report.md`:
   ```bash
   uv run okf validate <wiki> --strict-provenance --check-evidence --evidence-dir <wiki>/evidence
   uv run okf index <wiki>
   uv run okf index <wiki> --check
   uv run okf stats <wiki> --json
   ```
   - The `--evidence-dir` is always explicit (okf-design-conventions sections 3 and 7): use the target KB's own evidence dir. For a KB whose evidence lives in the shared repo cache (the semiconductor patent-type KB), pass that cache path explicitly instead - `--evidence-dir data/external/source_verify`, run from the repository root. Never rely on the CWD-relative default.
   - Gate pass criteria: `validate` exits `0` with `0` errors, `0` warnings, `0` missing cited snapshots (`[E1]`), and `0` unexplained orphan snapshots (`[E2]`); `index --check` exits `0`; `stats` reports `"broken_links": 0` and `"undeclared": 0`.
   - Index gate exception: For purely additive enrichment batches on existing notes (Phases 2–3), `uv run okf index <wiki> --check` alone is an acceptable index gate; running a full `uv run okf index <wiki>` write is required when notes are added, removed, or retitled.
2. **Orphan & Off-Topic Hygiene Before Closing**:
   - In patent staging batches, prune uncited off-topic patent nodes (and their unreferenced family stubs) before running final gates so orphan count is `0`.
   - In web batches, every newly fetched snapshot that is not cited in any note's `sources:` must receive an objective rejection reason via `uv run okf verify-source <url> --orphan-reason "<reason>"` before running the final `validate --check-evidence` gate. Pre-existing explained orphans (`with reason`) are allowed; `unexplained` must be `0`.
3. **Per-Concept Source Targets & Floors**:
   Each concept closes as `done` only when it reaches the batch target:
   - Patents (Phase 1): Target 3–5 patents.
   - Papers (Phase 2): Target 3 peer-reviewed papers (with $\ge 1$ quantitative claim where abstracts exist); 2 acceptable only when literature is genuinely sparse, explicitly justified in `problems:` and `## Reflection`. For paper-centric KBs, the Paper-Centric Batch Profile below defines the composition and grounding for this target.
   - Web (Phase 3): Target 2 distinct-vendor primary sources with quantitative specs; 1 acceptable only when vendor coverage is sparse, explicitly justified in `problems:` and `## Reflection`.
4. **Partial Close Rule**:
   - Conclude the status table with explicit totals: `**Totals: X done / Y partial / Z failed.**`
   - If a concept hits rate limits, lacks domain hits, or fails verification, leave it marked `partial` or `failed` (open) with the concrete reason (`rate_limit`, `sparse_literature`, `http_403`, `validation_error`). **Never** pad a concept with off-topic sources, invent citations from memory, or round a partial result up to `done`.

## Paper-Centric Batch Profile

For paper-centric KBs (e.g. `kb-ai-research`), this profile operationalizes the Phase-2 paper target. The full profile text is the reference file `.agents/skills/kb-batch-discipline/references/paper-centric-profile.md` in this skill folder - required reading before running such a batch. The essentials:

- **Composition**: 3 papers per concept (1 defining original + 2 follow-up / rival / application); web sources only from official repositories / official technical blogs (guideline 0-2). Never pad a concept with unrelated papers to reach the count.
- **Two-stage grounding**: the abstract confirms identity and adoption (stage 1); the full text grounds 手法 / 結果 / 限界と前提 (stage 2), acquired via route A (arXiv HTML/PDF), route B (proceedings as a web source with `retrieved=`), or route C (official repos/docs, release/commit pinned when a claim depends on it). Numbers are written only with their conditions (model / hardware / dataset / baseline); a number without conditions is a de-facto `[P4]` violation.
- **Note form**: 7 sections （問い / 主張 / 手法 / 結果 / 限界と前提 / 関連概念 / 履歴メモ）; exactly one primary Tier A `strand:` tag; the grounding route and depth are declared in 履歴メモ - a mismatch between the declaration and reality is the heaviest audit return reason.
- **Links**: link only to notes that exist at write time; not-yet-created concepts are bare slug text; convert slug mentions to links in the end-of-stage link-conversion pass.
- **Evidence**: direct `verify-source` output with `--cache-dir <wiki>/evidence`; gates always pass `--evidence-dir <wiki>/evidence` explicitly (okf-design-conventions sections 3 and 7); pass requires `referenced` equal to the distinct arXiv snapshots across `sources:` and `missing` 0.

## Reflection & Cross-Batch Handoff

Every `<work-dir>/report.md` must end with a `## Reflection` section written immediately after the gates run:

1. **Coverage & Shortfalls**: Exact count of concepts completed vs. target, explicitly noting whether preflight `(d) established / niche` source-availability estimates held true and why any concept fell below target (e.g. niche materials with only 2 peer-reviewed papers).
2. **Failures & Rejections**: Queries or URLs that returned `not_found` / `403` / SSL errors, off-topic search noise (e.g. supporting-information component DOIs, battery/building cooling hits, wrong material class), or metadata anomalies (e.g. conference year vs. registry year, missing abstracts).
3. **Mid-Batch Corrections**: Query refinements, pruned nodes, text/typo fixes, or claim-scope restrictions (such as restricting body bullets to title-level claims when an abstract is unavailable in the ledger).
4. **Carry-Forward Rules for Next Batch**: 1–3 concrete, actionable instructions for the next phase/batch (e.g. API filters to apply, dead vendor domains to skip, material-class disambiguation checks), plus any notable **uncollected candidate topics (`(b)` seeds)** spotted in newly ingested patents, papers, or vendor datasheets (with source IDs) to feed the next batch's `Candidate Selection & Proposal`.

**Mandatory Handoff Rule**: When authoring `<next-work-dir>/prompt.md`, the orchestrator must read the previous batch's `## Reflection` and copy its carry-forward rules and known dead/off-topic traps into the new prompt's constraints. Even when launched with a terse prompt, the worker must independently read prior `## Reflection` sections at batch start and log `Prior Reflection consulted: <path>` in `<work-dir>/report.md` (see `Batch Setup` item 3).

**Skill-Improvement Proposals (Two-Stage Gate)**:
When a batch discovers a reusable workflow rule or failure guard that should be codified across all future batches (beyond one-off query tweaks), emit a reviewable proposal file to `.agents/skills/.proposals/<skill-slug>.md` (where `<skill-slug>` matches the target skill folder name, e.g. `kb-batch-discipline`, `source-verify`, `patent-to-wiki`, or `okf-wiki`):
- **Two-Stage Gate**: Workers may write or append proposals under `.agents/skills/.proposals/<skill-slug>.md` (which is ignored by `okf skills` discovery) and **must never edit `.agents/skills/<skill-slug>/SKILL.md` directly**. Only the orchestrator/reviewer promotes an approved proposal into the live `SKILL.md` via `modify-skill` after batch audit passes.
- **Proposal File Format** (`.agents/skills/.proposals/<skill-slug>.md`):
  ````markdown
  # Skill update proposal: <short title>

  - Target: `.agents/skills/<skill-slug>/SKILL.md`
  - Source batch: `<work-dir>/report.md` (`YYYY-MM-DD`)
  - Target section: `<## Heading in SKILL.md>`

  ## Observed problem / evidence
  - <1–3 bullets citing concrete batch failures, gate errors, or audit findings>

  ## Proposed rule change
  - <exact wording or bullet to insert/update in the target section>

  ## Review checklist
  - [ ] Rule is reusable across batches (not a single-concept workaround).
  - [ ] Commands and flags match the current `okf` CLI.
  - [ ] `uv run okf skills . --check --json` passes after promotion.
  ````

## Staged Delegation & Audit Protocol

Delegation between the **Orchestrator/Reviewer** (e.g. Shana) and the **Batch Worker** (e.g. OpenCode) progresses through three stages. The evidence and reflection rules in `## Closing with Evidence (No Silent Drops)` and `## Reflection & Cross-Batch Handoff` are the precondition for every stage.

| Stage | When Used | Worker Responsibility | Reviewer / Orchestrator Verification |
| :--- | :--- | :--- | :--- |
| **Stage 1: Full Verification** | Phase 1 (new concepts/patents), new KB domain, or new worker model | Run in staging wiki (`.kb-*-work/<batch>/wiki/`), prune off-topic nodes, pass gates, write `report.md` + `Reflection`. | **100% check**: inspect all drafted concept notes, verify source relevance, re-run gates, then merge into live KB. |
| **Stage 2: Sample Audit** | Phases 2–3 (papers/web) after Stage 1 passes clean | Edit target concept notes, run `okf verify-source`, record `--orphan-reason` on rejected snapshots, pass all live-KB gates, write `report.md` + `Reflection`. | **Stratified sample audit** ($\ge 3$ concepts or $15\text{--}20\%$, plus all concepts with `problems != none` or below-target counts). |
| **Stage 3: Anomaly Audit** | Mature steady-state batches after repeated clean Stage 2 passes | Same as Stage 2. | Check gate outputs and `report.md` totals; deep-audit **only anomalies** (concepts with `problems`, `< target` sources, or new orphan reasons). |

**Promotion & Demotion Rule**:
- Advance from Stage $k$ to Stage $k+1$ only after a **clean pass** (gates exit `0`, zero unverified/fabricated sources, zero silent drops, complete Reflection).
- Demote immediately back to Stage 1 if a sample audit uncovers a fabricated identifier, an ungrounded numeric claim outside an unverified marker, or a hidden failure.

**Sample-Audit Record Form** (recorded by Reviewer after Stage 2/3 batches):
````markdown
### Audit Summary (<Reviewer>, YYYY-MM-DD)
- **Stage**: Stage 2 (Sample Audit) | Stage 3 (Anomaly Audit)
- **Gate check**: `okf validate --strict-provenance --check-evidence --evidence-dir <wiki>/evidence` exit 0 (missing=0, unexplained_orphans=0); `okf index --check` exit 0
- **Sampled concepts & sources**:
  1. `<concept>` (`<source_id_or_url>`): <independent DOI/snapshot/.extracted.txt verification result — pass/fail>
  2. `<concept>` (`<source_id_or_url>`): <pass/fail>
  3. `<anomaly/problem concept>` (`<source_id_or_url>`): <pass/fail>
- **Verdict**: APPROVED (eligible for Stage <N>) | REWORK REQUIRED (<reason>)
````

## Hard Constraints (Non-Negotiables)

Every batch prompt and worker execution must enforce these five hard boundaries:

1. **Zero `.secrets/` or Credential Access**: Never read, print, or modify `.secrets/`, `.env`, or token caches. Never place internal absolute filesystem paths (such as home or root paths) in tracked repository files (`scripts/check_public_repo.py`).
2. **Strict File-Scope Lock**: A batch worker may write only to (a) its `<work-dir>/`, (b) the explicitly assigned concept/patent Markdown files, evidence caches via the `okf` CLI (`data/external/` for shared-cache KBs, or the KB-internal `<wiki>/evidence/`), files the `okf` CLI itself generates or appends (`patents/<PUB>.md` nodes, family stubs, and `<wiki>/log.md` via `okf ingest-epo`), (c) generated `index*.md` files updated by running `okf index`, and (d) optional skill-improvement proposals under `.agents/skills/.proposals/<skill-slug>.md`. Never modify `src/`, `tests/`, live skill files (`.agents/skills/*/SKILL.md`), or backup KB directories. Within an assigned note, updating stale internal narrative counts (e.g. `"上記 2 件"` → `"上記 3 件"` after adding a third source) to maintain consistency with added sources is allowed and is NOT a scope violation; record any such fix in the status line (`problems: ...`) and in `## Reflection` (`Mid-batch corrections`).
3. **No `git` Commands by Batch Workers**: Workers must not run `git add`, `git commit`, `git checkout`, `git reset`, or `git clean`. Repository git operations and KB backup snapshots are performed solely by the orchestrator/owner.
4. **Rate-Limit Politeness & Fail-Safe Stop**: Always use `okf ingest-epo` / `okf verify-source` rate limiters (3.0 s EPO/arXiv, 1.5–2.5 s Crossref/Web) and URL-encode query parameters. If an API returns persistent HTTP 429 or ledger HTTP 403 (`exit 2`), stop network calls immediately and close the batch partially.
5. **See-Before-Cite & Material-Class Check**: Cite only identifiers resolved via ledger/snapshot. When a ledger record has no abstract, restrict body claims to what the title supports. Confirm exact material/device class (e.g. Cu sintering vs. Ag sintering; chip TIM vs. building/battery PCM) before adopting a source.
