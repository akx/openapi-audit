import contextlib
import io
import json
import pathlib
from contextlib import redirect_stdout

import pytest

from openapi_audit.audit import audit_schema, parse_args, resolve_config
from openapi_audit.audit import main as audit_main
from openapi_audit.summarize import main as summarize_main

EXAMPLES_PATH = pathlib.Path(__file__).parent / "examples"
TEST_PYPROJECT_TOML_PATH = pathlib.Path(__file__).parent / "fake-pyproject.toml"
EXAMPLE_PARAMS = [pytest.param(p, id=str(p.relative_to(EXAMPLES_PATH))) for p in EXAMPLES_PATH.rglob("*.json")]


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
