"""Tests for the schema-awareness (--schema) and overrides (--overrides)
mechanisms — Phase 2/3 of the platform roadmap.
"""

import json

import tfl_to_sql as t


def test_apply_steps_to_columns_tracks_rename_add_remove_keep():
    cols = {"a", "b", "c"}
    steps = [("rename", "a", "a2"), ("add", "d", "expr"), ("remove", ["b"])]
    result = t._apply_steps_to_columns(cols, steps)
    assert result == {"a2", "c", "d"}


def test_apply_steps_to_columns_keep_restricts_to_list():
    cols = {"a", "b", "c"}
    result = t._apply_steps_to_columns(cols, [("keep", ["a", "c"])])
    assert result == {"a", "c"}


def test_apply_steps_to_columns_none_stays_none():
    assert t._apply_steps_to_columns(None, [("add", "x")]) is None


def test_resolve_bulk_rename_substring_operation():
    sub_node = {
        "columnsSelection": {"exemptedColumns": ["id"]},
        "columnsOperation": {
            "type": "replaceColumnAllSubStringOperation",
            "existingSubString": " ",
            "newSubString": "_",
        },
    }
    pairs = t._resolve_bulk_rename(sub_node, {"order id", "ship date", "id"})
    assert set(pairs) == {("order id", "order_id"), ("ship date", "ship_date")}


def test_resolve_bulk_rename_prefix_and_suffix_operations():
    prefix_node = {"columnsSelection": {}, "columnsOperation": {"type": "addColumnPrefixOperation", "columnNamePrefix": "cat_"}}
    assert t._resolve_bulk_rename(prefix_node, {"bags"}) == [("bags", "cat_bags")]

    suffix_node = {"columnsSelection": {}, "columnsOperation": {"type": "addColumnSuffixOperation", "columnNameSuffix": "_demand"}}
    assert t._resolve_bulk_rename(suffix_node, {"bags"}) == [("bags", "bags_demand")]


def test_resolve_bulk_rename_unknown_operation_type_returns_empty():
    sub_node = {"columnsSelection": {}, "columnsOperation": {"type": "someFutureOperation"}}
    assert t._resolve_bulk_rename(sub_node, {"a", "b"}) == []


def test_resolve_bulk_rename_returns_empty_when_cols_unknown():
    sub_node = {"columnsSelection": {}, "columnsOperation": {"type": "addColumnPrefixOperation", "columnNamePrefix": "x_"}}
    assert t._resolve_bulk_rename(sub_node, None) == []


def test_load_schema_populates_schema_dict(tmp_path):
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"orders": {"columns": ["order_id", "customer_id"]}}))
    t.load_schema(str(schema_file))
    assert t.SCHEMA["orders"] == ["order_id", "customer_id"]


def test_load_schema_accepts_bare_list_form(tmp_path):
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({"orders": ["order_id", "customer_id"]}))
    t.load_schema(str(schema_file))
    assert t.SCHEMA["orders"] == ["order_id", "customer_id"]


def test_load_overrides_populates_all_three_categories(tmp_path):
    overrides_file = tmp_path / "overrides.json"
    overrides_file.write_text(json.dumps({
        "expressions": {"bad calc": "`fixed`"},
        "parameters": {"Parameters.x": "42"},
        "bulk_renames": {"Rename Fields 1": [["a", "b"]]},
    }))
    t.load_overrides(str(overrides_file))
    assert t.OVERRIDES["expressions"]["bad calc"] == "`fixed`"
    assert t.OVERRIDES["parameters"]["Parameters.x"] == "42"
    assert t.OVERRIDES["bulk_renames"]["Rename Fields 1"] == [["a", "b"]]


def test_cte_join_full_dedup_when_both_sides_known():
    node = {
        "actionNode": {
            "joinType": "inner",
            "joinExpressions": [{"leftExpression": "[id]", "rightExpression": "[id]"}],
        }
    }
    sql = t._cte_join(node, "left_t", "right_t", left_cols={"id", "name"}, right_cols={"id", "name", "extra"})
    assert "r.* EXCEPT (`ID`, `NAME`)" in sql
    assert "TODO: verify no other duplicate" not in sql


def test_cte_join_falls_back_to_key_only_dedup_when_unknown():
    node = {
        "actionNode": {
            "joinType": "inner",
            "joinExpressions": [{"leftExpression": "[id]", "rightExpression": "[id]"}],
        }
    }
    sql = t._cte_join(node, "left_t", "right_t")
    assert "r.* EXCEPT (`ID`)" in sql
    assert "TODO: verify no other duplicate" in sql


def test_extract_clean_steps_flags_rename_of_unknown_column():
    node = {
        "loomContainer": {
            "nodes": {
                "1": {
                    "nodeType": ".v1.RenameColumn",
                    "columnName": "does_not_exist",
                    "rename": "new_name",
                    "nextNodes": [],
                },
            }
        }
    }
    steps, cols = t._extract_clean_steps(node, known_cols={"real_col"})
    assert steps == [("rename", "does_not_exist", "new_name")]
    assert cols == {"real_col", "new_name"}  # tracking still proceeds structurally
    assert any("not found in tracked schema" in kind for kind, _ in t.PARSE_WARNINGS)
