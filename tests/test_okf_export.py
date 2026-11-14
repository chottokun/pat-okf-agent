"""Tests for deterministic offline ``okf export`` (no network)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pat_okf_agent.wiki.cli import main as okf_main
from pat_okf_agent.wiki.export import export_bundle, export_kb
from pat_okf_agent.wiki.verify import snapshot_key_for_url


class TestOkfExport(unittest.TestCase):
    def _write_concept(
        self,
        kb: Path,
        slug: str,
        *,
        title: str,
        sources: list[str],
        body: str = "Body text.\n",
    ) -> None:
        cdir = kb / "concepts"
        cdir.mkdir(parents=True, exist_ok=True)
        src_lines = "\n".join(f'  - "{s}"' for s in sources)
        content = (
            "---\n"
            "type: Concept\n"
            f'title: "{title}"\n'
            'description: "desc"\n'
            f"tags: [{slug}]\n"
            "scope: taxonomy\n"
            "basis: external\n"
            "sources:\n"
            f"{src_lines}\n"
            'verified_at: "2026-11-14"\n'
            "---\n\n"
            f"# {title}\n\n"
            f"{body}"
        )
        (cdir / f"{slug}.md").write_text(content, encoding="utf-8")

    def test_index_order_and_arxiv_and_missing_cache_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            kb = root / "kb-research"
            self._write_concept(
                kb,
                "z-concept",
                title="First in Index",
                sources=[
                    "preprint:arXiv:2309.06180 issued=2023-09-12 retrieved=2026-11-14 via=arxiv",
                    "paper:10.9999/missing.doi issued=2024-05-01 retrieved=2026-11-14 via=crossref",
                ],
                body="See [Second](a-concept.md) and [Pat](../patents/EP123A1.md).\n",
            )
            self._write_concept(
                kb,
                "a-concept",
                title="Second in Index",
                sources=[
                    "preprint:arXiv:2309.06180 issued=2023-09-12 retrieved=2026-11-14 via=arxiv",
                    "web:https://example.org/paper.pdf retrieved=2026-11-14 via=web genre=paper",
                ],
            )
            self._write_concept(
                kb,
                "m-unlisted",
                title="Unlisted Concept",
                sources=["local-file:notes.txt retrieved=2026-11-14 via=local-file snapshot=local_1234"],
            )
            (kb / "concepts" / "index.md").write_text(
                "# concepts\n\n"
                "- [First in Index](z-concept.md)\n"
                "- [Second in Index](a-concept.md)\n",
                encoding="utf-8",
            )

            # Patent file
            pdir = kb / "patents"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "EP123A1.md").write_text(
                "---\n"
                "type: Patent\n"
                'title: "Sample Patent"\n'
                "publication: EP123A1\n"
                "tags: [thermal]\n"
                "---\n\n# Sample Patent\n",
                encoding="utf-8",
            )

            # arXiv XML cache inside KB
            arxiv_dir = kb / "evidence" / "arxiv"
            arxiv_dir.mkdir(parents=True, exist_ok=True)
            (arxiv_dir / "2309.06180.xml").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">\n'
                "  <entry>\n"
                "    <title>Efficient Memory Management with PagedAttention</title>\n"
                "    <published>2023-09-12T00:00:00Z</published>\n"
                "    <author><name>Woosuk Kwon</name></author>\n"
                "    <author><name>Zhuohan Li</name></author>\n"
                "  </entry>\n"
                "</feed>\n",
                encoding="utf-8",
            )

            # Web PDF snapshot metadata with genre=paper
            snap_key = snapshot_key_for_url("https://example.org/paper.pdf")
            snap_dir = kb / "evidence" / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            (snap_dir / f"{snap_key}.meta.json").write_text(
                json.dumps({
                    "url": "https://example.org/paper.pdf",
                    "title": "Web Hosted Paper PDF",
                    "extracted_publisher": "USENIX",
                    "extracted_issued": "2022-07-11",
                }),
                encoding="utf-8",
            )

            res = export_kb(kb, cache_dirs=[], format_version=2)
            # index.md appearance order, then unlisted alphabetically
            self.assertEqual(
                [c["slug"] for c in res["concepts"]],
                ["z-concept", "a-concept", "m-unlisted"],
            )
            self.assertEqual(res["stats"]["papers"], 3)
            self.assertEqual(res["stats"]["patents"], 1)

            papers_by_id = {p["id"]: p for p in res["papers"]}
            self.assertIn("arXiv:2309.06180", papers_by_id)
            p_arxiv = papers_by_id["arXiv:2309.06180"]
            self.assertTrue(p_arxiv["metadata_cached"])
            self.assertEqual(p_arxiv["title"], "Efficient Memory Management with PagedAttention")
            self.assertEqual(p_arxiv["authors"], ["Woosuk Kwon", "Zhuohan Li"])
            self.assertEqual(p_arxiv["year"], 2023)
            self.assertEqual(p_arxiv["related_concepts"], ["z-concept", "a-concept"])

            p_web = papers_by_id["https://example.org/paper.pdf"]
            self.assertTrue(p_web["metadata_cached"])
            self.assertEqual(p_web["title"], "Web Hosted Paper PDF")
            self.assertEqual(p_web["venue"], "USENIX")
            self.assertEqual(p_web["related_concepts"], ["a-concept"])

            p_missing = papers_by_id["10.9999/missing.doi"]
            self.assertFalse(p_missing["metadata_cached"])
            self.assertIsNone(p_missing["title"])
            self.assertEqual(p_missing["year"], 2024)
            self.assertEqual(p_missing["url"], "https://doi.org/10.9999/missing.doi")
            self.assertEqual(res["warnings"], ["missing local metadata cache for paper: 10.9999/missing.doi"])

            # Concept-side structured sources & bidirectional IDs
            c_first = res["concepts"][0]
            self.assertEqual(c_first["paper_ids"], ["arXiv:2309.06180", "10.9999/missing.doi"])
            self.assertEqual(c_first["patent_ids"], ["EP123A1"])
            self.assertEqual(c_first["sources"][0]["genre"], "paper")

            c_unlisted = res["concepts"][2]
            self.assertEqual(c_unlisted["sources"][0]["genre"], "other")

            # Format v1 omits papers & structured sources for backwards compatibility
            res_v1 = export_kb(kb, cache_dirs=[], format_version=1)
            self.assertNotIn("papers", res_v1)
            self.assertNotIn("sources", res_v1["concepts"][0])

    def test_crossref_external_cache_and_cli_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            kb1 = root / "kb-a"
            kb2 = root / "kb-b"
            self._write_concept(
                kb1,
                "c1",
                title="Concept 1",
                sources=["paper:10.1016/j.diamond.2014.04.007 via=crossref"],
            )
            self._write_concept(
                kb2,
                "c2",
                title="Concept 2",
                sources=["ops-record:log.md snapshot=s1"],
            )

            ext_cache = root / "ext_cache"
            cr_dir = ext_cache / "crossref"
            cr_dir.mkdir(parents=True, exist_ok=True)
            (cr_dir / "10.1016_j.diamond.2014.04.007.json").write_text(
                json.dumps({
                    "message": {
                        "title": ["Diamond Composite Thermal Properties"],
                        "author": [{"given": "Jane", "family": "Doe"}],
                        "container-title": ["Diamond and Related Materials"],
                        "published-print": {"date-parts": [[2014, 6, 1]]},
                        "URL": "https://doi.org/10.1016/j.diamond.2014.04.007",
                    }
                }),
                encoding="utf-8",
            )

            out_file = root / "bundle.json"
            rc = okf_main([
                "export",
                str(kb1),
                str(kb2),
                "--label",
                "KB A",
                "--label",
                "KB B",
                "--cache-dir",
                str(ext_cache),
                "--out",
                str(out_file),
            ])
            self.assertEqual(rc, 0)
            bundle = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(bundle["format_version"], 2)
            self.assertEqual([k["label"] for k in bundle["kbs"]], ["KB A", "KB B"])
            self.assertEqual(bundle["kbs"][0]["papers"][0]["title"], "Diamond Composite Thermal Properties")
            self.assertEqual(bundle["kbs"][0]["papers"][0]["authors"], ["Jane Doe"])
            self.assertEqual(bundle["kbs"][0]["papers"][0]["year"], 2014)
            self.assertEqual(bundle["warnings"], [])

            # Determinism check
            b1 = export_bundle([(kb1, "KB A")], cache_dirs=[ext_cache], generated_at="2026-11-14T15:00:00+09:00")
            b2 = export_bundle([(kb1, "KB A")], cache_dirs=[ext_cache], generated_at="2026-11-14T15:00:00+09:00")
            self.assertEqual(b1, b2)

    def test_cached_snapshot_without_title_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            kb = Path(tmpdir) / "kb-snap"
            pdf_url = "https://www.usenix.org/system/files/osdi22-yu.pdf"
            self._write_concept(
                kb,
                "continuous-batching",
                title="Continuous Batching",
                sources=[f"web:{pdf_url} retrieved=2026-11-13 via=web genre=paper"],
            )
            snap_key = snapshot_key_for_url(pdf_url)
            snap_dir = kb / "evidence" / "snapshots"
            snap_dir.mkdir(parents=True, exist_ok=True)
            (snap_dir / f"{snap_key}.meta.json").write_text(
                json.dumps({"url": pdf_url, "final_url": pdf_url}),
                encoding="utf-8",
            )

            res = export_kb(kb, cache_dirs=[], format_version=2)
            self.assertEqual(res["stats"]["papers"], 1)
            paper = res["papers"][0]
            self.assertTrue(paper["metadata_cached"])
            self.assertIsNone(paper["title"])
            self.assertEqual(
                res["warnings"],
                [f"missing bibliographic title in local cache for paper: {pdf_url}"],
            )


if __name__ == "__main__":
    unittest.main()
