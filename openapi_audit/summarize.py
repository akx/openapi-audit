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
import dataclasses
import json
import sys
from collections.abc import Iterator


@dataclasses.dataclass(frozen=True)
class SummarizeConfig:
    max_ref_depth: int = 2
    max_enum_display: int = 5
    max_desc_length: int = 80


class Summarizer:
    def __init__(self, schema: dict, config: SummarizeConfig | None = None) -> None:
        self.schema = schema
        self.config = config or SummarizeConfig()
        self.components = schema.get("components", {}).get("schemas", {})

    def summarize(self) -> str:
        """Produce a compact, LLM-friendly text summary of the schema."""
        return "\n".join(self._emit_lines())

    def _emit_lines(self) -> Iterator[str]:
        info = self.schema.get("info", {})
        yield f"# {info.get('title', 'Untitled API')} (v{info.get('version', '?')})"
        if info.get("description"):
            yield f"  {info['description'].strip().splitlines()[0]}"
        yield ""

        yield from self._summarize_security()
        yield from self._summarize_operations()
        yield from self._summarize_component_schemas()

    def _describe_ref(self, schema_obj: dict, depth: int, seen: set[str]) -> str:
        ref = schema_obj["$ref"]
        name = ref.rsplit("/", 1)[-1]
        if ref in seen or depth > self.config.max_ref_depth:
            return name
        resolved = self.components.get(name, {})
        return self._describe_schema_shape(resolved, depth=depth, seen=seen | {ref})

    def _describe_composition(self, keyword: str, schema_obj: dict, depth: int, seen: set[str]) -> str:
        joiner = " & " if keyword == "allOf" else " | "
        parts = [self._describe_schema_shape(s, depth=depth + 1, seen=seen) for s in schema_obj[keyword]]
        return joiner.join(parts)

    def _describe_object(self, schema_obj: dict, depth: int, seen: set[str]) -> str:
        props = schema_obj.get("properties", {})
        if not props:
            return "object"
        required = set(schema_obj.get("required", []))
        parts = []
        for name, prop in props.items():
            shape = self._describe_schema_shape(prop, depth=depth + 1, seen=seen)
            marker = "" if name in required else "?"
            parts.append(f"{name}{marker}: {shape}")
        return "{" + ", ".join(parts) + "}"

    def _describe_schema_shape(self, schema_obj: dict, *, depth: int = 0, seen: set[str] | None = None) -> str:
        """Return a compact one-line shape description for a schema object."""
        if seen is None:
            seen = set()
        if "$ref" in schema_obj:
            return self._describe_ref(schema_obj, depth, seen)
        for keyword in ("allOf", "oneOf", "anyOf"):
            if keyword in schema_obj:
                return self._describe_composition(keyword, schema_obj, depth, seen)
        typ = schema_obj.get("type", "any")
        if typ == "array":
            inner = self._describe_schema_shape(schema_obj.get("items", {}), depth=depth + 1, seen=seen)
            return f"[{inner}]"
        if typ == "object":
            return self._describe_object(schema_obj, depth, seen)
        if "enum" in schema_obj:
            vals = ", ".join(repr(v) for v in schema_obj["enum"][: self.config.max_enum_display])
            if len(schema_obj["enum"]) > self.config.max_enum_display:
                vals += ", ..."
            return f"{typ}({vals})"
        fmt = schema_obj.get("format")
        return f"{typ}<{fmt}>" if fmt else typ

    def _summarize_security(self) -> Iterator[str]:
        schemes = self.schema.get("components", {}).get("securitySchemes", {})
        if not schemes:
            return
        yield "## Security schemes"
        for name, sec in sorted(schemes.items()):
            yield f"  {name}: {sec.get('type', '?')} ({sec.get('scheme', sec.get('in', '?'))})"
        if global_sec := self.schema.get("security", []):
            names = [k for entry in global_sec for k in entry]
            yield f"  Global: {', '.join(names)}"
        yield ""

    def _summarize_operation(self, details: dict, path: str, method: str) -> Iterator[str]:
        op_id = details.get("operationId", "")
        label = f"  {method.upper()} {path}"
        if op_id:
            label += f"  ({op_id})"
        if summary := details.get("summary", ""):
            label += f"  -- {summary}"
        yield label

        if params := details.get("parameters", []):
            param_parts = []
            for p in params:
                shape = self._describe_schema_shape(p.get("schema", {}))
                req = "*" if p.get("required") else "?"
                param_parts.append(f"{p.get('name', '?')}{req}:{shape}")
            yield f"    params: {', '.join(param_parts)}"

        if rb := details.get("requestBody", {}):
            for media_type, media in rb.get("content", {}).items():
                shape = self._describe_schema_shape(media.get("schema", {}))
                yield f"    body ({media_type}): {shape}"

        if resp_parts := list(self._summarize_responses(details.get("responses", {}))):
            yield f"    responses: {', '.join(resp_parts)}"

    def _summarize_responses(self, responses: dict) -> Iterator[str]:
        for status in sorted(responses):
            resp = responses[status]
            if not isinstance(resp, dict):
                continue
            if content := resp.get("content", {}):
                media = next(iter(content.values()))
                shape = self._describe_schema_shape(media.get("schema", {}))
                yield f"{status}:{shape}"
            else:
                yield str(status)

    def _summarize_operations(self) -> Iterator[str]:
        ops_by_tag: dict[str, list[tuple[str, str, dict]]] = {}
        for path, method, details in _iter_operations(self.schema):
            tags = details.get("tags", ["(untagged)"])
            for tag in tags:
                ops_by_tag.setdefault(tag, []).append((path, method, details))

        yield "## Operations"
        for tag in sorted(ops_by_tag):
            yield f"  ### {tag}"
            for path, method, details in ops_by_tag[tag]:
                yield from self._summarize_operation(details, path, method)
            yield ""

    def _summarize_component_schemas(self) -> Iterator[str]:
        if not self.components:
            return
        yield "## Schemas"
        for name, s in sorted(self.components.items()):
            shape = self._describe_schema_shape(s, seen={f"#/components/schemas/{name}"})
            line = f"  {name}: {shape}"
            if desc := s.get("description", ""):
                line += f"  -- {desc.strip().splitlines()[0][: self.config.max_desc_length]}"
            yield line
        yield ""


