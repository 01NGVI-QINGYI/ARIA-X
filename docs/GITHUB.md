# GitHub submission notes

This file collects the short repository metadata and the pre-publish checklist.

## Repository

- Suggested name: `ARIA-X`
- Default branch: `main`
- Visibility: public
- License: AGPL-3.0-or-later

## About (English, ≤160 chars)

```text
Local-first state-space reasoning engine for security research. Fuses ARTEX, Cairn & PentAGI. Python, zero dependencies.
```

## About (中文，简短)

```text
本地优先的安全研究状态空间推理引擎，融合 ARTEX、Cairn 与 PentAGI 的设计。Python，零依赖。
```

Use either line in the GitHub **About** field; the English version is recommended for the public
repository description, the Chinese version can be used for the Chinese README introduction.

## Suggested topics

```text
security-research  state-space-search  ai-agents  python  local-first
artex  cairn  pentagi  llm  agpl
```

## Before the first push

```bash
# from the repository root
bash scripts/verify.sh
git status --short
```

Confirm that:

- `data/`, `runs/`, `*.db`, `.env` and `configs/aria-x.toml` are not staged;
- no API keys, tokens, private hostnames, personal paths or email addresses are tracked;
- the only public identity used in the repository is `01NGVI-QINGYI`;
- `SHA256SUMS.txt` was regenerated after the final edit.

Suggested first-push sequence (replace the remote URL if your username or repository name differs):

```bash
git init
git add .
git commit -m "ARIA-X v0.1.0"
git branch -M main
git remote add origin https://github.com/01NGVI-QINGYI/ARIA-X.git
git push -u origin main
```

## Repository settings to enable

- **Private vulnerability reporting** for the security policy.
- **Dependabot alerts** and **secret scanning** (no runtime dependencies, but GitHub Actions
  and future tooling benefit).
- **Branch protection** on `main`: require the CI check and one review.
- **Issues** and **Discussions** as appropriate; the issue templates are in `.github/ISSUE_TEMPLATE/`.

## Release checklist

1. Update `CHANGELOG.md` and `CITATION.cff` if the version changes.
2. Run `bash scripts/verify.sh` on Python 3.11 and 3.12.
3. Confirm `README.md`, `README.en.md`, `LICENSE`, `NOTICE.md` and `THIRD_PARTY.md` are current.
4. Create a signed tag (`v0.1.0`) and a GitHub Release from it.
5. Attach no build artifacts unless they are reproducible; the project is pure Python and runs
   from source.

> Update the URLs in this file, `CITATION.cff`, `SECURITY.md` and
> `.github/ISSUE_TEMPLATE/config.yml` if the repository owner or name changes.
