# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `0.1.x` | Yes |

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately through **GitHub Security Advisories** on this repository (`Security` -> `Report a vulnerability`). Please include:

- A clear description of the issue and affected component(s).
- Minimal reproduction steps or a proof-of-concept script.
- The `legisbr` version and Python version you tested against.

We aim to acknowledge reports within **5 business days** and coordinate disclosure once a fix is ready.

## TLS & Network Security Policy

- `legisbr.core.http.HttpClient` enables TLS certificate verification by default (`verify_ssl=True`) and uses Python's default secure OpenSSL cipher configuration.
- `DEFAULT@SECLEVEL=1` and disabled certificate verification are **never** enabled globally.
- A small number of official Brazilian government portals ship broken certificate chains or legacy TLS parameters; those sources are explicitly flagged with `insecure_tls=True` on their `ScraperSpec` after live verification. Callers can override `verify_ssl` explicitly on `Client(...)` or `HttpClient(...)`.

## Secrets, Proxies & Packaging Hygiene

- Library code never auto-loads `.env` files or mutates `os.environ` at import time.
- Source distributions (`sdist`) explicitly exclude `.env*`, `proxies*.txt`, `logs/`, and `outputs/`, verified automatically in CI and release workflows.
