"""Deterministic flow distillation for the Maestro EvolutionLoop baseline.

The loop is intentionally dependency-free and conservative:

1. read append-only ``flow_history.jsonl`` records
2. cluster repeated step n-grams
3. render a reviewable ``SKILL.md`` draft
4. write it only when the caller explicitly passes ``--apply``
5. optionally write a non-destructive update proposal under
   ``.agents/skills/.proposals/``

LLM-backed analysers can replace :func:`analyse_flows` later; the storage and
review boundary stays the same.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "FlowCluster",
    "FlowRecord",
    "analyse_flows",
    "append_learned",
    "apply_cluster",
    "load_history",
    "propose_cluster",
    "render_proposal",
    "render_skill",
    "slugify",
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"[0-9A-Za-z_\u3040-\u30ff\u4e00-\u9fff]+")


@dataclass(frozen=True)
class FlowRecord:
    """One observed task flow."""

    task: str
    steps: tuple[str, ...]
    success: bool = True
    timestamp: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlowRecord":
        task = raw.get("task")
        steps = raw.get("steps")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("flow record needs a non-empty task")
        if not isinstance(steps, list) or not steps:
            raise ValueError("flow record needs a non-empty steps list")
        clean_steps = tuple(_step_text(step) for step in steps)
        if not all(clean_steps):
            raise ValueError("every flow step must be a string or object")
        outcome = str(raw.get("outcome") or raw.get("status") or "success")
        success = outcome.casefold() in {
            "ok", "success", "succeeded", "passed", "complete", "completed",
        }
        return cls(
            task=task.strip(),
            steps=clean_steps,
            success=success,
            timestamp=str(raw.get("timestamp") or ""),
        )


@dataclass(frozen=True)
class FlowCluster:
    """A repeated step window with its observed outcomes."""

    steps: tuple[str, ...]
    support: int
    successes: int
    tasks: tuple[str, ...]
    slug_suffix: int = 0

    @property
    def success_rate(self) -> float:
        return self.successes / self.support if self.support else 0.0

    @property
    def slug(self) -> str:
        base = slugify(self.tasks[0] if self.tasks else self.steps[0])
        return _slug_with_suffix(base, self.slug_suffix)

    def as_dict(self) -> dict[str, Any]:
        return {
            "steps": list(self.steps),
            "support": self.support,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 6),
            "tasks": list(self.tasks),
            "slug": self.slug,
        }


def load_history(path: str | Path) -> list[FlowRecord]:
    """Load JSONL flow records, rejecting malformed entries with line numbers."""
    source = Path(path)
    records: list[FlowRecord] = []
    for number, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{source}:{number}: record must be an object")
        try:
            records.append(FlowRecord.from_dict(raw))
        except ValueError as exc:
            raise ValueError(f"{source}:{number}: {exc}") from exc
    return records


def analyse_flows(records: Iterable[FlowRecord], *, min_support: int = 2,
                  ngram: int = 3, max_clusters: int = 20) -> list[FlowCluster]:
    """Cluster repeated step windows, ranked by support and success rate.

    Clusters that would otherwise share a task slug receive deterministic
    ``-2``, ``-3`` suffixes so that apply/propose never silently overwrite one
    another.
    """
    if min_support < 1:
        raise ValueError("min_support must be >= 1")
    if ngram < 1:
        raise ValueError("ngram must be >= 1")
    if max_clusters < 1:
        raise ValueError("max_clusters must be >= 1")

    buckets: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        if len(record.steps) < ngram:
            continue
        for start in range(len(record.steps) - ngram + 1):
            key = record.steps[start:start + ngram]
            bucket = buckets.setdefault(
                key, {"support": 0, "successes": 0, "tasks": []})
            bucket["support"] += 1
            if record.success:
                bucket["successes"] += 1
            if record.task not in bucket["tasks"]:
                bucket["tasks"].append(record.task)

    clusters = [
        FlowCluster(steps=key, support=value["support"],
                    successes=value["successes"],
                    tasks=tuple(value["tasks"]))
        for key, value in buckets.items()
        if value["support"] >= min_support
    ]
    clusters.sort(key=lambda cluster: (
        -cluster.support, -cluster.success_rate, -len(cluster.steps),
        cluster.steps))
    unique: list[FlowCluster] = []
    used_slugs: set[str] = set()
    for cluster in clusters[:max_clusters]:
        base = slugify(cluster.tasks[0] if cluster.tasks
                       else cluster.steps[0])
        candidate = base
        suffix = 0
        while candidate in used_slugs:
            suffix += 1
            candidate = _slug_with_suffix(base, suffix)
        used_slugs.add(candidate)
        if suffix:
            cluster = replace(cluster, slug_suffix=suffix)
        unique.append(cluster)
    return unique


def render_skill(cluster: FlowCluster, *, name_prefix: str = "Flow") -> str:
    """Render a reviewable skill draft from one cluster."""
    title = _title(cluster)
    triggers = _triggers(cluster)
    lines = [
        "---",
        f"name: {_yaml_scalar(f'{name_prefix}: {title}')}",
        "description: " + _yaml_scalar(
            f"Auto-distilled from {cluster.support} observed flow(s) "
            f"with a {cluster.success_rate:.0%} success rate; review before use."),
        "trigger_conditions:",
    ]
    lines.extend(f"  - {_yaml_scalar(trigger)}" for trigger in triggers)
    lines.extend([
        "---",
        "",
        f"# {name_prefix}: {title}",
        "",
        f"Observed {cluster.successes}/{cluster.support} successful runs.",
        "This is a deterministic draft. Verify every step against the current",
        "tools and repository state before adopting it.",
        "",
        "## Source tasks",
        "",
    ])
    lines.extend(f"- {task}" for task in cluster.tasks[:8])
    lines.extend([
        "",
        "## Steps",
        "",
    ])
    lines.extend(f"{index}. `{step}`" for index, step in enumerate(
        cluster.steps, 1))
    lines.extend([
        "",
        "## Verification",
        "",
        "- Re-run the flow on a small fixture.",
        "- Run `okf validate <wiki>` and `okf index <wiki> --check` after any",
        "  knowledge mutation.",
        "- Remove or rewrite this draft if the observed steps are stale.",
        "",
    ])
    return "\n".join(lines)


def apply_cluster(root: str | Path, cluster: FlowCluster, *,
                  force: bool = False) -> dict[str, Any]:
    """Write a generated skill without silently overwriting an existing one."""
    target = (Path(root) / ".agents" / "skills" / cluster.slug
              / "SKILL.md")
    if target.exists() and not force:
        return {"written": False, "path": str(target),
                "reason": "skill already exists"}
    _atomic_write(target, render_skill(cluster))
    return {"written": True, "path": str(target), "reason": "created"}


def render_proposal(cluster: FlowCluster,
                    existing_skill: str | None = None) -> str:
    """Render a non-destructive update proposal for human review.

    The proposal is deliberately separate from ``SKILL.md``.  It records the
    observed flow, the steps already present in an existing skill, and a
    checklist so an operator can accept or reject the update manually.
    """
    title = _title(cluster)
    existing_steps = _extract_existing_steps(existing_skill or "")
    observed_steps = [f"{index}. `{step}`" for index, step in enumerate(
        cluster.steps, 1)]
    existing_values = [_step_value(step) for step in existing_steps]
    observed_values = [_step_value(step) for step in cluster.steps]
    added = _unique(step for step in observed_values
                    if step not in existing_values)
    removed = _unique(step for step in existing_values
                      if step not in observed_values)
    unchanged = _unique(step for step in observed_values
                        if step in existing_values)
    lines = [
        f"# Skill update proposal: {title}",
        "",
        "> Generated by `okf evolve --propose`. Review before copying into",
        "> `SKILL.md`. Re-running without `--force` preserves this file.",
        "",
        f"- Target: `.agents/skills/{cluster.slug}/SKILL.md`",
        f"- Existing skill: {'yes' if existing_skill else 'no'}",
        f"- Observed: {cluster.successes}/{cluster.support} successful runs",
        f"- Source tasks: {', '.join(cluster.tasks[:8]) or '(none)'}",
        "",
        "## Observed steps",
        "",
        *observed_steps,
        "",
        "## Existing steps",
        "",
    ]
    if existing_steps:
        lines.extend(existing_steps)
    elif existing_skill:
        lines.append("- (existing skill has no `## Steps` section)")
    else:
        lines.append("- (no existing skill steps)")
    lines.extend([
        "",
        "## Step diff",
        "",
        "- Added:",
        *_diff_lines(added),
        "- Removed:",
        *_diff_lines(removed),
        "- Unchanged:",
        *_diff_lines(unchanged),
        "",
        "## Review checklist",
        "",
        "- [ ] Each observed step still matches the current tools.",
        "- [ ] Existing steps were not removed unless they are stale.",
        "- [ ] The change is small enough to review in one pass.",
        "- [ ] `okf validate <wiki>` and `okf index <wiki> --check` pass.",
        "- [ ] The proposal is copied into `SKILL.md` only after approval.",
    ])
    if not existing_skill:
        lines.extend([
            "",
            "No existing skill was found.  Use `okf evolve <wiki> --apply`",
            "after reviewing the candidate draft below.",
        ])
    lines.extend([
        "",
        "## Candidate draft",
        "",
        "~~~~markdown",
        render_skill(cluster),
        "~~~~",
    ])
    return "\n".join(lines)


def propose_cluster(root: str | Path, cluster: FlowCluster, *,
                    force: bool = False) -> dict[str, Any]:
    """Write a reviewable proposal without modifying the live skill.

    Proposals live under ``.agents/skills/.proposals/``.  That directory has
    no ``SKILL.md`` and is therefore ignored by skill discovery.  Existing
    proposals are left untouched unless ``force`` is explicit; this preserves
    review notes written by a human.
    """
    skills = Path(root) / ".agents" / "skills"
    skill = skills / cluster.slug / "SKILL.md"
    existing = skill.read_text(encoding="utf-8") if skill.exists() else None
    target = skills / ".proposals" / f"{cluster.slug}.md"
    content = render_proposal(cluster, existing)
    existed = target.exists()
    if existed and not force:
        current = target.read_text(encoding="utf-8")
        reason = ("proposal unchanged" if current == content
                  else "proposal already exists")
        return {
            "written": False,
            "path": str(target),
            "skill_path": str(skill),
            "reason": reason,
        }
    _atomic_write(target, content)
    return {
        "written": True,
        "path": str(target),
        "skill_path": str(skill),
        "reason": "proposal updated" if existed else "proposal created",
    }


def append_learned(root: str | Path, lines: Iterable[str]) -> dict[str, Any]:
    """Append a short lesson below ``<!-- LEARNED -->`` when it exists."""
    agents = Path(root) / ".agents" / "AGENTS.md"
    if not agents.exists():
        return {"written": False, "path": str(agents),
                "reason": "AGENTS.md not found"}
    text = agents.read_text(encoding="utf-8")
    marker = "\n<!-- LEARNED -->"
    if marker not in text:
        return {"written": False, "path": str(agents),
                "reason": "LEARNED sentinel not found"}
    existing = set(text.splitlines())
    additions = [line for line in lines
                 if line.strip() and line not in existing]
    if not additions:
        return {"written": False, "path": str(agents),
                "reason": "no new learned lines"}
    agents.write_text(text.rstrip() + "\n" + "\n".join(additions) + "\n",
                      encoding="utf-8")
    return {"written": True, "path": str(agents), "reason": "appended"}


def slugify(value: str) -> str:
    """Return a stable ASCII skill-directory slug."""
    ascii_slug = _SLUG_RE.sub("-", value.casefold()).strip("-")
    if ascii_slug:
        return ascii_slug[:48].rstrip("-")
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"flow-{digest}"


def _slug_with_suffix(base: str, suffix: int) -> str:
    if suffix <= 0:
        return base
    tail = f"-{suffix}"
    return base[:max(1, 48 - len(tail))].rstrip("-") + tail


def _extract_existing_steps(text: str) -> list[str]:
    """Return the bullet/numbered lines under an existing ``## Steps``."""
    match = re.search(
        r"(?ms)^##\s+Steps\s*$\n(.*?)(?=^##\s+|\Z)", text)
    if not match:
        return []
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip()]


