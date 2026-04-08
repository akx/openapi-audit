"""
Produce a compact, LLM-friendly text summary of an OpenAPI schema.

Usage:
    # From a running server:
    curl -s 'http://127.0.0.1:8888/api/v0/openapi/?format=openapi-json' | uv run openapi-summarize

    # From a file:
    uv run -m openapi-summarize schema.json
"""

from __future__ import annotations

import argparse
import json
import sys

_MAX_REF_DEPTH = 2
_MAX_ENUM_DISPLAY = 5


def _iter_operations(schema: dict):
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, details in methods.items():
            if isinstance(details, dict):
                yield path, method, details


def _describe_ref(schema_obj: dict, components: dict, depth: int, seen: set[str]) -> str:
    ref = schema_obj["$ref"]
    name = ref.rsplit("/", 1)[-1]
    if ref in seen or depth > _MAX_REF_DEPTH:
        return name
    resolved = components.get(name, {})
    return _describe_schema_shape(resolved, components, depth=depth, seen=seen | {ref})


def _describe_composition(keyword: str, schema_obj: dict, components: dict, depth: int, seen: set[str]) -> str:
    joiner = " & " if keyword == "allOf" else " | "
    parts = [_describe_schema_shape(s, components, depth=depth + 1, seen=seen) for s in schema_obj[keyword]]
    return joiner.join(parts)


def _describe_object(schema_obj: dict, components: dict, depth: int, seen: set[str]) -> str:
    props = schema_obj.get("properties", {})
    if not props:
        return "object"
    required = set(schema_obj.get("required", []))
    parts = []
    for name, prop in props.items():
        shape = _describe_schema_shape(prop, components, depth=depth + 1, seen=seen)
        marker = "" if name in required else "?"
        parts.append(f"{name}{marker}: {shape}")
    return "{" + ", ".join(parts) + "}"


def _describe_schema_shape(schema_obj: dict, components: dict, *, depth: int = 0, seen: set[str] | None = None) -> str:
    """Return a compact one-line shape description for a schema object."""
    if seen is None:
        seen = set()
    if "$ref" in schema_obj:
        return _describe_ref(schema_obj, components, depth, seen)
    for keyword in ("allOf", "oneOf", "anyOf"):
        if keyword in schema_obj:
            return _describe_composition(keyword, schema_obj, components, depth, seen)
    typ = schema_obj.get("type", "any")
    if typ == "array":
        inner = _describe_schema_shape(schema_obj.get("items", {}), components, depth=depth + 1, seen=seen)
        return f"[{inner}]"
    if typ == "object":
        return _describe_object(schema_obj, components, depth, seen)
    if "enum" in schema_obj:
        vals = ", ".join(repr(v) for v in schema_obj["enum"][:_MAX_ENUM_DISPLAY])
        if len(schema_obj["enum"]) > _MAX_ENUM_DISPLAY:
            vals += ", ..."
        return f"{typ}({vals})"
    fmt = schema_obj.get("format")
    return f"{typ}<{fmt}>" if fmt else typ


def _summarize_security(schema: dict, lines: list[str]) -> None:
    schemes = schema.get("components", {}).get("securitySchemes", {})
    if not schemes:
        return
    lines.append("## Security schemes")
    for name, sec in sorted(schemes.items()):
        lines.append(f"  {name}: {sec.get('type', '?')} ({sec.get('scheme', sec.get('in', '?'))})")
    global_sec = schema.get("security", [])
    if global_sec:
        names = [k for entry in global_sec for k in entry]
        lines.append(f"  Global: {', '.join(names)}")
    lines.append("")


def _summarize_operation(details: dict, path: str, method: str, components: dict, lines: list[str]) -> None:
    op_id = details.get("operationId", "")
    summary = details.get("summary", "")
    label = f"  {method.upper()} {path}"
    if op_id:
        label += f"  ({op_id})"
    if summary:
        label += f"  -- {summary}"
    lines.append(label)

    # Parameters
    params = details.get("parameters", [])
    if params:
        param_parts = []
        for p in params:
            shape = _describe_schema_shape(p.get("schema", {}), components)
            req = "*" if p.get("required") else "?"
            param_parts.append(f"{p.get('name', '?')}{req}:{shape}")
        lines.append(f"    params: {', '.join(param_parts)}")

    # Request body
    rb = details.get("requestBody", {})
    if rb:
        for media_type, media in rb.get("content", {}).items():
            shape = _describe_schema_shape(media.get("schema", {}), components)
            lines.append(f"    body ({media_type}): {shape}")

    # Responses
    resp_parts = _summarize_responses(details.get("responses", {}), components)
    if resp_parts:
        lines.append(f"    responses: {', '.join(resp_parts)}")


def _summarize_responses(responses: dict, components: dict) -> list[str]:
    parts = []
    for status in sorted(responses):
        resp = responses[status]
        if not isinstance(resp, dict):
            continue
        content = resp.get("content", {})
        if content:
            media = next(iter(content.values()))
            shape = _describe_schema_shape(media.get("schema", {}), components)
            parts.append(f"{status}:{shape}")
        else:
            parts.append(str(status))
    return parts


def _summarize_operations(schema: dict, components: dict, lines: list[str]) -> None:
    ops_by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, method, details in _iter_operations(schema):
        tags = details.get("tags", ["(untagged)"])
        for tag in tags:
            ops_by_tag.setdefault(tag, []).append((path, method, details))

    lines.append("## Operations")
    for tag in sorted(ops_by_tag):
        lines.append(f"  ### {tag}")
        for path, method, details in ops_by_tag[tag]:
            _summarize_operation(details, path, method, components, lines)
        lines.append("")


def _summarize_component_schemas(components: dict, lines: list[str]) -> None:
    if not components:
        return
    lines.append("## Schemas")
    for name, s in sorted(components.items()):
        shape = _describe_schema_shape(s, components, seen={f"#/components/schemas/{name}"})
        line = f"  {name}: {shape}"
        desc = s.get("description", "")
        if desc:
            line += f"  -- {desc.strip().splitlines()[0][:80]}"
        lines.append(line)
    lines.append("")


def summarize_schema(schema: dict) -> str:
    """Produce a compact, LLM-friendly text summary of an OpenAPI schema."""
    lines: list[str] = []
    info = schema.get("info", {})
    lines.append(f"# {info.get('title', 'Untitled API')} (v{info.get('version', '?')})")
    if info.get("description"):
        lines.append(f"  {info['description'].strip().splitlines()[0]}")
    lines.append("")

    components = schema.get("components", {}).get("schemas", {})

    _summarize_security(schema, lines)
    _summarize_operations(schema, components, lines)
    _summarize_component_schemas(components, lines)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Summarize an OpenAPI schema in a compact, LLM-friendly format.")
    parser.add_argument("schema_file", nargs="?", help="Path to OpenAPI JSON file (reads stdin if omitted)")
    args = parser.parse_args(argv)

    if args.schema_file:
        with open(args.schema_file) as f:
            schema = json.load(f)
    else:
        schema = json.load(sys.stdin)

    print(summarize_schema(schema))


if __name__ == "__main__":
    main()
