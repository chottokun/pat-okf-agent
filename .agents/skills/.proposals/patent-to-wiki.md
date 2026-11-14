# Skill update proposal: CN claims 404 — prefer a family member with claims

- Target: `.agents/skills/patent-to-wiki/SKILL.md`
- Source batch: `.kb-scaleup-work/nightly-20261111/report.md` (`2026-11-11`)
- Target section: `## Failure handling` / `### 4. Verify the result`

## Observed problem / evidence
- In this batch, 12 CN publications (`CN121793323A`, `CN120152246A`, `CN224006952U`, `CN121520059A`, `CN121116034A`, `CN121001321A`, `CN120625019A`, `CN122644550A`, `CN122625637A`, `CN122357999A`, `CN122327047A`, `CN122054997A`) returned claims 404, while 3 WO publications supplied claims. The CN-only concepts had to be restricted to title-level claims.
- The ingester reports `claims unavailable` as a warning, but the workflow has no explicit step to inspect the family for a WO/US/EP member with claims before accepting title-level descriptions.

## Proposed rule change
- In `### 4. Verify the result`, after checking `claims` and `claims_source`, add: "If claims are unavailable for a CN publication, inspect `family` and attempt `ingest-epo` on a WO/US/EP family member with claims before restricting the concept note to title-level claims. Record the fallback and the final claim source in the batch status line."
- In `## Failure handling`, replace the `claims unavailable` row with: "`claims unavailable` | try EPS for EP publications; for CN publications, try a WO/US/EP family member with claims; otherwise record warning and restrict body claims to title/biblio."

## Review checklist
- [x] Rule is reusable across batches (not a single-concept workaround).
- [x] Commands and flags match the current `okf` CLI.
- [x] `uv run okf skills . --check --json` passes after promotion.