def _step_value(line: str) -> str:
    """Strip list numbering and a single backtick wrapper from a step."""
    value = re.sub(r"^\s*(?:\d+[.)]|[-*+])\s+", "", line.strip(), count=1)
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1]
    return value.strip()


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _diff_lines(values: list[str]) -> list[str]:
    if not values:
        return ["  - (none)"]
    return [f"  - `{value}`" for value in values]


def _atomic_write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def _step_text(step: Any) -> str:
    if isinstance(step, str):
        return " ".join(step.split())
    if isinstance(step, dict):
        tool = (step.get("tool") or step.get("command")
                or step.get("action") or "step")
        args = step.get("args") or step.get("arguments") or {}
        if isinstance(args, dict) and args:
            encoded = json.dumps(args, ensure_ascii=False, sort_keys=True)
            return f"{tool} {encoded}"
        return str(tool).strip()
    return ""


def _title(cluster: FlowCluster) -> str:
    if cluster.tasks:
        first = cluster.tasks[0]
        return first if len(first) <= 72 else first[:69].rstrip() + "..."
    return cluster.steps[0]


def _triggers(cluster: FlowCluster) -> list[str]:
    triggers: list[str] = []
    for task in cluster.tasks:
        if task not in triggers:
            triggers.append(task)
        for word in _WORD_RE.findall(task):
            if len(word) >= 3 and word.casefold() not in {
                    item.casefold() for item in triggers}:
                triggers.append(word)
    if len(triggers) < 2:
        triggers.extend(step for step in cluster.steps
                        if step.casefold() not in {
                            item.casefold() for item in triggers})
    return triggers[:6]


def _yaml_scalar(value: str) -> str:
    # JSON strings are valid YAML scalars and avoid ambiguity for titles that
    # contain a colon, a leading dash, or a comment marker.
    return json.dumps(value, ensure_ascii=False)
