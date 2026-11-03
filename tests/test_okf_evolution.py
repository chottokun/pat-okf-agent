"""Tests for deterministic flow clustering and skill distillation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.evolution import (
    FlowRecord,
    analyse_flows,
    append_learned,
    apply_cluster,
    load_history,
    propose_cluster,
    render_proposal,
    render_skill,
    slugify,
)
from pat_okf_agent.wiki.frontmatter import parse_frontmatter


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def history(self) -> list[FlowRecord]:
        return [
            FlowRecord("ingest patent EP1", ("epo check", "okf ingest-epo",
                                             "okf validate"), True),
            FlowRecord("ingest patent EP2", ("epo check", "okf ingest-epo",
                                             "okf validate"), True),
            FlowRecord("broken import", ("epo check", "okf ingest-epo",
                                         "okf validate"), False),
        ]

    def test_load_history_and_validate_jsonl(self):
        path = write(self.root, "flows.jsonl", "\n".join([
            json.dumps({"task": "a", "steps": ["one", {"tool": "two",
                                                       "args": {"x": 1}}]}),
            json.dumps({"task": "b", "steps": ["one"], "status": "failed"}),
        ]) + "\n")
        records = load_history(path)
        self.assertEqual(len(records), 2)
        self.assertIn('two {"x": 1}', records[0].steps[1])
        self.assertFalse(records[1].success)
        write(self.root, "bad.jsonl", "{nope}\n")
        with self.assertRaises(ValueError):
            load_history(self.root / "bad.jsonl")

    def test_analyse_clusters_and_renders_valid_skill(self):
        clusters = analyse_flows(self.history(), min_support=2, ngram=2)
        self.assertTrue(clusters)
        cluster = clusters[0]
        self.assertEqual(cluster.support, 3)
        self.assertAlmostEqual(cluster.success_rate, 2 / 3)
        draft = render_skill(cluster)
        meta, body = parse_frontmatter(draft)
        self.assertTrue(meta["name"].startswith("Flow:"))
        self.assertIsInstance(meta["trigger_conditions"], list)
        self.assertIn("epo check", body)
        self.assertIn("## Verification", body)

    def test_clusters_with_the_same_task_get_unique_slugs(self):
        records = [
            FlowRecord("same task", ("a", "b", "c", "d"), True)
            for _ in range(2)
        ]
        clusters = analyse_flows(records, min_support=2, ngram=2)
        self.assertEqual(len(clusters), 3)
        slugs = [cluster.slug for cluster in clusters]
        self.assertEqual(slugs[0], "same-task")
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue(all(slug.startswith("same-task") for slug in slugs))

    def test_apply_is_non_destructive_unless_forced(self):
        cluster = analyse_flows(self.history(), min_support=2)[0]
        first = apply_cluster(self.root, cluster)
        self.assertTrue(first["written"])
        path = Path(first["path"])
        self.assertTrue(path.exists())
        path.write_text("user-authored\n", encoding="utf-8")
        second = apply_cluster(self.root, cluster)
        self.assertFalse(second["written"])
        self.assertEqual(path.read_text("utf-8"), "user-authored\n")
        forced = apply_cluster(self.root, cluster, force=True)
        self.assertTrue(forced["written"])
        self.assertIn("Auto-distilled", path.read_text("utf-8"))

    def test_proposal_is_reviewable_and_does_not_change_existing_skill(self):
        cluster = analyse_flows(self.history(), min_support=2)[0]
        skill = write(
            self.root, f".agents/skills/{cluster.slug}/SKILL.md",
            "# Existing\n\n## Steps\n\n1. `keep me`\n")
        original = skill.read_text("utf-8")
        result = propose_cluster(self.root, cluster)
        self.assertTrue(result["written"])
        proposal = Path(result["path"])
        self.assertEqual(proposal.parent.name, ".proposals")
        self.assertEqual(proposal.name, f"{cluster.slug}.md")
        self.assertEqual(skill.read_text("utf-8"), original)
        self.assertEqual(result["skill_path"], str(skill))
        text = proposal.read_text("utf-8")
        self.assertIn("## Observed steps", text)
        self.assertIn("1. `keep me`", text)
        self.assertIn("## Step diff", text)
        self.assertIn("- Added:", text)
        self.assertIn("`epo check`", text)
        self.assertIn("- Removed:", text)
        self.assertIn("`keep me`", text)
        self.assertIn("## Review checklist", text)
        self.assertIn("## Candidate draft", text)
        self.assertIn("epo check", text)

    def test_existing_proposals_are_preserved_unless_forced(self):
        cluster = analyse_flows(self.history(), min_support=2)[0]
        first = propose_cluster(self.root, cluster)
        self.assertTrue(first["written"])
        proposal = Path(first["path"])
        unchanged = propose_cluster(self.root, cluster)
        self.assertFalse(unchanged["written"])
        self.assertEqual(unchanged["reason"], "proposal unchanged")
        proposal.write_text("review notes\n", encoding="utf-8")
        blocked = propose_cluster(self.root, cluster)
        self.assertFalse(blocked["written"])
        self.assertEqual(blocked["reason"], "proposal already exists")
        self.assertEqual(proposal.read_text("utf-8"), "review notes\n")
        forced = propose_cluster(self.root, cluster, force=True)
        self.assertTrue(forced["written"])
        self.assertEqual(forced["reason"], "proposal updated")
        self.assertIn("## Review checklist", proposal.read_text("utf-8"))

    def test_proposal_without_an_existing_skill_points_to_apply(self):
        cluster = analyse_flows(self.history(), min_support=2)[0]
        text = render_proposal(cluster)
        self.assertIn("Existing skill: no", text)
        self.assertIn("No existing skill was found", text)
        self.assertIn("## Candidate draft", text)
        self.assertIn(str(cluster.slug), text)

    def test_append_learned_preserves_existing_tail(self):
        agents = write(
            self.root, ".agents/AGENTS.md",
            "# Guide\n\n<!-- LEARNED -->\n<!-- placeholder -->\n- old\n")
        result = append_learned(self.root, ["- new lesson"])
        self.assertTrue(result["written"])
        text = agents.read_text("utf-8")
        self.assertIn("- old", text)
        self.assertIn("- new lesson", text)
        self.assertLess(text.index("- old"), text.index("- new lesson"))
        self.assertFalse(append_learned(self.root, [])["written"])

    def test_slugify_handles_japanese_and_ascii(self):
        self.assertEqual(slugify("Ingest EPO patents"), "ingest-epo-patents")
        self.assertTrue(slugify("特許を取り込む").startswith("flow-"))


if __name__ == "__main__":
    unittest.main()
