"""OKF v0.2 Markdown knowledge base (Pattern A/B, optional Pattern C)."""
from __future__ import annotations

from .context import ContextBundle, ContextResolver
from .embeddings import GemmaEmbedder, HttpEmbedder
from .evolution import (
    FlowCluster,
    FlowRecord,
    analyse_flows,
    apply_cluster,
    load_history,
    propose_cluster,
    render_proposal,
    render_skill,
)
from .frontmatter import FrontmatterError, dump_document, parse_frontmatter
from .graph import Edge, TraversalHit, WikiGraph, build_graph
from .index import plan_indexes, stale_indexes, write_indexes
from .model import (
    SCOPE_WEIGHTS,
    Concept,
    Link,
    Section,
    extract_links,
    extract_sections,
    find_wikilinks,
    iter_concepts,
    load_concept,
    scope_from_heading,
    scope_of,
    validate_meta,
    write_concept,
)
from .onnx_backend import MicroOnnxBackend
from .qdrant_backend import QdrantBackend
from .sqlite_backend import SQLiteSearchBackend
from .search import (
    BackendUnavailable,
    LocalSearchBackend,
    SearchHit,
    get_backend,
    pattern_for,
)
from .synonyms import (
    ExpandedSearchBackend,
    SynonymIndex,
    evaluate_synonyms,
    load_eval_queries,
)
from .validate import ValidationReport, validate_wiki

__all__ = [
    "BackendUnavailable",
    "Concept",
    "ContextBundle",
    "ContextResolver",
    "Edge",
    "ExpandedSearchBackend",
    "FlowCluster",
    "FlowRecord",
    "FrontmatterError",
    "GemmaEmbedder",
    "HttpEmbedder",
    "Link",
    "LocalSearchBackend",
    "MicroOnnxBackend",
    "QdrantBackend",
    "SCOPE_WEIGHTS",
    "SQLiteSearchBackend",
    "SearchHit",
    "SynonymIndex",
    "Section",
    "TraversalHit",
    "ValidationReport",
    "WikiGraph",
    "analyse_flows",
    "apply_cluster",
    "build_graph",
    "dump_document",
    "extract_links",
    "evaluate_synonyms",
    "extract_sections",
    "find_wikilinks",
    "get_backend",
    "iter_concepts",
    "load_concept",
    "load_eval_queries",
    "load_history",
    "parse_frontmatter",
    "pattern_for",
    "plan_indexes",
    "propose_cluster",
    "render_proposal",
    "render_skill",
    "scope_from_heading",
    "scope_of",
    "stale_indexes",
    "validate_meta",
    "validate_wiki",
    "write_concept",
    "write_indexes",
]
