"""Tests for UNPIVOT node support — found missing entirely while batch-testing
against a real production flow collection (Tableau's SuperUnpivotExtended)."""

import tfl_to_sql as t


def _unpivot_node(source_cols, values_col_name="Pivot1 Values", names_col_name="Pivot1 Names",
                   before=None, after=None):
    return {
        "nodeType": ".v2018_3_4.SuperUnpivotExtended",
        "name": "Pivot 1",
        "baseType": "superNode",
        "beforeActionAnnotations": before or [],
        "afterActionAnnotations": after or [],
        "actionNode": {
            "nodeType": ".v2018_3_4.UnpivotExtended",
            "unpivotGroup": {
                "literalColumn": {"literalColumnName": names_col_name, "names": source_cols},
                "unpivotColumns": [{"unpivotColumnName": values_col_name,
                                    "columnInformation": {"manualBindings": source_cols}}],
            },
        },
    }


def test_classify_node_recognises_super_unpivot_extended():
    node = _unpivot_node(["DM Cold", "DM House"])
    cat, _raw = t.classify_node(node)
    assert cat == "UNPIVOT"


def test_cte_unpivot_generates_bigquery_unpivot_syntax():
    node = _unpivot_node(["DM Cold", "DM House", "Awin"])
    sql = t._cte_unpivot(node, "source_table")
    assert "FROM source_table" in sql
    assert "UNPIVOT(" in sql
    assert "`PIVOT1_VALUES` FOR `PIVOT1_NAMES` IN (`DM_COLD`, `DM_HOUSE`, `AWIN`)" in sql


def test_cte_unpivot_applies_after_action_renames():
    after = [
        {"namespace": "Default", "annotationNode": {
            "nodeType": ".v1.RenameColumn", "columnName": "Pivot1 Values", "rename": "Total Cost"}},
        {"namespace": "Default", "annotationNode": {
            "nodeType": ".v1.RenameColumn", "columnName": "Pivot1 Names", "rename": "Media Type"}},
    ]
    node = _unpivot_node(["DM Cold", "DM House"], after=after)
    sql = t._cte_unpivot(node, "source_table")
    assert "`PIVOT1_VALUES` AS `TOTAL_COST`" in sql
    assert "`PIVOT1_NAMES` AS `MEDIA_TYPE`" in sql


def test_cte_unpivot_missing_info_flags_instead_of_crashing():
    node = {"nodeType": ".v2018_3_4.SuperUnpivotExtended", "name": "Broken Unpivot", "actionNode": {}}
    sql = t._cte_unpivot(node, "source_table")
    assert "TODO" in sql
    assert any("UNPIVOT node missing" in kind for kind, _ in t.PARSE_WARNINGS)


def test_compute_node_columns_tracks_unpivot_output_columns():
    nodes = {
        "agg": {"nodeType": ".v1.SuperAggregate", "baseType": "superNode", "nextNodes": [{"nextNodeId": "unpivot"}],
                "actionNode": {"groupByFields": [{"columnName": "customer_id"}],
                               "aggregateFields": [{"columnName": "DM Cold"}, {"columnName": "DM House"}]}},
        "unpivot": _unpivot_node(["DM Cold", "DM House"]),
    }
    nodes["unpivot"]["nextNodes"] = []
    parents_map = {"unpivot": [("agg", "Default")]}
    order = ["agg", "unpivot"]
    node_columns = t.compute_node_columns(nodes, order, parents_map)
    assert node_columns["agg"] == {"customer_id", "DM Cold", "DM House"}
    assert node_columns["unpivot"] == {"customer_id", "Pivot1 Names", "Pivot1 Values"}
