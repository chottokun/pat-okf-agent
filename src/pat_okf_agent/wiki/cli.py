"""``okf`` command line interface for the Markdown knowledge base."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import __version__
from ..epo import EpoError
from .context import ContextResolver
from .evolution import (
    analyse_flows,
    append_learned,
    apply_cluster,
    load_history,
    propose_cluster,
    render_skill,
)
from .frontmatter import FrontmatterError, parse_frontmatter
from .graph import build_graph
from .index import stale_indexes, write_indexes
from .ingest import ingest_publication, ingest_query
from .search import BackendUnavailable, get_backend
from .synonyms import (
    ExpandedSearchBackend,
    SynonymIndex,
    evaluate_synonyms,
    load_eval_queries,
)
from .tooling import TOOL_SPECS, ToolError, wiki_stats
from .validate import validate_wiki
from .verify import SourceVerifier, SourceVerifyError

__all__ = ["main", "SourceVerifier", "SourceVerifyError"]


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except (EpoError, FrontmatterError, BackendUnavailable, ToolError,
            SourceVerifyError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="okf",
        description="OKF v0.2 Markdown knowledge base (A/B, optional C)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create index, log and agent guide")
    p_init.add_argument("dir")
    p_init.add_argument("--skills-dir", action="append", default=None,
                        help="directory containing */SKILL.md to bind "
                             "(repeatable)")
    p_init.add_argument("--json", action="store_true")

    p_index = sub.add_parser("index", help="write progressive-disclosure indexes")
    p_index.add_argument("dir")
    p_index.add_argument("--max-entries", type=int, default=50)
    p_index.add_argument("--check", action="store_true",
                         help="exit 1 if indexes are missing or stale")
    p_index.add_argument("--json", action="store_true")

    p_validate = sub.add_parser("validate", help="validate structure and links")
    p_validate.add_argument("dir")
    p_validate.add_argument(
        "--strict-provenance", action="store_true",
        help="promote provenance self-contradictions (P2, P4, P5) to errors")
    p_validate.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search", help="local full-text search")
    p_search.add_argument("dir")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--backend",
                          choices=("auto", "local", "sqlite", "qdrant"),
                          default="auto")
    p_search.add_argument("--db", default=None,
                          help="SQLite database path (Pattern B)")
    _add_qdrant_options(p_search)
    p_search.add_argument("--expand", action="store_true",
                          help="expand query aliases from synonyms.json")
    p_search.add_argument("--synonyms", default=None,
                          help="alias file (default: <wiki>/synonyms.json)")
    p_search.add_argument("--json", action="store_true")

    p_trav = sub.add_parser("traverse", help="weighted graph traversal")
    p_trav.add_argument("dir")
    p_trav.add_argument("seed")
    p_trav.add_argument("--depth", type=int, default=2)
    p_trav.add_argument("--gamma", type=float, default=0.85)
    p_trav.add_argument("--direction", choices=("out", "in", "both"),
                        default="out")
    p_trav.add_argument("--max-nodes", type=int, default=50)
    p_trav.add_argument("--scope", action="append", default=None,
                        choices=("taxonomy", "workflow", "synergistic",
                                 "gotchas"),
                        help="only expand neighbours of this scope "
                             "(repeatable; seeds are always kept)")
    p_trav.add_argument("--query", default="",
                        help="query text for anchor relevance weighting")
    p_trav.add_argument("--min-relevance", type=float, default=0.05,
                        help="relevance floor for generic anchors (0 = strict)")
    p_trav.add_argument("--json", action="store_true")

    p_context = sub.add_parser(
        "context", help="resolve a query into token-bounded context")
    p_context.add_argument("dir")
    p_context.add_argument("query")
    p_context.add_argument("--max-nodes", type=int, default=5)
    p_context.add_argument("--depth", type=int, default=2)
    p_context.add_argument("--gamma", type=float, default=0.85)
    p_context.add_argument("--token-limit", type=int, default=4000)
    p_context.add_argument("--min-relevance", type=float, default=0.05)
    p_context.add_argument("--backend",
                           choices=("auto", "local", "sqlite", "qdrant"),
                           default="auto")
    p_context.add_argument("--db", default=None,
                           help="SQLite database path (Pattern B)")
    _add_qdrant_options(p_context)
    p_context.add_argument("--expand", action="store_true",
                           help="expand query aliases from synonyms.json")
    p_context.add_argument("--synonyms", default=None,
                           help="alias file (default: <wiki>/synonyms.json)")
    p_context.add_argument("--phases", action="store_true",
                           help="emit the three progressive-disclosure phases")
    p_context.add_argument("--json", action="store_true")

    p_sqlite = sub.add_parser(
        "sqlite", help="build/sync the Pattern B SQLite-FTS5 index")
    p_sqlite.add_argument("dir")
    p_sqlite.add_argument("--db", default=None)
    p_sqlite.add_argument("--force", action="store_true")
    p_sqlite.add_argument("--json", action="store_true")

    p_qdrant = sub.add_parser(
        "qdrant", help="sync Markdown embeddings into Qdrant (Pattern C)")
    p_qdrant.add_argument("dir")
    _add_qdrant_options(p_qdrant)
    p_qdrant.add_argument("--force", action="store_true")
    p_qdrant.add_argument("--json", action="store_true")

    p_eval = sub.add_parser(
        "eval-synonyms",
        help="measure synonym-expansion failure rate and Pattern C trigger")
    p_eval.add_argument("dir")
    p_eval.add_argument("--queries", default=None,
                        help="labelled query JSON (default: "
                             "<wiki>/eval_queries.json)")
    p_eval.add_argument("--synonyms", default=None,
                        help="alias file (default: <wiki>/synonyms.json)")
    p_eval.add_argument("--limit", type=int, default=5)
    p_eval.add_argument("--threshold", type=float, default=0.15)
    p_eval.add_argument("--backend",
                        choices=("auto", "local", "sqlite", "qdrant"),
                        default="local")
    p_eval.add_argument("--db", default=None)
    _add_qdrant_options(p_eval)
    p_eval.add_argument("--check", action="store_true",
                        help="exit 1 when the failure rate exceeds threshold")
    p_eval.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="counts and pattern recommendation")
    p_stats.add_argument("dir")
    p_stats.add_argument("--json", action="store_true")

    p_skills = sub.add_parser(
        "skills", help="discover and validate SKILL.md files")
    p_skills.add_argument("dir", nargs="?", default=".")
    p_skills.add_argument("--skills-dir", action="append", default=None,
                          help="skill directory (repeatable; skips discovery)")
    p_skills.add_argument("--check", action="store_true",
                          help="exit 1 when required metadata is missing")
    p_skills.add_argument("--json", action="store_true")

    p_schema = sub.add_parser(
        "schema", help="print the stable JSON tool contracts (MCP compatible)")
    p_schema.add_argument("--json", action="store_true",
                          help="emit JSON (the default; kept for symmetry)")

    p_evolve = sub.add_parser(
        "evolve", help="distill repeated flow_history.jsonl patterns")
    p_evolve.add_argument("dir")
    p_evolve.add_argument("--history", default=None,
                          help="JSONL path (default: <wiki>/.agents/"
                               "flow_history.jsonl)")
    p_evolve.add_argument("--min-support", type=int, default=2)
    p_evolve.add_argument("--ngram", type=int, default=3)
    p_evolve.add_argument("--max-clusters", type=int, default=20)
    p_evolve.add_argument("--dry-run", action="store_true",
                          help="explicitly do not write skills (the default)")
    p_evolve.add_argument("--apply", action="store_true",
                          help="write generated skills (default: dry run)")
    p_evolve.add_argument("--propose", action="store_true",
                          help="write review proposals without modifying "
                               "existing skills")
    p_evolve.add_argument("--force", action="store_true",
                          help="overwrite an existing generated skill or "
                               "proposal (requires --apply or --propose)")
    p_evolve.add_argument("--learn", action="store_true",
                          help="append a summary below <!-- LEARNED --> "
                               "(requires --apply)")
    p_evolve.add_argument("--json", action="store_true")

    p_ingest = sub.add_parser("ingest-epo",
                              help="ingest EPO publications into the wiki")
    p_ingest.add_argument("publication", nargs="?")
    p_ingest.add_argument("--query", default=None)
    p_ingest.add_argument("--wiki", required=True)
    p_ingest.add_argument("--max-results", type=int, default=10)
    p_ingest.add_argument("--no-family", action="store_true")
    p_ingest.add_argument("--no-claims", action="store_true")
    p_ingest.add_argument("--no-stubs", action="store_true")
    p_ingest.add_argument("--offline", action="store_true")
    p_ingest.add_argument("--credentials", default=None)
    p_ingest.add_argument("--cache-dir", default=None)
    p_ingest.add_argument("--json", action="store_true")

    p_verify = sub.add_parser(
        "verify-source",
        help="verify an external source (DOI, arXiv ID, or Web URL)",
    )
    p_verify.add_argument("target", help="DOI, arXiv ID, Web URL, or <kind>:<id>")
    p_verify.add_argument("--kind", default=None, help="override source kind")
    p_verify.add_argument("--issued", default=None, help="known issue date YYYY-MM-DD")
    p_verify.add_argument("--expect-title", default=None, help="expected title substring")
    p_verify.add_argument("--expect-author", default=None, help="expected author name")
    p_verify.add_argument("--expect-year", type=int, default=None, help="expected year")
    p_verify.add_argument(
        "--check-claim",
        action="append",
        default=None,
        help="string to check in content (repeatable)",
    )
    p_verify.add_argument("--verify-landing", action="store_true", help="verify landing page for DOI")
    p_verify.add_argument("--mailto", default=None, help="contact email for Crossref polite pool")
    p_verify.add_argument("--cache-dir", default=None, help="cache directory path")
    p_verify.add_argument("--sleep", type=float, default=None, help="minimum request spacing in seconds")
    p_verify.add_argument("--no-rate-state", action="store_true", help="disable cross-process rate state lock")
    p_verify.add_argument("--offline", action="store_true", help="offline mode (cache only)")
    p_verify.add_argument("--refresh", action="store_true", help="ignore cache and re-fetch")
    p_verify.add_argument("--json", action="store_true", help="output JSON result")

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        paths = initialize_wiki(Path(args.dir),
                                skills_dir=args.skills_dir)
        if args.json:
            print(json.dumps({label: str(path)
                              for label, path in paths.items()},
                             ensure_ascii=False, indent=2))
        else:
            for label, path in paths.items():
                print(f"{label}: {path}")
        return 0

    if args.command == "index":
        stale = stale_indexes(args.dir, max_entries=args.max_entries)
        if args.check:
            if args.json:
                print(json.dumps({"ok": not stale, "stale": stale},
                                 ensure_ascii=False, indent=2))
            else:
                for rel in stale:
                    print(f"stale: {rel}")
            return 1 if stale else 0
        written = write_indexes(args.dir, max_entries=args.max_entries)
        if args.json:
            print(json.dumps({"ok": True, "written": written,
                              "count": len(written)},
                             ensure_ascii=False, indent=2))
        else:
            print(f"wrote {len(written)} index page(s)")
        return 0

    if args.command == "validate":
        report = validate_wiki(args.dir, strict_provenance=args.strict_provenance)
        if args.json:
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(f"{'OK' if report.ok else 'FAILED'}: "
                  f"{report.concept_count} concept(s)")
            for warning in report.warnings:
                print(f"WARN: {warning}")
            for error in report.errors:
                print(f"ERROR: {error}")
        return 0 if report.ok else 1

    if args.command == "search":
        backend = _backend_from_args(args, args.dir)
        hits = backend.search(args.query, limit=args.limit)
        if args.json:
            print(json.dumps([hit.as_dict() for hit in hits],
                             ensure_ascii=False, indent=2))
        elif not hits:
            print("no matches")
        else:
            for hit in hits:
                print(f"{hit.score:6.2f}  {hit.node}  {hit.title}")
                if hit.snippet:
                    print(f"        {hit.snippet}")
        return 0

    if args.command == "traverse":
        graph = build_graph(args.dir)
        try:
            hits = graph.traverse(
                [args.seed], depth=args.depth, gamma=args.gamma,
                direction=args.direction, max_nodes=args.max_nodes,
                scopes=set(args.scope) if args.scope else None,
                query=args.query, min_relevance=args.min_relevance)
        except KeyError:
            print(f"error: seed not found: {args.seed}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps([hit.as_dict() for hit in hits],
                             ensure_ascii=False, indent=2))
        else:
            for number, hit in enumerate(hits, start=1):
                title = graph.nodes[hit.node].title
                print(f"{number}. {hit.node} "
                      f"(score={hit.score:.3f}, depth={hit.depth}) — {title}")
        return 0

    if args.command == "context":
        resolver = ContextResolver(
            args.dir, damping_factor=args.gamma,
            token_limit=args.token_limit,
            min_relevance=args.min_relevance,
            backend=_backend_from_args(args, args.dir))
        if args.phases:
            phases = resolver.progressive(args.query,
                                          max_nodes=args.max_nodes)
            if args.json:
                print(json.dumps(phases, ensure_ascii=False, indent=2))
            else:
                for name in ("phase1", "phase2", "phase3"):
                    phase = phases[name]
                    print(f"== {name} "
                          f"({phase['tokens']}/{phase['token_limit']} tokens)"
                          f" ==")
                    print(phase["text"])
            return 0

        bundle = resolver.resolve(
            args.query, max_nodes=args.max_nodes, depth=args.depth,
            token_limit=args.token_limit)
        if args.json:
            print(json.dumps(bundle.as_dict(), ensure_ascii=False, indent=2))
        else:
            if not bundle.nodes:
                print("no matches")
                return 0
            print(f"seeds: {', '.join(bundle.seeds)}")
            for number, node in enumerate(bundle.nodes, start=1):
                print(f"{number}. {node.node} "
                      f"(score={node.score:.3f}, depth={node.depth}) — "
                      f"{node.title}")
            if bundle.pruned:
                print(f"pruned by Gotchas: {', '.join(bundle.pruned)}")
            print()
            print(bundle.text)
            print(f"\n[{bundle.tokens}/{bundle.token_limit} tokens]")
        return 0

    if args.command == "sqlite":
        from .sqlite_backend import SQLiteSearchBackend
        backend = SQLiteSearchBackend(args.dir, db_path=args.db,
                                      auto_sync=False)
        summary = backend.sync(force=args.force)
        stats = backend.stats()
        if args.json:
            print(json.dumps({**summary, **stats},
                             ensure_ascii=False, indent=2))
        else:
            print(f"indexed: {summary['indexed']}  "
                  f"unchanged: {summary['unchanged']}  "
                  f"removed: {summary['removed']}")
            print(f"db: {stats['db']}  documents: {stats['documents']}  "
                  f"edges: {stats['edges']}")
        return 0

    if args.command == "qdrant":
        backend = _backend_from_args(
            args, args.dir, backend="qdrant", auto_sync=False)
        summary = backend.sync(force=args.force)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"indexed: {summary['indexed']}  "
                  f"unchanged: {summary['unchanged']}  "
                  f"removed: {summary['removed']}  "
                  f"points: {summary.get('points', 0)}  "
                  f"dimensions: {summary['dimensions']}")
        return 0

    if args.command == "eval-synonyms":
        wiki = Path(args.dir)
        queries_path = (Path(args.queries) if args.queries
                        else wiki / "eval_queries.json")
        synonyms_path = (Path(args.synonyms) if args.synonyms
                         else wiki / "synonyms.json")
        queries = load_eval_queries(queries_path)
        index = SynonymIndex.from_path(synonyms_path)
        result = evaluate_synonyms(
            wiki, _backend_from_args(args, wiki), index,
            queries=queries, limit=args.limit,
            failure_threshold=args.threshold)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"queries: {result['total']}  failures: "
                  f"{result['failures']}  rate: "
                  f"{result['failure_rate']:.1%}  "
                  f"(baseline {result['baseline_failure_rate']:.1%})")
            print(f"improved: {result['improved']}  regressed: "
                  f"{result['regressed']}")
            print(f"recommended pattern: {result['recommended_pattern']} "
                  f"({result['pattern_reason']})")
            for row in result["rows"]:
                status = "ok" if row["expanded_ok"] else "FAIL"
                print(f"- {row['id']}: {status}  {row['query']}")
        if args.check and result["failure_rate"] > args.threshold:
            return 1
        return 0

    if args.command == "stats":
        stats = wiki_stats(args.dir)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        else:
            print(f"concepts: {stats['concept_count']}  "
                  f"links: {stats['links']}  "
                  f"broken: {stats['broken_links']}  "
                  f"pattern: {stats['pattern']}")
            for scope, count in stats["scopes"].items():
                print(f"  {scope}: {count}")
            basis = stats["basis"]
            print(f"basis: external={basis['external']}  "
                  f"model={basis['model']}  "
                  f"mixed={basis['mixed']}  "
                  f"undeclared={basis['undeclared']}")
        return 0

    if args.command == "skills":
        specs = discover_skills(Path(args.dir), args.skills_dir)
        missing = [
            spec["path"] for spec in specs
            if spec.get("error") or not spec.get("name")
            or not spec.get("description") or not spec.get("triggers")
        ]
        if args.json:
            print(json.dumps({
                "ok": not missing,
                "skills": specs,
                "missing": missing,
            }, ensure_ascii=False, indent=2))
        else:
            for spec in specs:
                print(f"- {spec['name']}: {spec['description']}")
                print(f"  triggers: {spec['triggers'] or '—'}")
                print(f"  location: {spec['path']}")
                if spec.get("error"):
                    print(f"  ERROR: {spec['error']}")
            if not specs:
                print("no skills discovered")
            for path in missing:
                print(f"WARN: invalid or incomplete skill: {path}")
        return 1 if args.check and missing else 0

    if args.command == "schema":
        print(json.dumps(TOOL_SPECS, ensure_ascii=False, indent=2))
        return 0

    if args.command == "evolve":
        if args.dry_run and args.apply:
            raise ValueError("--dry-run and --apply are mutually exclusive")
        if args.dry_run and args.propose:
            raise ValueError("--dry-run and --propose are mutually exclusive")
        if args.learn and not args.apply:
            raise ValueError("--learn requires --apply")
        if args.force and not (args.apply or args.propose):
            raise ValueError("--force requires --apply or --propose")
        wiki = Path(args.dir)
        history = (Path(args.history) if args.history
                   else wiki / ".agents" / "flow_history.jsonl")
        if not history.exists():
            raise OSError(f"flow history not found: {history}")
        records = load_history(history)
        clusters = analyse_flows(
            records,
            min_support=args.min_support,
            ngram=args.ngram,
            max_clusters=args.max_clusters,
        )
        results = []
        written = []
        proposals = []
        for cluster in clusters:
            item = cluster.as_dict()
            item["path"] = str(
                wiki / ".agents" / "skills" / cluster.slug / "SKILL.md")
            item["draft"] = render_skill(cluster)
            if args.apply:
                result = apply_cluster(wiki, cluster, force=args.force)
                item.update(result)
                if result["written"]:
                    written.append(result["path"])
            if args.propose:
                proposal = propose_cluster(wiki, cluster, force=args.force)
                item["proposal"] = proposal
                if proposal["written"]:
                    proposals.append(proposal["path"])
            results.append(item)
        learned = None
        if args.learn:
            learned = append_learned(wiki, [
                f"- Flow distilled: {item['slug']} "
                f"(support {item['support']}, "
                f"success {item['success_rate']:.0%})"
                for item in results
            ])
        payload = {
            "history": str(history),
            "records": len(records),
            "clusters": results,
            "written": written,
            "proposals": proposals,
            "learned": learned,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"records: {len(records)}  clusters: {len(results)}  "
                  f"written: {len(written)}  proposals: {len(proposals)}")
            for item in results:
                print(f"- {item['slug']}  support={item['support']}  "
                      f"success={item['success_rate']:.0%}  "
                      f"{item['path']}")
                for step in item["steps"]:
                    print(f"    {step}")
            for path in written:
                print(f"wrote: {path}")
            for path in proposals:
                print(f"proposal: {path}")
            for item in results:
                proposal = item.get("proposal")
                if proposal and not proposal["written"]:
                    print(f"proposal: {proposal['path']} "
                          f"(not written: {proposal['reason']})")
            if learned and not learned["written"]:
                print(f"learned: not written ({learned['reason']})")
        return 0

    if args.command == "ingest-epo":
        if bool(args.publication) == bool(args.query):
            print("error: pass either PUBLICATION or --query",
                  file=sys.stderr)
            return 2
        from ..epo import EpsClient, OpsClient

        client = OpsClient(credentials_path=args.credentials,
                           cache_dir=args.cache_dir,
                           offline=args.offline)
        eps_client = (EpsClient(offline=args.offline)
                      if not args.no_claims else None)
        kwargs = {
            "include_family": not args.no_family,
            "include_claims": not args.no_claims,
            "create_stubs": not args.no_stubs,
            "eps_client": eps_client,
        }
        if args.query:
            summary = ingest_query(client, args.query, args.wiki,
                                   max_results=args.max_results, **kwargs)
        else:
            summary = ingest_publication(client, args.publication,
                                         args.wiki, **kwargs)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            if "publications" in summary:
                print(f"ingested {summary['count']} publication(s)")
            else:
                print(f"ingested {summary['publication']} -> "
                      f"{summary['node']}")
            for warning in summary.get("warnings", []):
                print(f"WARN: {warning}")
        return 0

    if args.command == "verify-source":
        verifier = SourceVerifier(
            cache_dir=args.cache_dir,
            offline=args.offline,
            refresh=args.refresh,
            mailto=args.mailto,
            sleep=args.sleep,
            no_rate_state=args.no_rate_state,
        )
        res = verifier.verify(
            args.target,
            kind=args.kind,
            issued=args.issued,
            expect_title=args.expect_title,
            expect_author=args.expect_author,
            expect_year=args.expect_year,
            check_claim=args.check_claim,
            verify_landing=args.verify_landing,
        )
        if args.json:
            print(json.dumps(res.as_dict(), indent=2, ensure_ascii=False))
        else:
            print(f"status: {res.status}")
            print(f"identifier: {res.identifier} ({res.identifier_type})")
            if res.metadata.title:
                print(f"title: {res.metadata.title}")
            if res.metadata.publisher:
                print(f"publisher: {res.metadata.publisher}")
            if res.issued:
                print(f"issued: {res.issued}")
            if res.okf_source:
                print(f"source: {res.okf_source}")
            if res.warnings:
                for w in res.warnings:
                    print(f"warning: {w}")
            if res.error:
                print(f"error: {res.error}")
        return 0 if res.ok else 1

    return 2


# -- backend helpers --------------------------------------------------------

def _add_qdrant_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--qdrant-url", default=os.environ.get(
            "QDRANT_URL", "http://localhost:6333"),
        help="Qdrant REST base URL (default: $QDRANT_URL or localhost:6333)")
    parser.add_argument(
        "--collection", default=os.environ.get("QDRANT_COLLECTION"),
        help="Qdrant collection (default: deterministic okf_<hash>)")
    parser.add_argument(
        "--qdrant-api-key", default=os.environ.get("QDRANT_API_KEY"),
        help="Qdrant api-key header (default: $QDRANT_API_KEY)")
    parser.add_argument(
        "--embedding-url", default=os.environ.get("OKF_EMBEDDING_URL"),
        help="OpenAI-compatible or Gemini embedding endpoint")
    parser.add_argument(
        "--embedding-model", default=os.environ.get("OKF_EMBEDDING_MODEL"),
        help="embedding model name")
    parser.add_argument(
        "--embedding-api-key",
        default=os.environ.get("OKF_EMBEDDING_API_KEY"),
        help="embedding API key (default: $OKF_EMBEDDING_API_KEY)")
    parser.add_argument(
        "--embedding-style", choices=("openai", "gemini"),
        default=os.environ.get("OKF_EMBEDDING_STYLE", "openai"),
        help="embedding request style (default: openai)")


def _backend_from_args(args: argparse.Namespace, root: str | Path,
                       *, backend: str | None = None,
                       auto_sync: bool = True):
    name = backend or args.backend
    if name == "qdrant":
        if not args.embedding_url:
            raise ValueError("--embedding-url is required for Qdrant")
        from .embeddings import HttpEmbedder
        embedder = HttpEmbedder(
            args.embedding_url,
            model=args.embedding_model,
            api_key=args.embedding_api_key,
            style=args.embedding_style,
        )
        base = get_backend(
            "qdrant", root, embedder=embedder, url=args.qdrant_url,
            collection=args.collection, api_key=args.qdrant_api_key,
            auto_sync=auto_sync)
    else:
        base = get_backend(name, root, db_path=getattr(args, "db", None))
    if getattr(args, "expand", False):
        path = (Path(args.synonyms) if getattr(args, "synonyms", None)
                else Path(root) / "synonyms.json")
        base = ExpandedSearchBackend(base, SynonymIndex.from_path(path))
    return base


# -- init / agent guide -----------------------------------------------------

def initialize_wiki(root: Path, *, skills_dir: str | Path | list[str | Path]
                    | None = None) -> dict[str, Path]:
    """Create the minimal OKF structure and a dynamic agent harness."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    index = root / "index.md"
    if not index.exists():
        index.write_text(
            "# Knowledge Index\n\n<!-- generated by okf index -->\n\n"
            "No concepts yet.\n", encoding="utf-8")
    log = root / "log.md"
    if not log.exists():
        log.write_text("# Ingestion Log\n\n", encoding="utf-8")

    skills = root / ".agents/skills"
    skills.mkdir(parents=True, exist_ok=True)
    skill_readme = skills / "README.md"
    if not skill_readme.exists():
        skill_readme.write_text(
            "# Agent skills\n\n"
            "Place each skill in `<name>/SKILL.md` with YAML frontmatter "
            "(`name`, `description`, `trigger_conditions`).\n"
            "Bind a skill only when the current task matches its triggers.\n",
            encoding="utf-8")
    rules = root / ".agents/rules"
    rules.mkdir(parents=True, exist_ok=True)
    task_memory = root / ".agents/task_memory.md"
    if not task_memory.exists():
        task_memory.write_text(
            "# Task memory\n\n"
            "Durable, task-local notes. Keep this file small.\n",
            encoding="utf-8")

    agents = root / ".agents/AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    learned = ""
    if agents.exists():
        old = agents.read_text(encoding="utf-8")
        sentinel = "\n<!-- LEARNED -->"
        if sentinel in old:
            learned = old.split(sentinel, 1)[1]
            placeholder = (
                "<!-- Dynamic runtime additions appended by Maestro "
                "EvolutionLoop will be placed below this line -->")
            learned = learned.replace(placeholder, "", 1).strip("\n")
    content = render_agents_md(
        discover_skills(root, skills_dir), base=root)
    if learned:
        prefix = content.split("\n<!-- LEARNED -->", 1)[0]
        content = (prefix + "\n<!-- LEARNED -->\n"
                   "<!-- Dynamic runtime additions appended by Maestro "
                   "EvolutionLoop will be placed below this line -->\n\n"
                   + learned + "\n")
    agents.write_text(content, encoding="utf-8")
    return {"index": index, "log": log, "agents": agents,
            "skills": skills, "task_memory": task_memory}


