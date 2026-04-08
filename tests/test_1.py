import contextlib
import io
import json
import pathlib
import re
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from openapi_audit.audit import (
    AuditConfig,
    audit_schema,
    get_checks,
    load_config,
    parse_args,
    parse_config_regexps,
    print_result,
    resolve_config,
)
from openapi_audit.audit import main as audit_main
from openapi_audit.summarize import main as summarize_main

EXAMPLES_PATH = pathlib.Path(__file__).parent / "examples"
TEST_PYPROJECT_TOML_PATH = pathlib.Path(__file__).parent / "fake-pyproject.toml"
EXAMPLE_PARAMS = [pytest.param(p, id=str(p.relative_to(EXAMPLES_PATH))) for p in EXAMPLES_PATH.rglob("*.json")]


@pytest.fixture(scope="session")
def petstore_json_text() -> str:
    return (EXAMPLES_PATH / "learn" / "v3.0" / "petstore.json").read_text()


@pytest.fixture()
def petstore_schema(petstore_json_text):
    return json.loads(petstore_json_text)


@pytest.mark.parametrize("example", EXAMPLE_PARAMS)
def test_audit(example: pathlib.Path):
    schema = json.loads(example.read_text())
    results = audit_schema(schema)
    assert results  # So far, all examples have some issues


@pytest.mark.parametrize("example", EXAMPLE_PARAMS)
def test_audit_cli(example: pathlib.Path):
    sio = io.StringIO()
    with contextlib.suppress(SystemExit), redirect_stdout(sio):
        audit_main([str(example)])
    assert sio.getvalue()  # Just a smoke test


@pytest.mark.parametrize("example", EXAMPLE_PARAMS)
def test_summarize_cli(example: pathlib.Path):
    sio = io.StringIO()
    with redirect_stdout(sio):
        summarize_main([str(example)])
    assert sio.getvalue()  # Just a smoke test


def test_audit_config():
    args = parse_args(["--config", str(TEST_PYPROJECT_TOML_PATH), "--ignore=no-json-content-type"])
    cfg = resolve_config(args)
    assert cfg.ignore == {"missing-parameter-example", "5xx-response", "no-json-content-type"}  # merged
    assert cfg.ignore_regexps["missing-parameter-example"]  # Just checking something got parsed


def test_audit_config_invalid_check(tmp_path: pathlib.Path):
    config_path = tmp_path / "bad-pyproject.toml"
    config_path.write_text('[tool.openapi-audit]\nignore-checks = ["no-such-check"]\n')
    args = parse_args(["--config", str(config_path)])
    with pytest.raises(SystemExit, match="2"):
        resolve_config(args)


def test_audit_stdin(petstore_json_text: str):
    """load_schema reads from stdin when no filename is given."""
    sio = io.StringIO()
    with patch("sys.stdin", io.StringIO(petstore_json_text)), redirect_stdout(sio), contextlib.suppress(SystemExit):
        audit_main([])
    assert "Swagger Petstore" in sio.getvalue()


def test_summarize_stdin(petstore_json_text: str):
    """summarize main reads from stdin when no filename is given."""
    sio = io.StringIO()
    with patch("sys.stdin", io.StringIO(petstore_json_text)), redirect_stdout(sio):
        summarize_main([])
    assert "Swagger Petstore" in sio.getvalue()


def test_audit_ignore_and_regexp(petstore_schema):
    """Exercise the ignore and ignore_regexps filtering in audit_schema."""
    # Run with no ignores first to see what fires
    baseline = audit_schema(petstore_schema)
    assert baseline.results
    # Now ignore all checks that fired — the ignore branch (line 576)
    cfg = AuditConfig(ignore=set(baseline.results.keys()))
    result = audit_schema(petstore_schema, cfg)
    assert not result.has_issues
    # Test ignore_regexps: ignore all issues via a catch-all pattern (line 581)
    cfg2 = AuditConfig(ignore_regexps={k: [re.compile(".*")] for k in baseline.results})
    result2 = audit_schema(petstore_schema, cfg2)
    assert not result2.has_issues


def test_load_config_raw(tmp_path: pathlib.Path):
    """load_config returns raw config when there is no [tool.openapi-audit] namespace."""
    config_path = tmp_path / "raw.toml"
    config_path.write_text('ignore-checks = ["5xx-response"]\n')
    cfg = load_config(str(config_path))
    assert cfg["ignore-checks"] == ["5xx-response"]


def test_parse_config_regexps_unknown_check():
    """parse_config_regexps raises on unknown check IDs."""
    with pytest.raises(ValueError, match="Unknown check ID in config"):
        parse_config_regexps({"checks": {"no-such-check": {"ignore-regexps": [".*"]}}})


def test_resolve_config_max_summary_length(tmp_path: pathlib.Path):
    """resolve_config picks up max-summary-length from the config file."""
    n = 42
    config_path = tmp_path / "cfg.toml"
    config_path.write_text(f"[tool.openapi-audit]\nmax-summary-length = {n}\n")
    args = parse_args(["--config", str(config_path)])
    assert resolve_config(args).max_summary_length == n


def test_print_result_skip(petstore_schema):
    """print_result shows [SKIP] for ignored checks."""
    cfg = AuditConfig(ignore={"missing-operation-id"})
    ar = audit_schema(petstore_schema, cfg)
    sio = io.StringIO()
    with redirect_stdout(sio):
        print_result(ar, cfg)
    assert "[SKIP]" in sio.getvalue()


def test_list_checks():
    """--list-checks prints available checks and exits."""
    sio = io.StringIO()
    with redirect_stdout(sio):
        audit_main(["--list-checks"])
    output = sio.getvalue()
    for check in get_checks():
        assert check.id in output
        assert check.description in output


def test_no_issues_found(tmp_path: pathlib.Path):
    """A clean schema prints 'No issues found!'."""
    clean_schema = {
        "openapi": "3.0.3",
        "info": {"title": "Clean API", "version": "1.0.0"},
        "security": [{"apiKey": []}],
        "components": {"securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
        "paths": {},
    }
    schema_path = tmp_path / "clean.json"
    schema_path.write_text(json.dumps(clean_schema))
    sio = io.StringIO()
    with redirect_stdout(sio):
        audit_main([str(schema_path)])
    assert "No issues found!" in sio.getvalue()
