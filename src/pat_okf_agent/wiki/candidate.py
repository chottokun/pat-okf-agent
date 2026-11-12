"""Candidate screening and evidence verification for OKF wiki.

Implements deterministic relations inspection (name occupation, synonym membership,
tag provenance) and verbatim evidence checks for candidate concepts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..epo import _safe_name
from .model import load_concept
from .synonyms import (
    DEFAULT_EXCLUDE_TAGS,
    _load_existing_manual_groups,
    _load_existing_tier_a_tags,
    classify_tag_tier,
)


def _load_existing_generated_groups(synonyms_path: Path) -> list[list[str]]:
    """Load generated_groups symmetrically to _load_existing_manual_groups without raising."""
    if not synonyms_path.exists():
        return []
    try:
        data = json.loads(synonyms_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        raw_gen = data.get("generated_groups", [])
        if isinstance(raw_gen, list):
            groups: list[list[str]] = []
            for raw_group in raw_gen:
                if isinstance(raw_group, list) and len(raw_group) >= 2:
                    group = [
                        " ".join(t.split())
                        for t in raw_group
                        if isinstance(t, str) and t.strip()
                    ]
                    if len(group) >= 2:
                        groups.append(group)
            return groups
    return []


@dataclass
class CandidateRelations:
    name_occupied: dict[str, str] | None = None
    synonyms: list[dict[str, Any]] = field(default_factory=list)
    tag_on: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name_occupied": self.name_occupied,
            "synonyms": self.synonyms,
            "tag_on": self.tag_on,
        }


@dataclass
class CandidateCheckItem:
    slug: str
    established_or_niche: str | None = None
    relations: CandidateRelations = field(default_factory=CandidateRelations)
    verbatim_check: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "established_or_niche": self.established_or_niche,
            "relations": self.relations.to_dict(),
            "verbatim_check": self.verbatim_check,
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
            "version": "0.2",
            "wiki_root": self.wiki_root,
            "inputs_status": self.inputs_status,
            "scan_completeness": self.scan_completeness,
            "candidates": [c.to_dict() for c in self.candidates],
            "summary": self.summary,
        }


class CandidateChecker:
    """Deterministic candidate checker collecting relations and verifying evidence."""

    def __init__(self, wiki_root: str | Path):
        self.wiki_root = Path(wiki_root).resolve()
        self.concepts_dir = self.wiki_root / "concepts"
        self.synonyms_path = self.wiki_root / "synonyms.json"

        # Load existing concept notes
        self._concepts: list[tuple[str, Path, list[str]]] = []
        self._concept_slug_by_norm: dict[str, str] = {}
        if self.concepts_dir.exists():
            for p in sorted(self.concepts_dir.glob("*.md")):
                if p.name == "index.md" or not p.is_file():
                    continue
                stem = p.stem
                norm_stem = stem.replace("-", " ").casefold()
                self._concept_slug_by_norm[norm_stem] = stem
                self._concept_slug_by_norm[stem.casefold()] = stem
                try:
                    concept = load_concept(p, self.wiki_root)
                    self._concepts.append((stem, p, list(concept.tags)))
                except Exception:
                    # Fallback to empty tags if corrupt
                    self._concepts.append((stem, p, []))

        # Load synonyms
        self._manual_groups: list[list[str]] = []
        self._generated_groups: list[list[str]] = []
        self._tier_a_tags: list[str] = []

        if self.synonyms_path.exists():
            try:
                self._manual_groups = _load_existing_manual_groups(self.synonyms_path)
            except Exception:
                pass
            try:
                self._generated_groups = _load_existing_generated_groups(self.synonyms_path)
            except Exception:
                pass
            try:
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
        fixed_count = 0
        free_count = 0
        for slug, _, tags in self._concepts:
            for tag in tags:
                tier = classify_tag_tier(
                    tag,
                    manual_groups=self._manual_groups,
                    tier_a_list=self._tier_a_tags,
                    concept_slug=slug,
                )
                if tier == "Tier A":
                    fixed_count += 1
                elif tier == "Tier B":
                    free_count += 1

        total_syn_groups = len(self._manual_groups) + len(self._generated_groups)
        return {
            "scanned_concept_files": len(self._concepts),
            "scanned_concept_tags_total": total_tags,
            "scanned_synonym_groups": total_syn_groups,
            "scanned_synonym_groups_manual": len(self._manual_groups),
            "scanned_synonym_groups_generated": len(self._generated_groups),
            "scanned_fixed_vocabulary_tags": fixed_count,
            "scanned_free_tags": free_count,
        }

    def _classify_tag_provenance(self, tag: str, concept_slug: str) -> str:
        norm = tag.strip().casefold()
        if concept_slug and norm == concept_slug.strip().casefold():
            return "concept_slug"
        if self._tier_a_tags:
            tier_a_cf = {x.strip().casefold() for x in self._tier_a_tags if isinstance(x, str)}
            if norm in tier_a_cf:
                return "tier_a_tags"
        if self._manual_groups:
            manual_cf = {
                x.strip().casefold()
                for grp in self._manual_groups
                for x in grp
                if isinstance(x, str)
            }
            if norm in manual_cf:
                return "manual_group"
        return "free"

    def _resolve_generated_owner(self, group: list[str]) -> str | None:
        for term in group:
            norm = term.strip().replace("-", " ").casefold()
            if norm in self._concept_slug_by_norm:
                return self._concept_slug_by_norm[norm]
            folded = term.strip().casefold()
            if folded in self._concept_slug_by_norm:
                return self._concept_slug_by_norm[folded]
        if group:
            return group[0].strip().replace(" ", "-").lower()
        return None

    def check_candidate(
        self,
        slug: str,
        *,
        established_or_niche: str | None = None,
        verbatim_check: dict[str, Any] | None = None,
    ) -> CandidateCheckItem:
        """Check a single candidate slug and return its relations and evidence."""
        relations = CandidateRelations()
        norm_slug = slug.strip().casefold()
        norm_slug_spaced = norm_slug.replace("-", " ")

        # 1. Name occupied
        candidate_file = self.concepts_dir / f"{slug}.md"
        if candidate_file.exists():
            relations.name_occupied = {
                "matched_name": slug,
                "file": str(candidate_file.relative_to(self.wiki_root)),
            }
        else:
            for c_slug, c_path, _ in self._concepts:
                if c_slug.casefold() == norm_slug:
                    relations.name_occupied = {
                        "matched_name": c_slug,
                        "file": str(c_path.relative_to(self.wiki_root)),
                    }
                    break

        # 2. Synonym relations
        # 2a. manual groups
        for group in self._manual_groups:
            for term in group:
                t_norm = term.strip().casefold()
                t_spaced = t_norm.replace("-", " ")
                if t_norm == norm_slug or t_spaced == norm_slug_spaced:
                    relations.synonyms.append({
                        "kind": "manual",
                        "matched_term": term,
                        "group": list(group),
                        "owner": None,
                    })
                    break

        # 2b. generated groups
        for group in self._generated_groups:
            for term in group:
                t_norm = term.strip().casefold()
                t_spaced = t_norm.replace("-", " ")
                if t_norm == norm_slug or t_spaced == norm_slug_spaced:
                    owner = self._resolve_generated_owner(group)
                    relations.synonyms.append({
                        "kind": "generated",
                        "matched_term": term,
                        "group": list(group),
                        "owner": owner,
                    })
                    break

        # 3. Tag relations
        for c_slug, c_path, tags in self._concepts:
            for tag in tags:
                tag_str = str(tag).strip()
                tag_norm = tag_str.casefold()
                tag_spaced = tag_norm.replace("-", " ")
                if tag_norm in DEFAULT_EXCLUDE_TAGS:
                    continue
                if tag_norm == norm_slug or tag_spaced == norm_slug_spaced:
                    provenance = self._classify_tag_provenance(tag_str, c_slug)
                    relations.tag_on.append({
                        "tag": tag_str,
                        "concept": c_slug,
                        "file": str(c_path.relative_to(self.wiki_root)),
                        "provenance": provenance,
                    })

        return CandidateCheckItem(
            slug=slug,
            established_or_niche=established_or_niche,
            relations=relations,
            verbatim_check=verbatim_check,
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
        name_occupied_count = 0
        synonym_manual_count = 0
        synonym_generated_count = 0
        tag_relation_count = 0
        verbatim_miss_count = 0

        for slug in candidate_slugs:
            item = self.check_candidate(slug)
            items.append(item)
            if item.relations.name_occupied is not None:
                name_occupied_count += 1
            for syn in item.relations.synonyms:
                if syn.get("kind") == "manual":
                    synonym_manual_count += 1
                elif syn.get("kind") == "generated":
                    synonym_generated_count += 1
            tag_relation_count += len(item.relations.tag_on)
            if item.verbatim_check and not item.verbatim_check.get("verbatim_found", True):
                verbatim_miss_count += 1

        summary = {
            "total_candidates": len(items),
            "name_occupied_count": name_occupied_count,
            "synonym_manual_count": synonym_manual_count,
            "synonym_generated_count": synonym_generated_count,
            "tag_relation_count": tag_relation_count,
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
                    "name_occupied_count": 0,
                    "synonym_manual_count": 0,
                    "synonym_generated_count": 0,
                    "tag_relation_count": 0,
                    "verbatim_miss_count": 0,
                },
            )

        text = p_path.read_text(encoding="utf-8", errors="replace")
        parsed = parse_proposal_candidates(text)

        inputs_status = self.check_inputs_status(p_path)
        scan_completeness = self.scan_completeness()

        items: list[CandidateCheckItem] = []
        name_occupied_count = 0
        synonym_manual_count = 0
        synonym_generated_count = 0
        tag_relation_count = 0
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
            if item.relations.name_occupied is not None:
                name_occupied_count += 1
            for syn in item.relations.synonyms:
                if syn.get("kind") == "manual":
                    synonym_manual_count += 1
                elif syn.get("kind") == "generated":
                    synonym_generated_count += 1
            tag_relation_count += len(item.relations.tag_on)
            if item.verbatim_check and not item.verbatim_check.get("verbatim_found", True):
                verbatim_miss_count += 1

        summary = {
            "total_candidates": len(items),
            "name_occupied_count": name_occupied_count,
            "synonym_manual_count": synonym_manual_count,
            "synonym_generated_count": synonym_generated_count,
            "tag_relation_count": tag_relation_count,
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