def render_agents_md(skill_specs: list[dict[str, str]], *,
                     base: Path | None = None) -> str:
    """Render the minimal, plan-aligned ``.agents/AGENTS.md`` harness.

    When ``base`` is supplied, skill locations are written relative to that
    directory so generated instructions stay portable across machines.
    """
    skill_specs = [spec for spec in skill_specs if not spec.get("error")]
    lines = [
        "---",
        "name: core-agent-harness",
        "version: 0.2.0",
        "description: Minimal orientation and dynamic rule binding harness "
        "for OKF LLM-Wiki operations.",
        "---",
        "",
        "<!-- NO_AUTO_MUTATE -->",
        "",
        "# Core Agent Operating Rules",
        "",
        "## 1. Minimal Orientation & Protocol",
        "",
        "- You are an autonomous knowledge structuring agent operating over an "
        "Open Knowledge Format (OKF v0.2) repository.",
        "- Execute requested tasks while incrementally updating, deduplicating, "
        "and maintaining atomic knowledge nodes.",
        "- Always check the nearest `.agents/task_memory.md` and `index.md` "
        "before modifying the filesystem.",
        "- One topic per Markdown file; YAML frontmatter `type` is required; "
        "express relationships with relative Markdown links only "
        "(`[text](../path/to.md)`, not `[[WikiLink]]`).",
        "- Use `okf search`, `okf traverse`, `okf context`, `okf validate` and "
        "`okf index`; do not hand-edit generated `index.md` files.",
        "",
        "## 2. Progressive Disclosure",
        "",
        "- Phase 1: read only root/category `index.md` maps "
        "(< 1,000 tokens). Do not load concept bodies.",
        "- Phase 2: fetch only candidate frontmatter and heading structure "
        "(< 2,500 tokens).",
        "- Phase 3: load selected concept bodies and direct neighbours "
        "(< 4,000 tokens); discard them when the task completes.",
        "- Prefer `okf context <dir> \"<query>\"` to let the deterministic "
        "resolver assemble a bounded context.",
        "",
        "## 3. Dynamic Skill & Rule Binding",
        "",
        "- DO NOT load all domain rules into your active context at once.",
        "- Scan `.agents/skills/` and `.agents/rules/` (and the bound skill "
        "table below) before initiating complex operations.",
        "- Read and bind a specific `SKILL.md` ONLY when the current task "
        "matches the `trigger_conditions` in its YAML frontmatter.",
        "",
        "## 4. Meta-Skill Protocols",
        "",
        "When consolidating experience or updating knowledge files, invoke the "
        "following meta-skills strictly by reading their specification:",
        "",
        "- `create-skill`: find `create-skill/SKILL.md` under the nearest "
        "`.agents/skills/` and use it to distill a reusable workflow.",
        "- `modify-skill`: find `modify-skill/SKILL.md` under the nearest "
        "`.agents/skills/` and use it to patch an existing skill.",
        "",
        "## 5. Execution & Mutation Constraints",
        "",
        "- Every created or modified OKF concept must keep valid YAML "
        "frontmatter with a mandatory `type` field.",
        "- Do not modify content above the `<!-- LEARNED -->` sentinel in "
        "files marked with `<!-- NO_AUTO_MUTATE -->`.",
        "- Keep generated `index*.md` and Pattern B SQLite files derived; "
        "Markdown remains the source of truth.",
        "",
        "## 6. Bound Skills",
        "",
    ]
    if not skill_specs:
        lines.append(
            "No skills discovered. Add `.agents/skills/<name>/SKILL.md` or pass "
            "`--skills-dir` at init time.")
    else:
        lines.extend([
            "| Skill | Trigger conditions | Description | Location |",
            "| --- | --- | --- | --- |",
        ])
        for spec in skill_specs:
            location = spec["path"]
            if base is not None:
                location = os.path.relpath(
                    Path(location).resolve(), base.resolve())
            lines.append(
                f"| {_md_cell(spec['name'])} | "
                f"{_md_cell(spec.get('triggers') or '—')} | "
                f"{_md_cell(spec['description'])} | "
                f"{_md_cell(location)} |")
    lines.extend([
        "",
        "<!-- LEARNED -->",
        "<!-- Dynamic runtime additions appended by Maestro EvolutionLoop "
        "will be placed below this line -->",
        "",
    ])
    return "\n".join(lines)


