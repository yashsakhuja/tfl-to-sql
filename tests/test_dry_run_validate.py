"""Tests for tools/dry_run_validate.py — mocked BigQuery client, no live calls.

dry_run_one() does a real `from google.cloud import bigquery` internally
(needed to build a QueryJobConfig) even with the client mocked, so the two
tests that call it need the optional `bigquery` extra installed
(`pip install -e ".[bigquery]"`, or `.[dev,bigquery]` — see pyproject.toml
and CI). They're skipped, not failed, when it isn't present."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import dry_run_validate as drv  # noqa: E402


def _has_bigquery() -> bool:
    try:
        import google.cloud.bigquery  # noqa: F401
        return True
    except ImportError:
        return False


requires_bigquery = pytest.mark.skipif(
    not _has_bigquery(), reason="google-cloud-bigquery not installed (optional 'bigquery' extra)",
)


def test_to_plain_bigquery_sql_strips_config_block():
    sqlx = 'config {\n  type: "table",\n  schema: "x"\n}\n\nSELECT 1'
    result = drv.to_plain_bigquery_sql(sqlx, "my_dataset")
    assert "config {" not in result
    assert result.strip() == "SELECT 1"


def test_to_plain_bigquery_sql_replaces_ref_calls():
    sqlx = "SELECT * FROM ${ref('orders')}"
    result = drv.to_plain_bigquery_sql(sqlx, "my_dataset")
    assert result == "SELECT * FROM `my_dataset`.`orders`"


@requires_bigquery
def test_dry_run_one_returns_ok_on_success():
    client = MagicMock()
    client.query.return_value = MagicMock()
    ok, message = drv.dry_run_one(client, "proj", "SELECT 1")
    assert ok is True
    assert message == "OK"


@requires_bigquery
def test_dry_run_one_returns_failure_message_on_error():
    client = MagicMock()
    client.query.side_effect = RuntimeError("Syntax error: Unexpected keyword END")
    ok, message = drv.dry_run_one(client, "proj", "garbage sql")
    assert ok is False
    assert "Syntax error" in message
