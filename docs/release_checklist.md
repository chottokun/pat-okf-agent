# Release checklist (OKF v0.2)

This checklist is for the current repository scope: EPO/USPTO clients plus the
OKF Markdown knowledge base, optional Pattern C infrastructure, MCP servers and
EvolutionLoop.

## Definition of done

A release candidate is ready only when all gates below pass.

- [ ] `git status --short` is clean before the release commit.
- [ ] Unit tests pass on Python 3.12, 3.13 and 3.14.
- [ ] The demo wiki validates and its generated indexes are current.
- [ ] Skill discovery validates the repository skills.
- [ ] The release smoke test passes without network access.
- [ ] No tracked secret, token, external-data path or internal absolute path exists.
- [ ] `examples/wiki` contains synthetic data, not raw EPO/OPS data.
- [ ] Package version and `CHANGELOG.md` match the release tag.
- [ ] Public license, author and repository URL are decided.
- [ ] Optional infrastructure still fails closed when unavailable.
- [ ] Documentation names the known limitations below.

## Python support

- Minimum supported version: **3.12** (`requires-python = ">=3.12"`).
- CI matrix: **3.12, 3.13, 3.14**.
- Recommended development/runtime version: **3.14**.
- The free-threaded Python 3.13 build is not a supported target.

## 1. Local preflight

Run from the repository root:

```bash
uv sync --locked
uv run python -W error::ResourceWarning -m unittest discover -s tests -q
uv run okf validate examples/wiki
uv run okf index examples/wiki --check
uv run okf skills . --skills-dir .agents/skills --check --json
uv run python -m compileall -q src tests scripts
uv run python scripts/release_smoke.py
uv run python scripts/check_public_repo.py
uv build --out-dir /tmp/okf-dist
uv run python scripts/check_dist.py /tmp/okf-dist
git diff --check
```

The smoke test uses only a temporary directory. It verifies:

- `okf init`, `validate`, `index --check`, `schema` and `skills`
- EvolutionLoop `--propose` does not create a live skill
- `.agents/skills/.proposals/` is not discovered as a skill
- `--apply` creates a skill and is non-destructive on a second run
- existing proposals require explicit `--force` to refresh

## 2. Secret and data boundary

The following paths must never be tracked:

```bash
git ls-files | grep -E '(^|/)\.secrets/|^data/external/'
uv run python scripts/check_public_repo.py
```

Both commands must succeed. `.gitignore` excludes local credentials, databases,
models and `data/external/`, but the scanner also catches accidental force-adds,
high-confidence token formats, credential-like assignments and internal
absolute paths that point into a developer workspace, root home or user home.
It prints locations only, never the matching value.

Do not run the release smoke test against a production wiki. It writes only
inside `tempfile.TemporaryDirectory`.

### EPO data before a public push

`examples/wiki/` is deliberately **synthetic**. Do not replace it with raw OPS
responses, claims, or real patent documents without checking the EPO OPS terms
and obtaining any required permission. The OPS terms permit use inside products
but restrict making the data “as such” available to the public. Keep API keys,
token caches and raw responses in `.secrets/` or `data/external/`, never in the
repository or a public release artifact.

EPO OPS terms: <https://www.epo.org/en/service-support/ordering/terms-and-conditions/ops-terms-and-conditions>

## 3. CI gate

The GitHub Actions workflow in `.github/workflows/ci.yml` runs:

- unit tests on Python 3.12, 3.13 and 3.14
- `ResourceWarning` treated as an error to catch leaked handles
- demo validation and index freshness
- skill validation
- bytecode compilation
- the offline release smoke test
- wheel/sdist build and forbidden-path checks
- the tracked-secret/external-data and public-release guard

A red CI run blocks the release.

## 4. Package and entry points

Check the installed command surface:

```bash
uv run epo --help
uv run uspto --help
uv run okf --help
uv run okf-mcp --help
```

If building a distributable artifact, run:

```bash
uv build
```

`dist/` is ignored and must not be committed. Verify that the package version
in `pyproject.toml`, `uv.lock`, `CHANGELOG.md` and the release tag match. The
current package version is `0.2.0`.

## 5. First public push and release

Commit and test privately on `main` first. Do **not** push the current `main`
history: it still contains the pre-synthetic example files.

```bash
git add ...
git commit -m "Release 0.2.0"

# Build a clean, single-commit public history from the current tree.
git checkout --orphan public-release
git add -A
git commit -m "Initial public release 0.2.0"
git push origin public-release:main

# Wait for GitHub Actions to pass on 3.12, 3.13 and 3.14.
git tag -a v0.2.0 -m "okf-kb-agent 0.2.0"
git push origin v0.2.0
```

Do not create or push the tag before CI is green. Keep `.secrets/` and
`data/external/` out of the archive. If the history is rewritten instead, use
that rewritten branch in place of `public-release`. See
`docs/public_release_audit.md`.

## 6. Rollback

- Revert the release commit rather than rewriting shared history.
- Restore the previous tag if a downstream consumer already fetched it.
- Re-run the smoke test after rollback.
- Markdown remains the source of truth; derived SQLite/Qdrant stores can be
  rebuilt with `okf sqlite` or `okf qdrant`.

## Known limitations

- USPTO-to-OKF automatic ingestion is not connected. The standalone USPTO
  client remains available.
- Real Qdrant clusters, Gemma/HTTP embedding endpoints and ONNX model files are
  not bundled and are not exercised by CI. Adapters are tested through
  in-process HTTP fixtures and fail closed when unavailable.
- MCP HTTP is a minimal single-event SSE transport. Multi-session SSE, OAuth,
  audit logging and rate limiting are out of scope.
- EvolutionLoop produces deterministic drafts and review proposals. LLM-backed
  SkillMutator and automatic skill merging are out of scope.
- EPO OPS fair-use defaults still apply: 3-second cross-process spacing, a
  25-result search cap and cache-first behavior.
