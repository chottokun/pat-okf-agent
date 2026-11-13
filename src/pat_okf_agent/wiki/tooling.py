"""Stable JSON tool contracts shared by the CLI and MCP server.

The CLI remains the human-facing interface.  This module defines the small,
deterministic tool surface an LLM runtime may call, plus a single dispatcher so
the MCP server does not duplicate CLI logic.
"""
from __future__ import annotations

import copy
import os
from collections import Counter
from pathlib import Path
from typing import Any

from .context import ContextResolver
from .graph import build_graph
from .index import stale_indexes, write_indexes
from .model import iter_concepts
from .search import attach_link_counts, get_backend, pattern_for
from .validate import validate_wiki

__all__ = [
    "TOOL_SPECS",
    "ToolError",
    "dispatch_tool",
    "tool_specs",
    "wiki_stats",
]


class ToolError(RuntimeError):
    """A caller-correctable tool error (bad argument, unknown seed, ...)."""


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "okf_search",
        "description": (
            "Search an OKF wiki and return ranked hits with per-hit score "
            "breakdowns. type/scope/include_admin filter like the okf search "
            "CLI; expand adds synonyms.json aliases as 0.5x auxiliary terms."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki": {"type": "string", "description": "Wiki root directory."},
                "query": {"type": "string", "description": "Search query."},
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 100,
                    "default": 10,
                },
                "type": {
                    "type": "string",
                    "description": (
                        "Filter by note type (e.g. concept, patent); "
                        "comma-separated values allowed, mirroring okf search --type."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Filter by OKF scope (taxonomy, workflow, synergistic, "
                        "gotchas); comma-separated values allowed, mirroring okf search --scope."
                    ),
                },
                "backend": {
                    "type": "string",
                    "enum": ["auto", "local", "sqlite", "qdrant"],
                    "default": "auto",
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional Pattern B SQLite path.",
                },
                "qdrant_url": {
                    "type": "string",
                    "description": "Qdrant REST URL (Pattern C).",
                },
                "collection": {"type": "string"},
                "qdrant_api_key": {"type": "string"},
                "embedding_url": {
                    "type": "string",
                    "description": "OpenAI-compatible or Gemini endpoint.",
                },
                "embedding_model": {"type": "string"},
                "embedding_api_key": {"type": "string"},
                "embedding_style": {
                    "type": "string", "enum": ["openai", "gemini"],
                    "default": "openai",
                },
                "expand": {
                    "type": "boolean",
                    "description": (
                        "Expand the query with <wiki>/synonyms.json aliases as "
                        "inline 0.5x auxiliary terms (same as okf search --expand)."
                    ),
                    "default": False,
                },
                "include_admin": {
                    "type": "boolean",
                    "description": (
                        "Include admin/aggregate notes (BUILDLOG, SUMMARY, index); "
                        "default false mirrors okf search --no-admin."
                    ),
                    "default": False,
                },
            },
            "required": ["wiki", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_context",
        "description": (
            "Resolve a query into token-bounded context with weighted graph "
            "expansion and Gotchas pruning."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki": {"type": "string"},
                "query": {"type": "string"},
                "max_nodes": {
                    "type": "integer", "minimum": 1, "maximum": 50,
                    "default": 5,
                },
                "depth": {"type": "integer", "minimum": 0, "default": 2},
                "token_limit": {
                    "type": "integer", "minimum": 1, "maximum": 100000,
                    "default": 4000,
                },
                "min_relevance": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "default": 0.05,
                },
                "gamma": {
                    "type": "number", "exclusiveMinimum": 0,
                    "maximum": 1, "default": 0.85,
                },
                "backend": {
                    "type": "string",
                    "enum": ["auto", "local", "sqlite", "qdrant"],
                    "default": "auto",
                },
                "db_path": {"type": "string"},
                "qdrant_url": {"type": "string"},
                "collection": {"type": "string"},
                "qdrant_api_key": {"type": "string"},
                "embedding_url": {"type": "string"},
                "embedding_model": {"type": "string"},
                "embedding_api_key": {"type": "string"},
                "embedding_style": {
                    "type": "string", "enum": ["openai", "gemini"],
                    "default": "openai",
                },
                "expand": {
                    "type": "boolean",
                    "description": (
                        "Expand the seed search with <wiki>/synonyms.json "
                        "aliases as inline 0.5x auxiliary terms "
                        "(same as okf context --expand)."
                    ),
                    "default": False,
                },
                "phases": {
                    "type": "boolean",
                    "description": "Return phase1/phase2/phase3 instead of one bundle.",
                    "default": False,
                },
            },
            "required": ["wiki", "query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_traverse",
        "description": "Traverse the weighted link graph from one seed node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki": {"type": "string"},
                "seed": {"type": "string"},
                "depth": {"type": "integer", "minimum": 0, "default": 2},
                "gamma": {
                    "type": "number", "exclusiveMinimum": 0,
                    "maximum": 1, "default": 0.85,
                },
                "direction": {
                    "type": "string", "enum": ["out", "in", "both"],
                    "default": "out",
                },
                "max_nodes": {
                    "type": "integer", "minimum": 1, "maximum": 500,
                    "default": 50,
                },
                "scopes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["taxonomy", "workflow", "synergistic",
                                 "gotchas"],
                    },
                },
                "query": {"type": "string", "default": ""},
                "min_relevance": {
                    "type": "number", "minimum": 0, "maximum": 1,
                    "default": 0.05,
                },
            },
            "required": ["wiki", "seed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_validate",
        "description": "Validate frontmatter, links, orphans and index structure.",
        "inputSchema": {
            "type": "object",
            "properties": {"wiki": {"type": "string"}},
            "required": ["wiki"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_stats",
        "description": "Return concept, link, scope and pattern statistics.",
        "inputSchema": {
            "type": "object",
            "properties": {"wiki": {"type": "string"}},
            "required": ["wiki"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_index",
        "description": (
            "Check or write progressive-disclosure indexes. Markdown remains "
            "the source of truth."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki": {"type": "string"},
                "check": {"type": "boolean", "default": False},
                "max_entries": {
                    "type": "integer", "minimum": 1, "maximum": 1000,
                    "default": 50,
                },
            },
            "required": ["wiki"],
            "additionalProperties": False,
        },
    },
    {
        "name": "okf_ingest_epo",
        "description": (
            "Ingest one EPO publication or a CQL query into an OKF wiki. "
            "Offline mode is the default; pass offline=false only after the EPO "
            "skill has fetched missing data."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wiki": {"type": "string"},
                "publication": {"type": "string"},
                "query": {"type": "string"},
                "max_results": {
                    "type": "integer", "minimum": 1, "maximum": 25,
                    "default": 10,
                },
                "offline": {"type": "boolean", "default": True},
                "include_family": {"type": "boolean", "default": True},
                "include_claims": {"type": "boolean", "default": True},
                "create_stubs": {"type": "boolean", "default": True},
                "credentials": {"type": "string"},
                "cache_dir": {"type": "string"},
            },
            "required": ["wiki"],
            "additionalProperties": False,
        },
    },
]


def tool_specs() -> list[dict[str, Any]]:
    """Return the declared tool contracts."""
    return copy.deepcopy(TOOL_SPECS)


def wiki_stats(root: str | Path) -> dict[str, Any]:
    """Compute the stats payload shared by ``okf stats`` and MCP."""
    concepts = iter_concepts(root)
    graph = build_graph(root)
    types = Counter(concept.type or "(none)" for concept in concepts)
    scopes = Counter(concept.scope for concept in concepts)
    basis_counts = {"external": 0, "model": 0, "mixed": 0, "undeclared": 0}
    for concept in concepts:
        if concept.basis in ("external", "model", "mixed"):
            basis_counts[concept.basis] += 1
        else:
            basis_counts["undeclared"] += 1
    return {
        "concept_count": len(concepts),
        "types": dict(sorted(types.items())),
        "scopes": dict(sorted(scopes.items())),
        "basis": basis_counts,
        "links": sum(len(targets) for targets in graph.edges.values()),
        "broken_links": len(graph.broken),
        "pattern": pattern_for(len(concepts)),
    }


def dispatch_tool(name: str, arguments: dict[str, Any],
                  *, root: str | Path | None = None) -> Any:
    """Run one declared tool and return a JSON-serialisable result."""
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    known = {spec["name"] for spec in TOOL_SPECS}
    if name not in known:
        raise ToolError(f"unknown tool: {name}")
    root_path = _resolve_root(root)
    wiki = _resolve_wiki(arguments.get("wiki", "."), root_path)

    if name == "okf_search":
        query = _required_string(arguments, "query")
        limit = _bounded_int(arguments, "limit", 10, 1, 100)
        doc_types = _string_filters(arguments, "type")
        scopes = _string_filters(arguments, "scope")
        include_admin = _boolean(arguments, "include_admin", False)

        search_kwargs: dict[str, Any] = {"limit": limit}
        if doc_types:
            search_kwargs["doc_type"] = doc_types
        if scopes:
            search_kwargs["scope"] = scopes
        if include_admin:
            search_kwargs["include_admin"] = include_admin

        backend = _tool_backend(arguments, wiki, root=root_path)
        try:
            hits = backend.search(query, **search_kwargs)
        except TypeError:
            hits = backend.search(query, limit=limit)
        hits = attach_link_counts(hits, wiki)
        return [hit.as_dict() for hit in hits]

    if name == "okf_context":
        query = _required_string(arguments, "query")
        resolver = ContextResolver(
            wiki,
            token_limit=_bounded_int(
                arguments, "token_limit", 4000, 1, 100000),
            min_relevance=_bounded_float(
                arguments, "min_relevance", 0.05, 0.0, 1.0),
            damping_factor=_bounded_float(
                arguments, "gamma", 0.85, 0.000001, 1.0),
            backend=_tool_backend(arguments, wiki, root=root_path),
        )
        if _boolean(arguments, "phases", False):
            return resolver.progressive(
                query,
                max_nodes=_bounded_int(arguments, "max_nodes", 5, 1, 50))
        return resolver.resolve(
            query,
            max_nodes=_bounded_int(arguments, "max_nodes", 5, 1, 50),
            depth=_bounded_int(arguments, "depth", 2, 0, 100),
            token_limit=_bounded_int(
                arguments, "token_limit", 4000, 1, 100000),
        ).as_dict()

    if name == "okf_traverse":
        seed = _required_string(arguments, "seed")
        graph = build_graph(wiki)
        scopes = arguments.get("scopes")
        if scopes is not None:
            if not isinstance(scopes, list) or not all(
                    isinstance(scope, str) for scope in scopes):
                raise ToolError("scopes must be an array of strings")
            allowed = {"taxonomy", "workflow", "synergistic", "gotchas"}
            unknown = sorted(set(scopes) - allowed)
            if unknown:
                raise ToolError(
                    f"unknown scope(s): {', '.join(unknown)}")
        query = _optional_string(arguments, "query") or ""
        try:
            hits = graph.traverse(
                [seed],
                depth=_bounded_int(arguments, "depth", 2, 0, 100),
                gamma=_bounded_float(arguments, "gamma", 0.85, 0.000001, 1.0),
                direction=_choice(arguments, "direction",
                                  ("out", "in", "both"), "out"),
                max_nodes=_bounded_int(
                    arguments, "max_nodes", 50, 1, 500),
                scopes=set(scopes) if scopes else None,
                query=query,
                min_relevance=_bounded_float(
                    arguments, "min_relevance", 0.05, 0.0, 1.0),
            )
        except KeyError as exc:
            raise ToolError(f"seed not found: {seed}") from exc
        return [hit.as_dict() for hit in hits]

    if name == "okf_validate":
        return validate_wiki(wiki).as_dict()

    if name == "okf_stats":
        return wiki_stats(wiki)

    if name == "okf_index":
        if _boolean(arguments, "check", False):
            stale = stale_indexes(
                wiki, max_entries=_bounded_int(
                    arguments, "max_entries", 50, 1, 1000))
            return {"ok": not stale, "stale": stale}
        written = write_indexes(
            wiki, max_entries=_bounded_int(
                arguments, "max_entries", 50, 1, 1000))
        return {"ok": True, "written": written, "count": len(written)}

    if name == "okf_ingest_epo":
        return _ingest_epo(wiki, arguments, root=root_path)

    raise ToolError(f"tool is declared but not dispatched: {name}")


def _tool_backend(arguments: dict[str, Any], wiki: Path, *, root: Path):
    backend = _choice(arguments, "backend",
                      ("auto", "local", "sqlite", "qdrant"), "auto")
    db_path = _resolve_optional_path(
        arguments, "db_path", root, kind="file_or_new")
    if backend == "qdrant":
        embedding_url = (_optional_string(arguments, "embedding_url")
                         or os.environ.get("OKF_EMBEDDING_URL"))
        if not embedding_url:
            raise ToolError("embedding_url is required for backend=qdrant")
        from .embeddings import HttpEmbedder
        try:
            embedder = HttpEmbedder(
                embedding_url,
                model=(_optional_string(arguments, "embedding_model")
                       or os.environ.get("OKF_EMBEDDING_MODEL")),
                api_key=(_optional_string(arguments, "embedding_api_key")
                         or os.environ.get("OKF_EMBEDDING_API_KEY")),
                style=_choice(
                    arguments, "embedding_style", ("openai", "gemini"),
                    os.environ.get("OKF_EMBEDDING_STYLE", "openai")),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        base = get_backend(
            "qdrant", wiki, embedder=embedder,
            url=(_optional_string(arguments, "qdrant_url")
                 or os.environ.get("QDRANT_URL", "http://localhost:6333")),
            collection=(_optional_string(arguments, "collection")
                        or os.environ.get("QDRANT_COLLECTION")),
            api_key=(_optional_string(arguments, "qdrant_api_key")
                     or os.environ.get("QDRANT_API_KEY")),
        )
    else:
        base = get_backend(
            backend, wiki, db_path=db_path)
    expand = _boolean(arguments, "expand", False)
    if expand:
        from .synonyms import ExpandedSearchBackend, SynonymIndex
        path = wiki / "synonyms.json"
        if not path.is_file():
            raise ToolError(f"synonyms file not found: {path}")
        base = ExpandedSearchBackend(base, SynonymIndex.from_path(path))
    return base


def _ingest_epo(wiki: Path, arguments: dict[str, Any], *,
                root: Path) -> dict[str, Any]:
    publication = _optional_string(arguments, "publication") or ""
    query = _optional_string(arguments, "query") or ""
    if bool(publication) == bool(query):
        raise ToolError("pass exactly one of publication or query")
    from ..epo import EpsClient, OpsClient
    from .ingest import ingest_publication, ingest_query

    offline = _boolean(arguments, "offline", True)
    cache_dir = _resolve_optional_path(
        arguments, "cache_dir", root, kind="dir_or_new")
    credentials = _resolve_optional_path(
        arguments, "credentials", root, kind="existing_file")
    client = OpsClient(
        credentials_path=credentials,
        cache_dir=cache_dir,
        offline=offline,
    )
    include_claims = _boolean(arguments, "include_claims", True)
    eps_client = (EpsClient(cache_dir=cache_dir, offline=offline)
                  if include_claims else None)
    kwargs = {
        "include_family": _boolean(arguments, "include_family", True),
        "include_claims": include_claims,
        "create_stubs": _boolean(arguments, "create_stubs", True),
        "eps_client": eps_client,
    }
    if query:
        return ingest_query(
            client, query, wiki,
            max_results=_bounded_int(
                arguments, "max_results", 10, 1, 25),
            **kwargs,
        )
    return ingest_publication(client, publication, wiki, **kwargs)


def _resolve_root(root: str | Path | None) -> Path:
    return Path(root).resolve() if root is not None else Path.cwd().resolve()


def _resolve_confined_path(value: Any, root_path: Path, *, key: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} must be a non-empty path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    candidate = candidate.resolve()
    if candidate != root_path and root_path not in candidate.parents:
        raise ToolError(f"{key} escapes the allowed root: {value}")
    return candidate


def _resolve_wiki(value: Any, root_path: Path) -> Path:
    candidate = _resolve_confined_path(value, root_path, key="wiki")
    if not candidate.is_dir():
        raise ToolError(f"wiki directory does not exist: {candidate}")
    return candidate


def _resolve_optional_path(
    arguments: dict[str, Any],
    key: str,
    root_path: Path,
    *,
    kind: str,
) -> Path | None:
    raw = _optional_string(arguments, key)
    if raw is None:
        return None
    candidate = _resolve_confined_path(raw, root_path, key=key)
    if kind == "existing_file":
        if not candidate.is_file():
            raise ToolError(f"{key} file does not exist: {candidate}")
    elif kind == "file_or_new":
        if candidate.exists() and candidate.is_dir():
            raise ToolError(f"{key} must not be a directory: {candidate}")
    elif kind == "dir_or_new":
        if candidate.exists() and not candidate.is_dir():
            raise ToolError(f"{key} must be a directory: {candidate}")
    else:
        raise ValueError(f"unknown path kind: {kind}")
    return candidate


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} must be a non-empty string")
    return value


def _optional_string(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    return value


def _string_filters(arguments: dict[str, Any], key: str) -> list[str]:
    raw = _optional_string(arguments, key)
    if not raw:
        return []
    items: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip().casefold()
        if cleaned:
            items.append(cleaned)
    return items


def _boolean(arguments: dict[str, Any], key: str, default: bool) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be a boolean")
    return value


def _choice(arguments: dict[str, Any], key: str, choices: tuple[str, ...],
            default: str) -> str:
    value = arguments.get(key, default)
    if value not in choices:
        raise ToolError(f"{key} must be one of {', '.join(choices)}")
    return value


def _bounded_int(arguments: dict[str, Any], key: str, default: int,
                 minimum: int, maximum: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ToolError(f"{key} must be between {minimum} and {maximum}")
    return value


def _bounded_float(arguments: dict[str, Any], key: str, default: float,
                   minimum: float, maximum: float) -> float:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{key} must be a number")
    value = float(value)
    if not minimum <= value <= maximum:
        raise ToolError(f"{key} must be between {minimum} and {maximum}")
    return value
