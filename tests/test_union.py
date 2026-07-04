"""Tests for UNION column alignment.

BigQuery's UNION aligns branches by ordinal *position*, not by column name,
so a naive `SELECT * ... UNION ALL SELECT * ...` across branches with
different columns either errors on a column-count mismatch or — worse —
silently pairs up columns that don't mean the same thing. _cte_union builds
one explicit, name-aligned column list shared by every branch, NULL-filling
whichever columns a given branch doesn't have, whenever the column sets are
known. But every UNION is flagged for a manual check every time regardless
— name-matching alone can't catch a data-type mismatch under the same
column name, so this is the one node type worth a human's eyes on every
run, not just when the tool is unsure.
"""

import tfl_to_sql as t

NODE = {"actionNode": {"unionType": "all"}, "name": "My Union"}


def test_union_aligns_columns_by_name_and_null_fills_gaps():
    sql = t._cte_union(NODE, ["branch_a", "branch_b"],
                        [{"id", "name", "amount"}, {"id", "name", "region"}])

    branch_a_sql, branch_b_sql = sql.split("UNION ALL")

    # Same four columns, same order, in both branches.
    assert "`AMOUNT`" in branch_a_sql and "`ID`" in branch_a_sql
    assert "`NAME`" in branch_a_sql and "NULL AS `REGION`" in branch_a_sql
    assert "NULL AS `AMOUNT`" in branch_b_sql and "`REGION`" in branch_b_sql

    # Same column *order* in both branches, even though the content of a
    # given position differs (a real column on one side, NULL on the other).
    import re

    def col_order(select_sql):
        return re.findall(r"`(\w+)`", select_sql)
    assert col_order(branch_a_sql) == col_order(branch_b_sql) == ["AMOUNT", "ID", "NAME", "REGION"]


def test_union_with_identical_columns_has_no_null_placeholders():
    sql = t._cte_union(NODE, ["branch_a", "branch_b"], [{"id", "name"}, {"id", "name"}])
    assert "NULL AS" not in sql


def test_union_is_always_flagged_even_when_auto_aligned():
    sql = t._cte_union(NODE, ["branch_a", "branch_b"], [{"id", "name"}, {"id", "name"}])
    assert "TODO" in sql
    assert any(kind == "UNION — test and verify every time" for kind, _ in t.PARSE_WARNINGS)


def test_union_falls_back_and_flags_when_any_branch_columns_unknown():
    sql = t._cte_union(NODE, ["branch_a", "branch_b"], [None, {"id", "name"}])
    assert "SELECT * FROM branch_a" in sql
    assert "SELECT * FROM branch_b" in sql
    assert "TODO" in sql
    assert any(kind == "UNION — test and verify every time" for kind, _ in t.PARSE_WARNINGS)


def test_union_falls_back_when_no_column_info_supplied_at_all():
    sql = t._cte_union(NODE, ["branch_a", "branch_b"])
    assert "SELECT * FROM branch_a" in sql
    assert "TODO" in sql
    assert any(kind == "UNION — test and verify every time" for kind, _ in t.PARSE_WARNINGS)


def test_union_distinct_keyword_used_when_configured():
    node = {"actionNode": {"unionType": "distinct"}, "name": "U"}
    sql = t._cte_union(node, ["a", "b"], [{"x"}, {"x"}])
    assert "UNION DISTINCT" in sql
    assert "UNION ALL" not in sql


def test_union_no_upstream_nodes_placeholder():
    assert t._cte_union(NODE, []) == "-- TODO: no upstream nodes found"


def test_compute_node_columns_unions_all_branch_columns_not_just_first():
    nodes = {
        "a": {"nodeType": ".v1.SuperAggregate", "baseType": "superNode", "nextNodes": [{"nextNodeId": "u"}],
              "actionNode": {"groupByFields": [{"columnName": "id"}],
                             "aggregateFields": [{"columnName": "amount"}]}},
        "b": {"nodeType": ".v1.SuperAggregate", "baseType": "superNode", "nextNodes": [{"nextNodeId": "u"}],
              "actionNode": {"groupByFields": [{"columnName": "id"}],
                             "aggregateFields": [{"columnName": "region"}]}},
        "u": {"nodeType": "superunion", "baseType": "superNode", "nextNodes": [],
              "actionNode": {"unionType": "all"}},
    }
    parents_map = {"u": [("a", "Default"), ("b", "Default")]}
    order = ["a", "b", "u"]
    node_columns = t.compute_node_columns(nodes, order, parents_map)
    assert node_columns["u"] == {"id", "amount", "region"}
