# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| older | No |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/01NGVI-QINGYI/ARIA-X/security/advisories/new>

Include, where possible:

- the affected version or commit;
- a minimal reproduction (config shape with secrets removed, command, observed result);
- the impact you believe is possible;
- any suggested fix or mitigation.

You will receive an acknowledgement through the advisory thread. Please allow time for a fix
before public disclosure; coordinated disclosure is appreciated.

## Scope

This policy covers the ARIA-X codebase and its documented local operation. It does not authorize
testing any third-party system. See [docs/SECURITY.md](docs/SECURITY.md) for the technical threat
model and the README's licence/disclaimer section for the project's usage boundaries.

## Hardening checklist for operators

- Bind the console to `127.0.0.1`; use a reverse proxy with TLS if remote access is unavoidable.
- Set a long random `ARIA_X_AUTH_TOKEN` whenever the console is reachable by another process.
- Keep `safety.approval = "ask"` for high-risk tools; review the approval queue.
- Keep `sandbox.backend = "docker"` and `sandbox.network = "none"` for isolated experiments.
- Store API keys in environment variables, never in the repository.
