"""Tests for App/app_logic.py — the non-UI logic behind the Streamlit app.
Covers the regression this session found (extract_edges 2-tuple vs 3-tuple)
and the overrides/schema state-isolation guarantee the UI depends on."""

import io
import zipfile

from app_logic import (
    convert_tfl,
    list_input_tables,
    parse_column_names,
    render_sql_with_warnings,
)
from conftest import FLOWS_DIR

FLOW_BYTES = (FLOWS_DIR / "FST Segmentation Prep.tfl").read_bytes()


def test_convert_tfl_runs_without_the_edges_unpacking_bug():
    results, warnings, coverage = convert_tfl(FLOW_BYTES, "bigquery", "my_dataset", "flow.tfl")
    assert len(results) == 1
    assert coverage["nodes"] == 45
    assert coverage["flagged"] == len(warnings)


def test_convert_tfl_applies_overrides_and_does_not_leak_between_calls():
    baseline_results, baseline_warnings, baseline_coverage = convert_tfl(
        FLOW_BYTES, "bigquery", "my_dataset", "flow.tfl"
    )

    overrides = {"parameters": {
        "Parameters.192a3229-d1ff-47b9-ae47-9a0b0c718cbd": "208",
        "Parameters.382690fa-a3cc-404a-8d4e-7305f732b199": "104",
        "Parameters.03a2fddc-fdf5-4ecb-b9c2-aaf6f6d2abb1": "52",
        "Parameters.e7fc4ad8-153b-4cee-b575-c22753db54bb": "156",
        "Parameters.423040cf-51b5-466a-8017-344aa9c98a88": "260",
    }}
    _results, _warnings, overridden_coverage = convert_tfl(
        FLOW_BYTES, "bigquery", "my_dataset", "flow.tfl", overrides=overrides
    )
    assert overridden_coverage["flagged"] < baseline_coverage["flagged"]

    # A subsequent call with no overrides must not see the previous call's state.
    _results2, _warnings2, reset_coverage = convert_tfl(FLOW_BYTES, "bigquery", "my_dataset", "flow.tfl")
    assert reset_coverage["flagged"] == baseline_coverage["flagged"]


def test_convert_tfl_accepts_zip_packaged_tflx_bytes():
    """.tflx is Tableau's packaged (zipped) flow format — same content-based
    detection tfl_to_sql.load_tfl already does for .tfl, exercised here via
    the app's upload path specifically. The sample .tfl checked into this
    repo already happens to be zip-packaged, so pull its inner JSON out
    first rather than double-zipping it."""
    with zipfile.ZipFile(io.BytesIO(FLOW_BYTES)) as zf:
        flow_json = zf.read("flow")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("flow", flow_json)
    results, _warnings, coverage = convert_tfl(buf.getvalue(), "dataform", "my_dataset", "flow.tflx")
    assert len(results) == 1
    assert coverage["nodes"] == 45


def test_list_input_tables_finds_the_real_source_table():
    tables = list_input_tables(FLOW_BYTES)
    assert tables == {"fst_ecom_retail_sales": "FST Ecom & Retail Sales"}


def test_list_input_tables_returns_empty_dict_for_garbage_bytes():
    assert list_input_tables(b"not a real flow") == {}


def test_parse_column_names_handles_one_per_line():
    assert parse_column_names("a\nb\nc") == ["a", "b", "c"]


def test_parse_column_names_handles_comma_separated():
    assert parse_column_names("a, b, c") == ["a", "b", "c"]


def test_parse_column_names_strips_quotes_and_dedupes():
    assert parse_column_names('"a",\'b\',a\nb\nc') == ["a", "b", "c"]


def test_parse_column_names_handles_empty_input():
    assert parse_column_names("") == []
    assert parse_column_names("   \n  ") == []


def test_render_sql_with_warnings_highlights_todo_lines_only():
    html_out = render_sql_with_warnings("SELECT 1\nNULL /* TODO: fix */ AS x\nFROM t")
    assert html_out.count("⚠️") == 1
    assert "SELECT 1" in html_out
    assert "FROM t" in html_out


def test_render_sql_with_warnings_escapes_html_special_characters():
    html_out = render_sql_with_warnings("SELECT '<script>' AS x")
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out
