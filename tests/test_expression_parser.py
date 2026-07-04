"""Unit tests for the Tableau calc tokenizer/parser/emitter (translate_expr).

Each test pins one piece of the function-mapping table in the handover spec,
plus the structural cases (nesting, IF/CASE, LOD, comments, bare fields) that
the old flat-regex implementation couldn't handle at all.

Field names render UPPER_CASE and underscore-separated in generated SQL (see
_col_id) — that's cosmetic (BigQuery resolves columns case-insensitively) but
applied consistently everywhere a field name becomes SQL text, so every
expected value below uses the canonical upper-case form. Tableau Parameters
and --overrides values are the two exceptions: neither is a real column name,
so both stay exactly as written.
"""

import tfl_to_sql as t


def translate(expr):
    return t.translate_expr(expr)


# ---------------------------------------------------------------------------
# Function mapping table (handover.md Step 6)
# ---------------------------------------------------------------------------

def test_zn_maps_to_coalesce():
    assert translate("ZN([x])") == "COALESCE(`X`, 0)"


def test_isnull_maps_to_is_null():
    assert translate("ISNULL([x])") == "`X` IS NULL"


def test_ifnull_maps_to_coalesce():
    assert translate("IFNULL([x], [y])") == "COALESCE(`X`, `Y`)"


def test_iif_maps_to_if():
    assert translate("IIF([cond], 1, 0)") == "IF(`COND`, 1, 0)"


def test_dateparse_maps_to_parse_date():
    assert translate("DATEPARSE('%Y-%m-%d', [x])") == "PARSE_DATE('%Y-%m-%d', `X`)"


def test_datetrunc_two_arg():
    assert translate("DATETRUNC('month', [x])") == "DATE_TRUNC(`X`, MONTH)"


def test_datetrunc_week_start_day():
    assert translate("DATETRUNC('week', [x], 'monday')") == "DATE_TRUNC(`X`, WEEK(MONDAY))"


def test_datediff_swaps_arg_order_to_bigquery_convention():
    assert translate("DATEDIFF('day', [a], [b])") == "DATE_DIFF(`B`, `A`, DAY)"


def test_dateadd_supports_double_quoted_unit():
    # Regression: the old regex only matched single-quoted units.
    assert translate('DATEADD("week", -(13), [x])') == "DATE_ADD(`X`, INTERVAL -(13) WEEK)"


def test_today_and_now():
    assert translate("TODAY()") == "CURRENT_DATE()"
    assert translate("NOW()") == "CURRENT_TIMESTAMP()"


def test_left_maps_to_substr():
    assert translate("LEFT([x], 3)") == "SUBSTR(`X`, 1, 3)"


def test_contains_maps_to_strpos():
    assert translate("CONTAINS([x], 'y')") == "STRPOS(`X`, 'y') > 0"


def test_int_float_str_cast():
    assert translate("INT([x])") == "CAST(`X` AS INT64)"
    assert translate("FLOAT([x])") == "CAST(`X` AS FLOAT64)"
    assert translate("STR([x])") == "CAST(`X` AS STRING)"


def test_countd_and_stdev():
    assert translate("COUNTD([x])") == "COUNT(DISTINCT `X`)"
    assert translate("STDEV([x])") == "STDDEV(`X`)"


def test_unmapped_function_passes_through():
    assert translate("SUM([x])") == "SUM(`X`)"
    assert translate("ABS([x])") == "ABS(`X`)"


# ---------------------------------------------------------------------------
# Structural cases the old regex translator could not handle
# ---------------------------------------------------------------------------

def test_nested_function_calls_at_any_depth():
    # Old regex used [^)]+ and broke on nested parens.
    assert translate("INT(REGEXP_EXTRACT([x], 'P(\\d+)'))") == r"CAST(REGEXP_EXTRACT(`X`, 'P(\d+)') AS INT64)"


def test_if_then_else_end_becomes_case_when():
    result = translate("IF [x] > 0 THEN 1 ELSE 0 END")
    assert result == "CASE\nWHEN `X` > 0 THEN 1\nELSE 0\nEND"


def test_if_elseif_chain():
    result = translate("IF [x] = 1 THEN 'a' ELSEIF [x] = 2 THEN 'b' ELSE 'c' END")
    assert "WHEN `X` = 1 THEN 'a'" in result
    assert "WHEN `X` = 2 THEN 'b'" in result
    assert "ELSE 'c'" in result
    assert result.startswith("CASE") and result.endswith("END")


def test_if_without_else_omits_else_clause():
    result = translate("IF [x] > 0 THEN 1 END")
    assert "ELSE" not in result


def test_case_when_passthrough():
    result = translate("CASE [x] WHEN 1 THEN 'a' ELSE 'b' END")
    assert result == "CASE `X`\nWHEN 1 THEN 'a'\nELSE 'b'\nEND"


def test_lod_fixed_becomes_window_function():
    result = translate("{FIXED [customer_id]:MAX([customer_email])}")
    assert result == "MAX(`CUSTOMER_EMAIL`) OVER (PARTITION BY `CUSTOMER_ID`)"


