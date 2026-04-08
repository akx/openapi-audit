"""
Audit an OpenAPI schema for quality issues.

Usage:
    # From a running server:
    curl -s 'http://127.0.0.1:8888/api/v0/openapi/?format=openapi-json' | uv run openapi_audit.py

    # From a file:
    uv run openapi_audit.py schema.json
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterator


@dataclasses.dataclass(kw_only=True, frozen=True)
class IssueContent:
    location: tuple[str | int, ...] = ()
    message: str = ""


@dataclasses.dataclass(kw_only=True, frozen=True)
class Issue(IssueContent):
    id: str

    def format(self) -> str:
        loc = " ".join(str(p) for p in self.location)
        if self.message:
            return f"  {loc}: {self.message}" if loc else f"  {self.message}"
        return f"  {loc}"


@dataclasses.dataclass(frozen=True)
class AuditConfig:
    ignore: set[str] = dataclasses.field(default_factory=set)
    ignore_regexps: dict[str, list[re.Pattern[str]]] = dataclasses.field(default_factory=dict)
    max_summary_length: int = 80


@dataclasses.dataclass(frozen=True)
class AuditResult:
    results: dict[str, list[Issue]]

    @property
    def has_issues(self) -> bool:
        return bool(self.results)


@dataclasses.dataclass(frozen=True)
class Check:
    id: str
    description: str
    func: Callable[..., Iterator[IssueContent]]
    accepts_config: bool

    def run(self, schema: dict, config: AuditConfig) -> Iterator[Issue]:
        ic_iterator = self.func(schema, config=config) if self.accepts_config else self.func(schema)
        for ic in ic_iterator:
            yield Issue(id=self.id, location=ic.location, message=ic.message)


CheckerFn = Callable[..., Iterator[IssueContent]]


_CHECKS: list[Check] = []


def checker(*, id: str, description: str):
    def decorator(fn: CheckerFn) -> CheckerFn:
        check = Check(
            id=id,
            description=description,
            func=fn,
            accepts_config=("config" in inspect.signature(fn).parameters),
        )
        _CHECKS.append(check)
        return fn

    return decorator


def load_schema(filename: str | None) -> dict:
    if filename:
        with open(filename) as f:
            return json.load(f)
    return json.load(sys.stdin)


def iter_operations(schema: dict):
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, details in methods.items():
            if not isinstance(details, dict):
                continue
            yield path, method, details


@checker(id="missing-description", description="Missing descriptions")
def check_missing_descriptions(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        if not details.get("description"):
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"))


@checker(id="missing-operation-id", description="Missing operationIds")
def check_missing_operation_ids(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        if not details.get("operationId"):
            yield IssueContent(location=(method.upper(), path))


@checker(id="duplicate-operation-id", description="Duplicate operationIds")
def check_duplicate_operation_ids(schema: dict) -> Iterator[IssueContent]:
    seen: dict[str, str] = {}
    for path, method, details in iter_operations(schema):
        op_id = details.get("operationId")
        if not op_id:
            continue
        key = f"{method.upper()} {path}"
        if op_id in seen:
            yield IssueContent(location=(op_id,), message=f"{seen[op_id]} vs {key}")
        else:
            seen[op_id] = key


@checker(id="missing-summary", description="Missing summaries")
def check_missing_summaries(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        if not details.get("summary"):
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"))


@checker(id="verbose-summary", description="Verbose or malformed summaries")
def check_verbose_summaries(schema: dict, *, config: AuditConfig) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        summary = details.get("summary", "")
        if not summary:
            continue
        problems = []
        if len(summary) > config.max_summary_length:
            problems.append(f"too long ({len(summary)} chars)")
        if "\n" in summary:
            problems.append("contains newlines")
        if summary and not summary[0].isupper():
            problems.append("does not start with uppercase")
        if problems:
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=", ".join(problems))


_COREAPI_METHOD_PREFIX_RE = re.compile(
    r"^(get|post|put|patch|delete):\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@checker(id="coreapi-prefix", description="CoreAPI-style method prefixes in summary/description")
def check_coreapi_method_prefixes(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        op_id = details.get("operationId", "?")
        for field in ("summary", "description"):
            text = details.get(field, "")
            if text and _COREAPI_METHOD_PREFIX_RE.search(text):
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=field)


@checker(id="missing-response-schema", description="Missing response schemas (2xx with no content or description)")
def check_missing_response_schemas(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        responses = details.get("responses", {})
        for status_code, response in responses.items():
            if not status_code.startswith("2"):
                continue
            if not isinstance(response, dict):
                continue
            content = response.get("content", {})
            if not content and not response.get("description"):
                op_id = details.get("operationId", "?")
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=f"-> {status_code}")


@checker(id="param-no-description", description="Parameters without descriptions")
def check_params_without_descriptions(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        for param in details.get("parameters", []):
            if not param.get("description"):
                param_name = param.get("name", "?")
                op_id = details.get("operationId", "?")
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=param_name)


@checker(id="schema-no-description", description="Component schemas without descriptions")
def check_schemas_without_descriptions(schema: dict) -> Iterator[IssueContent]:
    schemas = schema.get("components", {}).get("schemas", {})
    for name, s in sorted(schemas.items()):
        if not s.get("description"):
            yield IssueContent(location=(name,))


@checker(id="no-global-security", description="No global security defined")
def check_no_global_security(schema: dict) -> Iterator[IssueContent]:
    if not schema.get("security"):
        yield IssueContent(message="No global security defined")


@checker(id="no-security-scheme", description="No security schemes defined")
def check_no_security_schemes(schema: dict) -> Iterator[IssueContent]:
    if not schema.get("components", {}).get("securitySchemes", {}):
        yield IssueContent(message="No security schemes defined")


@checker(id="unused-security-scheme", description="Security schemes defined but never referenced")
def check_unused_security_schemes(schema: dict) -> Iterator[IssueContent]:
    schemes = schema.get("components", {}).get("securitySchemes", {})
    if not schemes:
        return
    referenced = _get_referenced_schemes(schema)
    for name in sorted(schemes):
        if name not in referenced:
            yield IssueContent(message=f"Security scheme '{name}' is defined but never referenced")


def _collect_refs(obj, refs: set[str]) -> None:
    if isinstance(obj, dict):
        if "$ref" in obj:
            refs.add(obj["$ref"])
        for v in obj.values():
            _collect_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_refs(item, refs)


@checker(id="path-param-mismatch", description="Path parameter mismatches")
def check_path_parameter_mismatch(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        template_params = set(re.findall(r"\{(\w+)\}", path))
        declared_params = {p["name"] for p in details.get("parameters", []) if p.get("in") == "path"}
        op_id = details.get("operationId", "?")
        loc = (method.upper(), path, f"[{op_id}]")
        for missing in sorted(template_params - declared_params):
            yield IssueContent(location=loc, message=f"'{{{missing}}}' in path but not declared")
        for extra in sorted(declared_params - template_params):
            yield IssueContent(location=loc, message=f"'{extra}' declared as path param but not in path template")


@checker(id="missing-request-body", description="Missing request body (POST/PUT/PATCH)")
def check_missing_request_body(schema: dict) -> Iterator[IssueContent]:
    methods_needing_body = {"post", "put", "patch"}
    for path, method, details in iter_operations(schema):
        if method.lower() in methods_needing_body and not details.get("requestBody"):
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"))


@checker(id="unused-schema", description="Unused component schemas")
def check_unused_schemas(schema: dict) -> Iterator[IssueContent]:
    schemas = schema.get("components", {}).get("schemas", {})
    if not schemas:
        return
    all_refs: set[str] = set()
    _collect_refs(schema, all_refs)
    for name in sorted(schemas):
        ref = f"#/components/schemas/{name}"
        if ref not in all_refs:
            yield IssueContent(location=(name,))


def _classify_casing(name: str) -> str:
    if "_" in name:
        return "snake_case"
    if "-" in name:
        return "kebab-case"
    if name[0:1].isupper():
        return "PascalCase"
    return "camelCase"


@checker(id="inconsistent-casing", description="Inconsistent operationId casing")
def check_inconsistent_operation_id_casing(schema: dict) -> Iterator[IssueContent]:
    casing_counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for _, _, details in iter_operations(schema):
        op_id = details.get("operationId")
        if not op_id:
            continue
        style = _classify_casing(op_id)
        casing_counts[style] = casing_counts.get(style, 0) + 1
        examples.setdefault(style, []).append(op_id)
    if len(casing_counts) <= 1:
        return
    max_examples = 3
    for style in sorted(casing_counts):
        sample = ", ".join(examples[style][:max_examples])
        if len(examples[style]) > max_examples:
            sample += ", ..."
        yield IssueContent(location=(style,), message=f"{casing_counts[style]} operations (e.g. {sample})")


@checker(id="missing-tags", description="Operations without tags")
def check_missing_tags(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        if not details.get("tags"):
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"))


@checker(id="missing-error-response", description="Missing error responses (no 4xx/5xx/default)")
def check_missing_error_responses(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        responses = details.get("responses", {})
        has_error = any(str(code).startswith(("4", "5")) or code == "default" for code in responses)
        if not has_error:
            op_id = details.get("operationId", "?")
            yield IssueContent(location=(method.upper(), path, f"[{op_id}]"))


@checker(id="trailing-slash", description="Trailing slash inconsistency")
def check_trailing_slash_inconsistency(schema: dict) -> Iterator[IssueContent]:
    paths = list(schema.get("paths", {}).keys())
    if not paths:
        return
    with_slash = [p for p in paths if p != "/" and p.endswith("/")]
    without_slash = [p for p in paths if not p.endswith("/")]
    max_examples = 3
    if with_slash and without_slash:
        yield IssueContent(message=f"{len(with_slash)} paths end with '/', {len(without_slash)} do not")
        yield IssueContent(
            location=(),
            message=f"With: {', '.join(with_slash[:max_examples])}{', ...' if len(with_slash) > max_examples else ''}",
        )
        yield IssueContent(
            location=(),
            message=f"Without: {', '.join(without_slash[:max_examples])}{', ...' if len(without_slash) > max_examples else ''}",
        )


def _collect_tags(schema: dict) -> tuple[set[str], set[str]]:
    defined = {t["name"] for t in schema.get("tags", []) if isinstance(t, dict)}
    used: set[str] = set()
    for _, _, details in iter_operations(schema):
        for tag in details.get("tags", []):
            used.add(tag)
    return defined, used


@checker(id="unused-tag", description="Tags defined but never used")
def check_unused_tags(schema: dict) -> Iterator[IssueContent]:
    defined, used = _collect_tags(schema)
    for tag in sorted(defined - used):
        yield IssueContent(message=f"Tag '{tag}' is defined but never used")


@checker(id="undefined-tag", description="Tags used but not defined")
def check_undefined_tags(schema: dict) -> Iterator[IssueContent]:
    defined, used = _collect_tags(schema)
    for tag in sorted(used - defined):
        yield IssueContent(message=f"Tag '{tag}' is used but not defined in top-level tags")


@checker(id="opaque-schema", description="Opaque object schemas (no properties)")
def check_opaque_schemas(schema: dict) -> Iterator[IssueContent]:
    schemas = schema.get("components", {}).get("schemas", {})
    for name, s in sorted(schemas.items()):
        non_opaque_keys = ("properties", "allOf", "oneOf", "anyOf", "$ref", "additionalProperties")
        if s.get("type") == "object" and not any(s.get(k) for k in non_opaque_keys):
            yield IssueContent(location=(name,))


def _has_example(obj: dict) -> bool:
    return (
        obj.get("example") is not None
        or obj.get("examples") is not None
        or obj.get("schema", {}).get("example") is not None
    )


@checker(id="missing-parameter-example", description="Missing examples on parameters")
def check_missing_parameter_examples(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        op_id = details.get("operationId", "?")
        loc = (method.upper(), path, f"[{op_id}]")
        for param in details.get("parameters", []):
            if not _has_example(param):
                yield IssueContent(location=loc, message=f"{param.get('name', '?')}")


@checker(id="missing-body-example", description="Missing examples on request bodies")
def check_missing_body_examples(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        rb = details.get("requestBody", {})
        if not rb:
            continue
        op_id = details.get("operationId", "?")
        loc = (method.upper(), path, f"[{op_id}]")
        for media_type, media in rb.get("content", {}).items():
            if not _has_example(media):
                yield IssueContent(location=loc, message=f"request body of type {media_type}")


_CONVENTIONAL_SUCCESS_CODES = {
    "post": {"201", "202"},
    "delete": {"204"},
}


@checker(id="non-standard-success", description="Non-standard success codes")
def check_non_standard_success_codes(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        expected = _CONVENTIONAL_SUCCESS_CODES.get(method.lower())
        if not expected:
            continue
        responses = details.get("responses", {})
        success_codes = {c for c in responses if c.startswith("2")}
        if success_codes and not (success_codes & expected):
            op_id = details.get("operationId", "?")
            yield IssueContent(
                location=(method.upper(), path, f"[{op_id}]"),
                message=f"has {', '.join(sorted(success_codes))}, expected {' or '.join(sorted(expected))}",
            )


@checker(id="prop-no-description", description="Schema properties without descriptions")
def check_schema_properties_without_descriptions(schema: dict) -> Iterator[IssueContent]:
    schemas = schema.get("components", {}).get("schemas", {})
    for name, s in sorted(schemas.items()):
        for prop_name, prop in sorted(s.get("properties", {}).items()):
            if not prop.get("description"):
                yield IssueContent(location=(f"{name}.{prop_name}",))


@checker(id="broken-ref", description="Broken $ref references")
def check_broken_refs(schema: dict) -> Iterator[IssueContent]:
    all_refs: set[str] = set()
    _collect_refs(schema, all_refs)
    for ref in sorted(all_refs):
        if not ref.startswith("#/"):
            continue
        parts = ref.lstrip("#/").split("/")
        obj = schema
        for part in parts:
            if isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                yield IssueContent(location=(ref,))
                break


@checker(id="response-no-description", description="Responses without descriptions")
def check_response_descriptions(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        for status, response in (details.get("responses") or {}).items():
            if not isinstance(response, dict):
                continue
            if not response.get("description"):
                op_id = details.get("operationId", "?")
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=status)


@checker(id="5xx-response", description="Operations documenting 5xx responses")
def check_5xx_responses(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        for status in details.get("responses") or {}:
            if str(status).startswith("5"):
                op_id = details.get("operationId", "?")
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=str(status))


@checker(id="no-json-content-type", description="Success responses missing application/json content type")
def check_json_content_type(schema: dict) -> Iterator[IssueContent]:
    for path, method, details in iter_operations(schema):
        for status, response in (details.get("responses") or {}).items():
            if not isinstance(response, dict):
                continue
            if str(status) == "204" or not str(status).startswith("2"):
                continue
            if "content" not in response:
                continue
            if "application/json" not in response["content"]:
                op_id = details.get("operationId", "?")
                yield IssueContent(location=(method.upper(), path, f"[{op_id}]"), message=status)


def _collect_enums(obj, path: str = ""):
    if isinstance(obj, dict):
        if "enum" in obj and isinstance(obj["enum"], list):
            yield path, obj["enum"]
        for key, value in obj.items():
            yield from _collect_enums(value, f"{path}.{key}" if path else key)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _collect_enums(item, f"{path}[{i}]")


@checker(id="empty-enum", description="Empty enum arrays")
def check_empty_enums(schema: dict) -> Iterator[IssueContent]:
    for path, values in _collect_enums(schema):
        if len(values) == 0:
            yield IssueContent(location=(path,))


def _get_referenced_schemes(schema: dict) -> set[str]:
    referenced = set()
    for entry in schema.get("security", []):
        referenced.update(entry)
    for _, _, details in iter_operations(schema):
        for entry in details.get("security", []):
            referenced.update(entry)
    return referenced


ALL_CHECK_IDS = {c.id for c in _CHECKS}


def load_config(path: str) -> dict:
    with open(path, "rb") as f:
        config = tomllib.load(f)
    # Support `[tool.openapi-audit]` namespace (e.g. in pyproject.toml)
    tool_config = config.get("tool", {}).get("openapi-audit", {})
    if tool_config:
        return tool_config
    return config


def _validate_check_ids(check_ids: set[str]) -> set[str]:
    unknown = check_ids - set(ALL_CHECK_IDS)
    if unknown:
        raise ValueError(f"Unknown check IDs: {', '.join(sorted(unknown))}")
    return check_ids


def parse_config_regexps(config: dict) -> dict[str, list[re.Pattern[str]]]:
    result: dict[str, list[re.Pattern[str]]] = {}
    all_ids = set(ALL_CHECK_IDS)
    checks_conf = config.get("checks", {})
    for check_id, check_conf in checks_conf.items():
        if check_id not in all_ids:
            raise ValueError(f"Unknown check ID in config: {check_id}")
        patterns = check_conf.get("ignore-regexps", [])
        if patterns:
            result[check_id] = [re.compile(p) for p in patterns]
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit an OpenAPI schema for quality issues.")
    parser.add_argument("schema_file", nargs="?", help="Path to OpenAPI JSON file (reads stdin if omitted)")
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="CHECK",
        help="Check ID to skip (can be repeated). Use --list-checks to see IDs.",
    )
    parser.add_argument("--list-checks", action="store_true", help="List all available check IDs and exit")
    parser.add_argument("--config", metavar="FILE", help="Path to TOML config file for ignore rules")
    return parser.parse_args(argv)


def audit_schema(schema: dict, config: AuditConfig | None = None) -> AuditResult:
    """Run all checks against a schema dict, returning {check_id: [issues]} for failed checks."""
    cfg = config or AuditConfig()
    _validate_check_ids(cfg.ignore)
    results: dict[str, list[Issue]] = {}
    for check in _CHECKS:
        if check.id in cfg.ignore:
            continue
        issues: list[Issue] = []
        patterns = cfg.ignore_regexps.get(check.id, [])
        for issue in check.run(schema, config=cfg):
            if patterns and any(p.search(issue.format()) for p in patterns):
                continue
            issues.append(issue)
        if issues:
            results[check.id] = issues
    return AuditResult(results=results)


def resolve_config(args: argparse.Namespace) -> AuditConfig:
    ignored = set(args.ignore)
    kwargs = {}
    ignore_regexps: dict[str, list[re.Pattern[str]]] = {}
    try:
        _validate_check_ids(ignored)
        if args.config:
            config = load_config(args.config)
            ignore_regexps = parse_config_regexps(config)
            ignored.update(config.get("ignore-checks", []))
            _validate_check_ids(ignored)
            if (msl := config.get("max-summary-length")) is not None:
                kwargs["max_summary_length"] = int(msl)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        print("Run with --list-checks to see available IDs.", file=sys.stderr)
        sys.exit(2)
    return AuditConfig(ignore=ignored, ignore_regexps=ignore_regexps, **kwargs)


def print_result(ar: AuditResult, cfg: AuditConfig) -> None:
    for check in _CHECKS:
        check_header = f"{check.description} ({check.id})"
        if check.id in cfg.ignore:
            print(f"[SKIP] {check_header}")
        elif check.id in ar.results:
            issues = ar.results[check.id]
            print(f"[{len(issues)}] {check_header}:")
            for issue in issues:
                print(issue.format())
            print()
        else:
            print(f"[OK] {check_header}")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.list_checks:
        for check in _CHECKS:
            print(f"  {check.id:30s} {check.description}")
        return

    cfg = resolve_config(args)

    schema = load_schema(args.schema_file)

    total_ops = sum(1 for _ in iter_operations(schema))
    total_schemas = len(schema.get("components", {}).get("schemas", {}))
    print(
        f"Schema: {schema.get('info', {}).get('title', '?')} "
        f"({total_ops} operations, {total_schemas} component schemas)",
    )
    print()

    ar = audit_schema(schema, cfg)

    print_result(ar, cfg)

    if ar.has_issues:
        sys.exit(1)
    else:
        print("\nNo issues found!")


if __name__ == "__main__":
    main()
