# openapi-audit

Tools for working with OpenAPI schemas. Requires Python 3.11+.

## openapi-audit

Lint an OpenAPI schema for quality issues.

```bash
# From a file:
uv run openapi-audit schema.json

# From stdin:
curl -s http://localhost:8000/openapi.json | uv run openapi-audit

# Skip specific checks:
uv run openapi-audit schema.json --ignore missing-description --ignore missing-summary

# List available checks:
uv run openapi-audit --list-checks
```

### Configuration

Checks can be configured via a TOML file (`--config FILE`), either as a standalone file or under `[tool.openapi-audit]` in `pyproject.toml`.

**Skip entire checks:**

```toml
[tool.openapi-audit]
ignore-checks = ["missing-parameter-example", "5xx-response"]
```

**Suppress specific issues with regexps** (matched against the formatted issue line):

```toml
[tool.openapi-audit.checks.missing-parameter-example]
ignore-regexps = ["(limit|offset|ordering)"]

[tool.openapi-audit.checks.no-global-security]
ignore-regexps = [".*"]
```

## openapi-summarize

Produce a compact, LLM-friendly text summary of an OpenAPI schema.

```bash
uv run openapi-summarize schema.json
curl -s http://localhost:8000/openapi.json | uv run openapi-summarize
```