def discover_skills(root: Path, explicit: str | Path | list[str | Path] | None
                    ) -> list[dict[str, str]]:
    """Discover ``*/SKILL.md`` files and return display-ready metadata.

    ``explicit`` may be one directory or a list.  Explicit directories are
    searched in order and are not mixed with ancestor discovery.
    """
    candidates: list[Path] = []
    if explicit:
        values = ([explicit] if isinstance(explicit, (str, Path))
                  else list(explicit))
        for value in values:
            candidate = Path(value).expanduser()
            if candidate not in candidates:
                candidates.append(candidate)
    else:
        for base in (root, *root.parents):
            for relative in (".agents/skills", ".opencode/skills"):
                candidate = base / relative
                if candidate.is_dir() and candidate not in candidates:
                    candidates.append(candidate)
            if any(list(candidate.glob("*/SKILL.md"))
                   for candidate in candidates):
                break

    specs: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in candidates:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*/SKILL.md")):
            try:
                meta, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, FrontmatterError) as exc:
                specs.append({
                    "name": path.parent.name,
                    "description": "",
                    "triggers": "",
                    "path": str(path.resolve()),
                    "error": str(exc),
                })
                continue
            errors: list[str] = []
            raw_name = meta.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                name = path.parent.name
                errors.append("missing or invalid name")
            else:
                name = raw_name.strip()
            raw_description = meta.get("description")
            if not isinstance(raw_description, str) or not raw_description.strip():
                description = ""
                errors.append("missing or invalid description")
            else:
                description = " ".join(raw_description.split())
            triggers = meta.get("trigger_conditions")
            if isinstance(triggers, list):
                trigger_text = ", ".join(
                    str(item) for item in triggers if str(item).strip())
                if not trigger_text:
                    errors.append("empty trigger_conditions")
            else:
                trigger_text = str(triggers or "").strip()
                errors.append("trigger_conditions must be a list")
            if name in seen:
                errors.append(f"duplicate skill name: {name}")
            else:
                seen.add(name)
            spec = {"name": name, "description": description,
                    "triggers": trigger_text,
                    "path": str(path.resolve())}
            if errors:
                spec["error"] = "; ".join(errors)
            specs.append(spec)
    return specs


def _md_cell(value: str) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


if __name__ == "__main__":
    raise SystemExit(main())
