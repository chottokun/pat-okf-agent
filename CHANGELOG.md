# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning.

## [0.2.0] - 2026-11-02

### Added

- OKF v0.2 Markdown knowledge base with progressive-disclosure indexes,
  heading-weighted traversal, Gotchas-filtered context resolution and relative
  Markdown links as the canonical relationship format.
- Pattern B SQLite FTS5 search backend with incremental sync and an
  adjacency-list edge table.
- Opt-in Pattern C adapters for ONNX Runtime, Qdrant and HTTP/Gemma embeddings;
  unavailable optional infrastructure fails closed with `BackendUnavailable`.
- EPO OPS client with cache-first access, cross-process fair-use spacing,
  bounded search results and token caching.
- USPTO Open Data Portal client and bulk-download helpers.
- MCP stdio and HTTP transports with a minimal stateless single-event SSE
  endpoint, bearer authentication, loopback defaults and `/health`.
- Bounded synonym expansion and offline synonym evaluation.
- Deterministic EvolutionLoop clustering, skill drafts, non-destructive
  `--propose` review files and explicit `--force` refresh.
- Release gates: offline smoke test, wheel/sdist content inspection, secret
  boundary checks, a public-release guard and a Python 3.12/3.13/3.14 CI
  matrix.
- Synthetic `examples/wiki` data with no real EPO patent content, plus a
  documented public-release audit.

### Changed

- Minimum supported Python is now **3.12**; Python 3.14 is the recommended
  development and runtime version.
- CI treats `ResourceWarning` as an error and verifies cleanup of SQLite
  connections and HTTP responses.
- The private reuse bundle is explicitly separated from public wheel/sdist
  artifacts; `.secrets/` and `data/external/` are never included in builds.

### Known limitations

- USPTO-to-OKF automatic ingestion is not connected.
- Real Qdrant, Gemma/HTTP embedding and ONNX model deployments are not bundled
  or exercised by CI.
- MCP HTTP does not implement multi-session SSE, OAuth, audit logging or rate
  limiting.
- EvolutionLoop does not perform automatic LLM mutation or skill merging.
