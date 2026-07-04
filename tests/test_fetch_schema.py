"""Tests for tools/fetch_schema.py — mocked BigQuery client, no live calls.

fetch_columns() does a real `from google.cloud import bigquery` internally
(needed to build a query parameter object) even with the client mocked, so
the three tests that call it need the optional `bigquery` extra installed
(`pip install -e ".[bigquery]"`, or `.[dev,bigquery]` — see pyproject.toml
and CI). They're skipped, not failed, when it isn't present."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = Path(__file__).parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import fetch_schema  # noqa: E402
from conftest import FLOWS_DIR  # noqa: E402


def _has_bigquery() -> bool:
    try:
        import google.cloud.bigquery  # noqa: F401
        return True
    except ImportError:
        return False


FLOW = FLOWS_DIR / "FST Segmentation Prep.tfl"
requires_bigquery = pytest.mark.skipif(
    not _has_bigquery(), reason="google-cloud-bigquery not installed (optional 'bigquery' extra)",
)


def test_input_table_names_finds_the_real_source_table():
    tables = fetch_schema.input_table_names(str(FLOW))
    assert tables == {"fst_ecom_retail_sales": "FST Ecom & Retail Sales"}


@requires_bigquery
def test_fetch_columns_returns_column_names_from_mocked_client():
    client = MagicMock()
    row_a, row_b = MagicMock(), MagicMock()
    row_a.column_name, row_b.column_name = "order_id", "customer_id"
    client.query.return_value.result.return_value = [row_a, row_b]

    columns = fetch_schema.fetch_columns(client, "proj", "ds", "orders")
    assert columns == ["order_id", "customer_id"]


@requires_bigquery
def test_fetch_columns_returns_empty_and_warns_on_query_failure(capsys):
    client = MagicMock()
    client.query.side_effect = RuntimeError("permission denied")

    columns = fetch_schema.fetch_columns(client, "proj", "ds", "orders")
    assert columns == []
    assert "WARNING" in capsys.readouterr().out


@requires_bigquery
def test_fetch_columns_warns_on_empty_result(capsys):
    client = MagicMock()
    client.query.return_value.result.return_value = []

    columns = fetch_schema.fetch_columns(client, "proj", "ds", "ghost_table")
    assert columns == []
    assert "no columns found" in capsys.readouterr().out


def test_missing_flow_file_exits_nonzero_via_cli():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(_TOOLS_DIR / "fetch_schema.py"), "/no/such/file.tfl",
         "--project", "p", "--dataset", "d"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "File not found" in result.stdout + result.stderr
