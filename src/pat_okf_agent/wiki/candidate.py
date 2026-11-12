"""Candidate screening and evidence verification for OKF wiki.

Implements deterministic deduplication, filename/synonym hard collision detection,
Tier A/B tag collision scanning, and verbatim evidence checks for candidate concepts.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..epo import _safe_name
from .model import Concept, load_concept
from .synonyms import (
    DEFAULT_EXCLUDE_TAGS,
    SynonymIndex,
    _load_existing_manual_groups,
    _load_existing_tier_a_tags,
    classify_tag_tier,
)


@dataclass
class HardCollision:
    collision_type: str  # "filename" | "synonym"
    matched_term: str
    target_file: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TagCollision:
    colliding_tag: str
    existing_concept_slug: str
    existing_concept_file: str
    tier: str  # "Tier A" | "Tier B"
    severity: str  # "warning" | "note"
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateCheckItem:
    slug: str
    established_or_niche: str | None = None
    hard_collisions: list[HardCollision] = field(default_factory=list)
    tag_collisions: list[TagCollision] = field(default_factory=list)
    verbatim_check: dict[str, Any] | None = None
    status: str = "pass"  # "pass" | "pass_with_warnings" | "hard_collision"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "established_or_niche": self.established_or_niche,
            "hard_collisions": [hc.to_dict() for hc in self.hard_collisions],
            "tag_collisions": [tc.to_dict() for tc in self.tag_collisions],
            "verbatim_check": self.verbatim_check,
            "status": self.status,
        }


@dataclass
class CandidateCheckResult:
    wiki_root: str
    inputs_status: dict[str, Any]
    scan_completeness: dict[str, Any]
    candidates: list[CandidateCheckItem] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": "0.1",
            "wiki_root": self.wiki_root,
            "inputs_status": self.inputs_status,
            "scan_completeness": self.scan_completeness,
            "candidates": [c.to_dict() for c in self.candidates],
            "summary": self.summary,
        }


class CandidateChecker:
    """Deterministic candidate checker verifying collisions and evidence."""

    def __init__(self, wiki_root: str | Path):
        self.wiki_root = Path(wiki_root).resolve()
        self.concepts_dir = self.wiki_root / "concepts"
        self.synonyms_path = self.wiki_root / "synonyms.json"

        # Load existing concept notes
        self._concepts: list[tuple[str, Path, list[str]]] = []
        if self.concepts_dir.exists():
            for p in sorted(self.concepts_dir.glob("*.md")):
                if p.name == "index.md" or not p.is_file():
                    continue
                try:
                    concept = load_concept(p, self.wiki_root)
                    self._concepts.append((p.stem, p, list(concept.tags)))
                except Exception:
                    # Fallback to empty tags if corrupt
                    self._concepts.append((p.stem, p, []))

        # Load synonyms
        self._synonym_index: SynonymIndex | None = None
        self._manual_groups: list[list[str]] = []
        self._tier_a_tags: list[str] = []
        self._all_synonym_terms: set[str] = set()

        if self.synonyms_path.exists():
            try:
                self._synonym_index = SynonymIndex.from_path(self.synonyms_path)
                for group in self._synonym_index.groups:
                    for term in group:
                        self._all_synonym_terms.add(
                            term.strip().replace("-", " ").casefold()
                        )
            except Exception:
                pass
            try:
                self._manual_groups = _load_existing_manual_groups(self.synonyms_path)
                loaded_tier_a = _load_existing_tier_a_tags(self.synonyms_path)
                if loaded_tier_a is not None:
                    self._tier_a_tags = loaded_tier_a
            except Exception:
                pass

    def check_inputs_status(self, proposal_path: str | Path | None = None) -> dict[str, Any]:
        """Check presence of required wiki files and caches."""
        missing: list[str] = []
        syn_ok = self.synonyms_path.exists()
        if not syn_ok:
            missing.append("synonyms.json")

        sv_cache_dir = self.wiki_root / "data" / "external" / "source_verify"
        sv_present = sv_cache_dir.exists()

        prop_present = False
        if proposal_path is not None:
            p_path = Path(proposal_path)
            prop_present = p_path.exists()
            if not prop_present:
                missing.append(f"proposal:{proposal_path}")

        return {
            "synonyms_json_present": syn_ok,
            "source_verify_cache_present": sv_present,
            "proposal_file_present": prop_present,
            "inputs_missing": missing,
        }

    def scan_completeness(self) -> dict[str, Any]:
        """Collect metrics on the files scanned during check."""
        total_tags = sum(len(tags) for _, _, tags in self._concepts)
        tier_a_count = 0
        tier_b_count = 0
        for slug, _, tags in self._concepts:
            for tag in tags:
                tier = classify_tag_tier(
                    tag,
                    manual_groups=self._manual_groups,
                    tier_a_list=self._tier_a_tags,
                    concept_slug=slug,
                )
                if tier == "Tier A":
                    tier_a_count += 1
                elif tier == "Tier B":
                    tier_b_count += 1

        syn_groups_count = len(self._synonym_index.groups) if self._synonym_index else 0
        return {
            "scanned_concept_files": len(self._concepts),
            "scanned_concept_tags_total": total_tags,
            "scanned_synonym_groups": syn_groups_count,
            "scanned_tier_a_tags": tier_a_count,
            "scanned_tier_b_tags": tier_b_count,
        }

    def check_candidate(
        self,
        slug: str,
        *,
        established_or_niche: str | None = None,
        verbatim_check: dict[str, Any] | None = None,
    ) -> CandidateCheckItem:
        """Check a single candidate slug for hard and soft collisions."""
        hard_collisions: list[HardCollision] = []
        tag_collisions: list[TagCollision] = []

        norm_slug = slug.strip().casefold()
        norm_slug_spaced = norm_slug.replace("-", " ")

        # 1. Filename collision
        candidate_file = self.concepts_dir / f"{slug}.md"
        if candidate_file.exists():
            hard_collisions.append(
                HardCollision(
                    collision_type="filename",
                    matched_term=slug,
                    target_file=str(candidate_file.relative_to(self.wiki_root)),
                )
            )
        else:
            # Check against stems
            for c_slug, c_path, _ in self._concepts:
                if c_slug.casefold() == norm_slug:
                    hard_collisions.append(
                        HardCollision(
                            collision_type="filename",
                            matched_term=c_slug,
                            target_file=str(c_path.relative_to(self.wiki_root)),
                        )
                    )
                    break

        # 2. Synonym collision
        if norm_slug_spaced in self._all_synonym_terms or norm_slug in self._all_synonym_terms:
            hard_collisions.append(
                HardCollision(
                    collision_type="synonym",
                    matched_term=slug,
                    target_file="synonyms.json",
                )
            )

        # 3. Tag collisions
        for c_slug, c_path, tags in self._concepts:
            for tag in tags:
                tag_str = str(tag).strip()
                tag_norm = tag_str.casefold()
                tag_spaced = tag_norm.replace("-", " ")
                if tag_norm == norm_slug or tag_spaced == norm_slug_spaced:
                    tier = classify_tag_tier(
                        tag_str,
                        manual_groups=self._manual_groups,
                        tier_a_list=self._tier_a_tags,
                        concept_slug=c_slug,
                    )
                    if tier == "Tier A":
                        tag_collisions.append(
                            TagCollision(
                                colliding_tag=tag_str,
                                existing_concept_slug=c_slug,
                                existing_concept_file=str(
                                    c_path.relative_to(self.wiki_root)
                                ),
                                tier="Tier A",
                                severity="warning",
                                message=(
                                    f"Collides with Tier A tag on '{c_slug}' "
                                    "(controlled vocabulary; resolve post-batch)"
                                ),
                            )
                        )
                    elif tier == "Tier B":
                        tag_collisions.append(
                            TagCollision(
                                colliding_tag=tag_str,
                                existing_concept_slug=c_slug,
                                existing_concept_file=str(
                                    c_path.relative_to(self.wiki_root)
                                ),
                                tier="Tier B",
                                severity="note",
                                message=(
                                    f"Collides with Tier B tag on '{c_slug}' "
                                    "(free descriptive tag; resolve post-batch)"
                                ),
                            )
                        )

        # Determine overall status
        if hard_collisions:
            status = "hard_collision"
        elif tag_collisions or (
            verbatim_check is not None and not verbatim_check.get("verbatim_found", True)
        ):
            status = "pass_with_warnings"
        else:
            status = "pass"

        return CandidateCheckItem(
            slug=slug,
            established_or_niche=established_or_niche,
            hard_collisions=hard_collisions,
            tag_collisions=tag_collisions,
            verbatim_check=verbatim_check,
            status=status,
        )

    def check_candidates(
        self,
        candidate_slugs: Iterable[str],
        *,
        proposal_path: str | Path | None = None,
    ) -> CandidateCheckResult:
        """Run candidate checks across a list of slugs."""
        inputs_status = self.check_inputs_status(proposal_path)
        scan_completeness = self.scan_completeness()

        items: list[CandidateCheckItem] = []
        hard_count = 0
        tier_a_count = 0
        tier_b_count = 0
        verbatim_miss_count = 0

        for slug in candidate_slugs:
            item = self.check_candidate(slug)
            items.append(item)
            if item.hard_collisions:
                hard_count += len(item.hard_collisions)
            for tc in item.tag_collisions:
                if tc.tier == "Tier A":
                    tier_a_count += 1
                elif tc.tier == "Tier B":
                    tier_b_count += 1
            if item.verbatim_check and not item.verbatim_check.get("verbatim_found", True):
                verbatim_miss_count += 1

        summary = {
            "total_candidates": len(items),
            "hard_collision_count": hard_count,
            "tier_a_warning_count": tier_a_count,
            "tier_b_note_count": tier_b_count,
            "verbatim_miss_count": verbatim_miss_count,
        }

        return CandidateCheckResult(
            wiki_root=str(self.wiki_root),
            inputs_status=inputs_status,
            scan_completeness=scan_completeness,
            candidates=items,
            summary=summary,
        )

    def check_proposal(self, proposal_path: str | Path) -> CandidateCheckResult:
        """Run candidate checks across a proposal markdown file."""
        p_path = Path(proposal_path).resolve()
        if not p_path.exists():
            inputs_status = self.check_inputs_status(p_path)
            return CandidateCheckResult(
                wiki_root=str(self.wiki_root),
                inputs_status=inputs_status,
                scan_completeness=self.scan_completeness(),
                candidates=[],
                summary={
                    "total_candidates": 0,
                    "hard_collision_count": 0,
                    "tier_a_warning_count": 0,
                    "tier_b_note_count": 0,
                    "verbatim_miss_count": 0,
                },
            )

        text = p_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_proposal_candidates(text)

        inputs_status = self.check_inputs_status(p_path)
        scan_completeness = self.scan_completeness()

        items: list[CandidateCheckItem] = []
        hard_count = 0
        tier_a_count = 0
        tier_b_count = 0
        verbatim_miss_count = 0

        for cand in parsed:
            slug = cand["slug"]
            est_niche = cand.get("established_or_niche")
            v_check = check_verbatim_evidence(
                self.wiki_root, cand.get("cited_target", ""), cand.get("quoted_term", "")
            )
            item = self.check_candidate(
                slug,
                established_or_niche=est_niche,
                verbatim_check=v_check,
            )
            items.append(item)
            if item.hard_collisions:
                hard_count += len(item.hard_collisions)
            for tc in item.tag_collisions:
                if tc.tier == "Tier A":
                    tier_a_count += 1
                elif tc.tier == "Tier B":
                    tier_b_count += 1
            if item.verbatim_check and not item.verbatim_check.get("verbatim_found", True):
                verbatim_miss_count += 1

        summary = {
            "total_candidates": len(items),
            "hard_collision_count": hard_count,
            "tier_a_warning_count": tier_a_count,
            "tier_b_note_count": tier_b_count,
            "verbatim_miss_count": verbatim_miss_count,
        }

        return CandidateCheckResult(
            wiki_root=str(self.wiki_root),
            inputs_status=inputs_status,
            scan_completeness=scan_completeness,
            candidates=items,
            summary=summary,
        )


def parse_proposal_candidates(markdown_text: str) -> list[dict[str, Any]]:
    """Parse candidate blocks and their (a)-(e) metadata from proposal markdown."""
    lines = markdown_text.splitlines()
    candidates: list[dict[str, Any]] = []

    current_candidate: dict[str, Any] | None = None
    current_block_lines: list[str] = []

    def flush_current():
        nonlocal current_candidate, current_block_lines
        if current_candidate is not None:
            block_text = "\n".join(current_block_lines)

            est_match = re.search(
                r"\[`?(established|niche)`?\]", block_text, re.IGNORECASE
            )
            if est_match:
                current_candidate["established_or_niche"] = est_match.group(1).lower()

            b_match = re.search(
                r"-\s+(?:\*\*(?:\(b\)|b\))\s*Primary-Source Emergence\*\*|\(?b\)?\s*Primary-Source Emergence):\s*(.*)",
                block_text,
                re.IGNORECASE,
            )
            if b_match:
                b_line = b_match.group(1).strip()
                if "taxonomy-gap" in b_line.lower():
                    current_candidate["quoted_term"] = ""
                    current_candidate["cited_target"] = "taxonomy-gap"
                else:
                    quote_m = re.search(r'["“\']([^"”\']+)["”\']', b_line)
                    in_m = re.search(r'\bin\s+([^\s\(\)]+)', b_line)
                    current_candidate["quoted_term"] = (
                        quote_m.group(1).strip() if quote_m else ""
                    )
                    current_candidate["cited_target"] = (
                        in_m.group(1).strip("`<>") if in_m else ""
                    )
            candidates.append(current_candidate)

    for line in lines:
        cand_m = re.match(
            r"^\s*(?:\d+\.|\-)\s+\*\*(?:`?)([a-z0-9][a-z0-9-]+[a-z0-9]|[a-z0-9]+)(?:`?)\*\*(.*)",
            line,
        )
        if cand_m:
            flush_current()
            slug = cand_m.group(1).strip()
            current_candidate = {
                "slug": slug,
                "established_or_niche": None,
                "quoted_term": "",
                "cited_target": "",
                "raw_block": line,
            }
            current_block_lines = [line]
        elif current_candidate is not None:
            if (
                re.match(r"^#{1,3}\s+", line)
                or line.startswith("---")
                or line.startswith("**Recommended")
            ):
                flush_current()
                current_candidate = None
                current_block_lines = []
            else:
                current_block_lines.append(line)

    flush_current()
    return candidates


def check_verbatim_evidence(
    wiki_root: str | Path,
    citation_target: str,
    quoted_term: str,
) -> dict[str, Any]:
    """Verify that quoted_term appears verbatim in the cited artifact."""
    root_path = Path(wiki_root).resolve()
    clean_target = citation_target.strip("`<>").strip()

    if not clean_target or clean_target.lower() == "taxonomy-gap" or not quoted_term:
        return {
            "claimed_emergence": False,
            "cited_target": clean_target or "taxonomy-gap",
            "resolved_artifact_path": None,
            "artifact_exists": True,
            "quoted_term": quoted_term or "",
            "verbatim_found": True,
            "match_count": 0,
            "snippet": "taxonomy-gap (no primary-source emergence claimed)",
        }

    sv_dir = root_path / "data" / "external" / "source_verify"
    candidate_paths: list[Path] = [
        root_path / clean_target,
    ]
    if clean_target.startswith("10.") or "/" in clean_target:
        safe_doi = _safe_name(clean_target)
        candidate_paths.extend([
            sv_dir / f"crossref_{safe_doi}.json",
            sv_dir / "crossref" / f"{safe_doi}.json",
            sv_dir / f"openalex_{safe_doi}.json",
            sv_dir / "openalex" / f"{safe_doi}.json",
            sv_dir / f"{safe_doi}.json",
            sv_dir / f"{safe_doi}.extracted.txt",
        ])
    candidate_paths.extend([
        sv_dir / clean_target,
        sv_dir / Path(clean_target).name,
    ])

    resolved_file: Path | None = None
    for p in candidate_paths:
        if p.exists() and p.is_file():
            resolved_file = p
            break

    if resolved_file is None:
        target_name = Path(clean_target).name
        if sv_dir.exists():
            for p in sv_dir.rglob("*"):
                if p.is_file() and p.name == target_name:
                    resolved_file = p
                    break

    if resolved_file is None:
        return {
            "claimed_emergence": True,
            "cited_target": clean_target,
            "resolved_artifact_path": None,
            "artifact_exists": False,
            "quoted_term": quoted_term,
            "verbatim_found": False,
            "match_count": 0,
            "snippet": f"artifact not found for citation target: {clean_target}",
        }

    rel_path = str(resolved_file.relative_to(root_path))
    try:
        content = resolved_file.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {
            "claimed_emergence": True,
            "cited_target": clean_target,
            "resolved_artifact_path": rel_path,
            "artifact_exists": True,
            "quoted_term": quoted_term,
            "verbatim_found": False,
            "match_count": 0,
            "snippet": f"failed to read artifact: {exc}",
        }

    norm_content = " ".join(content.split()).casefold()
    norm_term = " ".join(quoted_term.split()).casefold()

    match_count = norm_content.count(norm_term)
    verbatim_found = match_count > 0

    if verbatim_found:
        idx = norm_content.find(norm_term)
        start = max(0, idx - 40)
        end = min(len(norm_content), idx + len(norm_term) + 40)
        snippet = f"...{norm_content[start:end]}..."
    else:
        snippet = f"term '{quoted_term}' not found in artifact"

    return {
        "claimed_emergence": True,
        "cited_target": clean_target,
        "resolved_artifact_path": rel_path,
        "artifact_exists": True,
        "quoted_term": quoted_term,
        "verbatim_found": verbatim_found,
        "match_count": match_count,
        "snippet": snippet,
    }