def test_lod_fixed_handles_doubled_braces():
    # Real-world flows sometimes emit {{ FIXED ... }} with doubled braces.
    result = translate("{{ FIXED [customer_id]:MAX([customer_email])}}")
    assert result == "MAX(`CUSTOMER_EMAIL`) OVER (PARTITION BY `CUSTOMER_ID`)"


def test_lod_include_exclude_flagged_not_guessed():
    result = translate("{INCLUDE [x]: SUM([y])}")
    assert result.startswith("NULL /* TODO")
    assert len(t.PARSE_WARNINGS) == 1


def test_bare_unbracketed_field_reference():
    # Tableau allows referencing a field without [brackets] when unambiguous.
    assert translate("fy_q1_demand / total_lifetime_demand") == "`FY_Q1_DEMAND` / `TOTAL_LIFETIME_DEMAND`"


def test_line_comments_become_sql_comments_above_expression():
    result = translate("// a note\n[x] + 1")
    assert result == "-- a note\n`X` + 1"


def test_unparseable_expression_never_raises():
    result = translate("[x] +)")
    assert result.startswith("NULL /* TODO: could not parse expression")
    assert len(t.PARSE_WARNINGS) == 1


def test_parameter_reference_flagged_by_default():
    # Parameters aren't real column names, so this is the one field-like
    # reference that is deliberately NOT upper-cased — it needs to match
    # --overrides' "Parameters.<id>" keys exactly, verbatim.
    result = translate("[Parameters.abc-123] + 1")
    assert "`Parameters.abc-123`" in result
    assert any(kind.startswith("parameter reference") for kind, _ in t.PARSE_WARNINGS)


def test_parameter_override_resolves_without_warning():
    t.OVERRIDES["parameters"]["Parameters.abc-123"] = "52"
    result = translate("[Parameters.abc-123] + 1")
    assert result == "52 + 1"
    assert not any(kind.startswith("parameter reference") for kind, _ in t.PARSE_WARNINGS)


def test_whole_expression_override_short_circuits_parsing():
    raw = "some totally invalid ((( expression"
    t.OVERRIDES["expressions"][raw] = "`fixed_value`"
    assert translate(raw) == "`fixed_value`"  # override value used verbatim, never transformed
    assert not t.PARSE_WARNINGS


# ---------------------------------------------------------------------------
# Tableau Prep's {PARTITION dims: {ORDERBY cols: FUNC()}} window-calc syntax
# (found in real production flows — duplicate-row detection, sequencing)
# ---------------------------------------------------------------------------

def test_partition_orderby_row_number():
    result = translate("{{ PARTITION [sku]: { ORDERBY [id] DESC: ROW_NUMBER() } }}")
    assert result == "ROW_NUMBER() OVER (PARTITION BY `SKU` ORDER BY `ID` DESC)"


def test_partition_orderby_multi_dim_multi_sort():
    result = translate("{{ PARTITION [a], [b]: { ORDERBY [x] ASC, [y] DESC: RANK() } }}")
    assert result == "RANK() OVER (PARTITION BY `A`, `B` ORDER BY `X` ASC, `Y` DESC)"


def test_partition_orderby_lookup_negative_offset_maps_to_lag():
    result = translate("{{ PARTITION [id]: { ORDERBY [d] ASC: LOOKUP([d], -1) } }}")
    assert result == "LAG(`D`, 1) OVER (PARTITION BY `ID` ORDER BY `D` ASC)"


def test_partition_orderby_lookup_positive_offset_maps_to_lead():
    result = translate("{{ PARTITION [id]: { ORDERBY [d] ASC: LOOKUP([d], 1) } }}")
    assert result == "LEAD(`D`, 1) OVER (PARTITION BY `ID` ORDER BY `D` ASC)"


def test_partition_block_embedded_in_larger_comparison():
    # Regression: single-brace nesting inside a larger expression used to eat
    # the wrong closing braces and merge trailing tokens into the block.
    result = translate("IF ({PARTITION [sku]: {ORDERBY [id] DESC: ROW_NUMBER()}} = 1) THEN 'Unique' ELSE 'Dup' END")
    assert result == (
        "CASE\nWHEN (ROW_NUMBER() OVER (PARTITION BY `SKU` ORDER BY `ID` DESC) = 1) "
        "THEN 'Unique'\nELSE 'Dup'\nEND"
    )
    assert not t.PARSE_WARNINGS


def test_orderby_outside_partition_is_flagged_not_guessed():
    result = translate("{ORDERBY [x] ASC: SUM([y])}")
    assert result.startswith("NULL /* TODO")
    assert len(t.PARSE_WARNINGS) == 1


# ---------------------------------------------------------------------------
# Tableau's #yyyy-mm-dd# date literal syntax
# ---------------------------------------------------------------------------

def test_hash_date_literal_inside_date_function():
    assert translate("DATE(#2027-05-01#)") == "DATE('2027-05-01')"


def test_hash_datetime_literal():
    assert translate("#2022-05-01 10:30:00#") == "'2022-05-01 10:30:00'"


# ---------------------------------------------------------------------------
# Leading-dot decimal literals (.6 == 0.6)
# ---------------------------------------------------------------------------

def test_leading_dot_decimal_literal():
    assert translate("[x] * .6") == "`X` * .6"
