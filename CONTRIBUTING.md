# Contributing to ARIA-X

Thanks for your interest. ARIA-X is a small, standard-library-only Python project; the best
contributions are focused, tested and easy to review.

## Before you start

- Read the [README](README.md), [architecture notes](docs/ARCHITECTURE.md) and
  [usage boundaries](README.md#许可与免责声明).
- For security issues, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.
- By contributing you agree that your contribution is licensed under AGPL-3.0-or-later.

## Development setup

Requirements: Python 3.11+ (3.12 recommended). There are no third-party runtime dependencies.

```bash
git clone https://github.com/01NGVI-QINGYI/ARIA-X.git
cd ARIA-X
export PYTHONPATH="$PWD/src"
python3.12 -m unittest discover -s tests -v
python3.12 -m aria_x demo --quiet
```

Or run the full gate:

```bash
bash scripts/verify.sh
```

## Code style

- Python standard library only; do not add runtime dependencies without prior discussion.
- `from __future__ import annotations` at the top of every module.
- Type hints on public functions; docstrings explain *why*, not just *what*.
- Keep engine, tool, provider and server boundaries explicit; no hidden global state.
- Truncate large outputs and redact secrets before writing logs, traces or reports.

## Tests

- Add a focused test for every behavior change.
- Prefer `unittest`; no pytest dependency is required.
- For engine changes, cover the budget, cancellation and persistence paths you touched.
- For UI/API changes, exercise the endpoint or add a small integration test.
- Run the suite on Python 3.11 and 3.12 when possible.

## Pull requests

1. Keep the change scoped; separate refactors from behavior changes.
2. Explain the problem and the chosen trade-off in the PR description.
3. Complete the PR checklist, including the privacy check.
4. Update documentation when commands, configuration or public behavior change.
5. Do not include `data/`, `runs/`, `.env`, API keys, tokens, personal paths or private target data.

## Commit messages

Use a short imperative subject, for example:

```text
fix(scope): deny unresolved hostnames by default
docs(readme): add bilingual language switch
```

## Review and merge

Maintainers review for correctness, safety boundaries, test coverage and documentation. A change
may be declined if it expands the project beyond its stated local-research scope or weakens the
scope/approval/sandbox model.
