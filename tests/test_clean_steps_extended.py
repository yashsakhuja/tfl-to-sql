"""Tests for clean-step types found while batch-testing against a real
production flow collection: ChangeColumnType, Remap, RichRangeFilter, and
RichDiscreteValueFilter. Synthetic containers here, not the real flows
(which aren't checked into this repo) — same shape, hand-built.
"""

import tfl_to_sql as t


def _container(sub_nodes: dict) -> dict:
    """Wrap sub-nodes into a minimal CLEAN container, chaining them via
    nextNodes the way _ordered_container_steps expects."""
    ids = list(sub_nodes)
    for i, sid in enumerate(ids):
        nxt = [{"nextNodeId": ids[i + 1]}] if i + 1 < len(ids) else []
        sub_nodes[sid] = {**sub_nodes[sid], "nextNodes": nxt}
    return {"loomContainer": {"nodes": sub_nodes}}


def test_change_column_type_with_calc_uses_the_calc_expression():
    node = _container({
        "1": {"nodeType": ".v1.ChangeColumnType", "fields": {
            "DATE_CREATED": {"type": "date", "calc": "DATE([DATE_CREATED])"},
        }},
    })
    steps, _cols = t._extract_clean_steps(node)
    assert steps == [("replace", "DATE_CREATED", "DATE(`DATE_CREATED`)")]


def test_change_column_type_without_calc_falls_back_to_cast():
    node = _container({
        "1": {"nodeType": ".v1.ChangeColumnType", "fields": {
            "amount": {"type": "real"},
        }},
    })
    steps, _cols = t._extract_clean_steps(node)
    assert steps == [("replace", "amount", "CAST(`AMOUNT` AS FLOAT64)")]


def test_change_column_type_maps_all_known_tableau_types():
    fields = {
        "a": {"type": "integer"}, "b": {"type": "string"},
        "c": {"type": "datetime"}, "d": {"type": "boolean"},
    }
    node = _container({"1": {"nodeType": ".v1.ChangeColumnType", "fields": fields}})
    steps, _cols = t._extract_clean_steps(node)
    casts = {col: sql for _, col, sql in steps}
    assert casts["a"] == "CAST(`A` AS INT64)"
    assert casts["b"] == "CAST(`B` AS STRING)"
    assert casts["c"] == "CAST(`C` AS DATETIME)"
    assert casts["d"] == "CAST(`D` AS BOOL)"


def test_remap_groups_values_into_case_when():
    node = _container({
        "1": {
            "nodeType": ".v2019_1_4.Remap",
            "columnName": "Channel",
            "values": {
                '"3rd Party"': ['"3rd Party"', '"Affiliate"'],
                '"Trade UK"': ['"Trade UK"', '"UK Wholesale"'],
            },
        },
    })
    steps, _cols = t._extract_clean_steps(node)
    assert len(steps) == 1
    kind, col, sql = steps[0]
    assert kind == "replace" and col == "Channel"
    assert "WHEN `CHANNEL` IN ('3rd Party', 'Affiliate') THEN '3rd Party'" in sql
    assert "WHEN `CHANNEL` IN ('Trade UK', 'UK Wholesale') THEN 'Trade UK'" in sql
    assert sql.strip().startswith("CASE") and sql.strip().endswith("END")
    assert "ELSE `CHANNEL`" in sql


def test_rich_range_filter_date_bounds_from_epoch_millis():
    node = _container({
        "1": {
            "nodeType": ".v1.RichRangeFilter",
            "columnName": "INVOICE_DATE",
            "dataType": "date",
            "appliedMin": "1651363200000",
            "appliedMax": "1767225600000",
            "includeNulls": False,
        },
    })
    steps, _cols = t._extract_clean_steps(node)
    assert steps == [("filter",
        "`INVOICE_DATE` BETWEEN DATE(TIMESTAMP_MILLIS(CAST(1651363200000 AS INT64))) "
        "AND DATE(TIMESTAMP_MILLIS(CAST(1767225600000 AS INT64)))")]


def test_rich_range_filter_include_nulls_ors_in_is_null():
    node = _container({
        "1": {
            "nodeType": ".v1.RichRangeFilter",
            "columnName": "amount",
            "dataType": "real",
            "appliedMin": "0",
            "appliedMax": "100",
            "includeNulls": True,
        },
    })
    steps, _cols = t._extract_clean_steps(node)
    kind, sql = steps[0]
    assert sql == "(`AMOUNT` BETWEEN 0 AND 100 OR `AMOUNT` IS NULL)"


def test_rich_discrete_value_filter_reuses_valuefilter_logic():
    node = _container({
        "1": {
            "nodeType": ".v2019_3_2.RichDiscreteValueFilter",
            "exclude": False,
            "values": {"FINAL_DELIVERY": ['"0"']},
        },
    })
    steps, _cols = t._extract_clean_steps(node)
    assert steps == [("filter", "`FINAL_DELIVERY` IN ('0')")]
