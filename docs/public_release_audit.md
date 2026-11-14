# Public-release audit

Audit date: 2026-11-02

Scope: tracked files on `main` and the reachable Git history, plus ignored
local credential/data directories. This document is a record of checks, not a
legal opinion.

## Result

The repository is ready for a **private** push and CI validation. A public
push still requires the owner decisions listed at the end.

- No `.secrets/`, `data/external/`, `.env`, private-key, database or model file
  is tracked.
- No high-confidence token/private-key pattern was found in the current tree or
  in the reachable history.
- `.secrets/` and `data/external/` are gitignored local directories and are
  enforced as forbidden paths by the public release and distribution guards.
- `examples/wiki/` has been replaced with clearly marked synthetic data. It no
  longer contains the real EPO publication content used during development.
- **History policy:** both `main` and `public-release` (plus tag `v0.2.0`) have
  been squashed to a clean single-commit history containing only synthetic
  examples and the `okf_kb_agent` package name.
- Documentation and generated `AGENTS.md` files use repository-relative paths;
  internal absolute paths into a developer or root home directory fail the
  guard.
- `scripts/check_public_repo.py` runs in CI and checks tracked paths, token
  patterns, credential-like assignments and internal absolute paths. It reports
  locations only, never matching values.

## Manual checks performed

```bash
git ls-files | grep -E '(^|/)(\.secrets|data/external|\.env)(/|$)'
git log --all --name-only --pretty=format: \
  | sort -u | grep -E '(^|/)(\.secrets|data/external|\.env)(/|$)'
git grep -I -l -E 'BEGIN .*PRIVATE KEY|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|sk-[A-Za-z0-9]{20,}'
uv run python scripts/check_public_repo.py
```

The history has no commits touching the excluded credential/data paths.

## EPO data

The EPO OPS terms allow use of data inside products and services, but restrict
making the data “as such” available to the public or copying/distributing it as
such. Raw OPS responses, full claims and real patent documents must not be
added to a public repository without a separate legal/product decision.

The demo wiki is synthetic for this reason. API documentation may use real
publication numbers as identifiers, but must not embed the corresponding data.

Terms: <https://www.epo.org/en/service-support/ordering/terms-and-conditions/ops-terms-and-conditions>

## Owner decisions before a public push

- [ ] Choose a license (`pyproject.toml` currently has no `license` field).
- [x] Public repository is
      `https://github.com/chottokun/okf-kb-agent`.
- [x] Replaced private `BUNDLE_README.md` with public `README.md` and excluded
      local `plan/` directory via `.gitignore`.
- [x] Public commit author is `Chottokun
      <Chottokun@users.noreply.github.com>`.
- [x] History strategy: clean single-commit history across `main`,
      `public-release` and `v0.2.0` (no legacy history or real patent corpus).
- [x] Add a public `README.md` if the repository will be presented to users.
- [x] Re-run the public guard and CI after any of the above changes.

## Residual risks

- The secret guard is pattern-based; it cannot prove the absence of a novel
  secret format.
- Optional Qdrant, embedding and ONNX deployments are not exercised by CI.
- USPTO integration remains out of scope.
- GitHub branch protection, signed tags and release attestations require
  repository-owner configuration after the remote is created.