def _iter_operations(schema: dict):
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, details in methods.items():
            if isinstance(details, dict):
                yield path, method, details


def summarize_schema(schema: dict, config: SummarizeConfig | None = None) -> str:
    """Produce a compact, LLM-friendly text summary of an OpenAPI schema."""
    return Summarizer(schema, config).summarize()


def main(argv: list[str] | None = None) -> None:
    config_defaults = dataclasses.asdict(SummarizeConfig())

    parser = argparse.ArgumentParser(description="Summarize an OpenAPI schema in a compact, LLM-friendly format.")
    parser.add_argument("schema_file", nargs="?", help="Path to OpenAPI JSON file (reads stdin if omitted)")
    parser.add_argument(
        "--max-ref-depth",
        type=int,
        default=config_defaults["max_ref_depth"],
        help="Maximum depth when resolving $ref (default: %(default)s)",
    )
    parser.add_argument(
        "--max-enum-display",
        type=int,
        default=config_defaults["max_enum_display"],
        help="Maximum number of enum values to display (default: %(default)s)",
    )
    parser.add_argument(
        "--max-desc-length",
        type=int,
        default=config_defaults["max_desc_length"],
        help="Maximum length for schema descriptions (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    config = SummarizeConfig(
        max_ref_depth=args.max_ref_depth,
        max_enum_display=args.max_enum_display,
        max_desc_length=args.max_desc_length,
    )

    if args.schema_file:
        with open(args.schema_file) as f:
            schema = json.load(f)
    else:
        schema = json.load(sys.stdin)

    print(summarize_schema(schema, config))


if __name__ == "__main__":
    main()
