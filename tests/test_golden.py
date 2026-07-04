"""Golden-file regression tests.

Pins the exact generated output for the real flow committed in
`Tableau Prep Flows/`. Any change to tfl_to_sql.py that alters output —
intentional or not — fails this test, so refactors have to explicitly
re-approve the new output (regenerate with UPDATE_GOLDEN=1) rather than
silently drifting.

Split+dataform mode is intentionally not golden-tested here: its config-block
branch is exercised by test_cli.py, and doubling the fixture set (another 45
files) for that alone wasn't worth it — see README for the full test-scope
rationale.
"""

import collections
import os

import pytest
import tfl_to_sql as t
from conftest import FLOWS_DIR, GOLDEN_DIR

FLOW_PATH = FLOWS_DIR / "FST Segmentation Prep.tfl"
UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


def _load_flow():
    flow = t.load_tfl(str(FLOW_PATH))
    nodes = t.extract_nodes(flow)
    edges = t.extract_edges(nodes)
    order = t.topological_sort(nodes, edges)
    parents_map = collections.defaultdict(list)
    for src, dst, ns in edges:
        parents_map[dst].append((src, ns))
    return nodes, order, parents_map


def _compare_or_update(path, actual: str):
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual + "\n", encoding="utf-8")
        return
    assert path.exists(), f"missing golden fixture {path} (run with UPDATE_GOLDEN=1 to create it)"
    expected = path.read_text(encoding="utf-8")
    assert actual + "\n" == expected, f"generated output for {path.name} no longer matches the golden fixture"


@pytest.mark.parametrize("mode,subdir,ext", [
    ("bigquery", "combined_bigquery", ".sql"),
    ("dataform", "combined_dataform", ".sqlx"),
])
def test_combined_output_matches_golden(mode, subdir, ext):
    nodes, order, parents_map = _load_flow()
    results = t.build_combined(nodes, order, parents_map, mode, "my_dataset", str(FLOW_PATH))
    assert len(results) == 1
    filename, sql = results[0]
    assert filename.endswith(ext)
    _compare_or_update(GOLDEN_DIR / subdir / filename, sql)


def test_split_bigquery_output_matches_golden():
    nodes, order, parents_map = _load_flow()
    node_columns = t.compute_node_columns(nodes, order, parents_map)

    for idx, nid in enumerate(order, start=1):
        node = nodes[nid]
        category, _raw = t.classify_node(node)
        filename = f"{idx:02d}_{t.sanitise(node.get('name', nid))}.sql"
        raw_pairs = parents_map.get(nid, [])
        parent_pairs = [(t.sanitise(nodes[pid].get("name", pid)), ns)
                        for pid, ns in raw_pairs if pid in nodes]

        if category == "JOIN":
            by_ns = {ns: pid for pid, ns in raw_pairs}
            known_cols = (node_columns.get(by_ns.get("Left")), node_columns.get(by_ns.get("Right")))
        elif category == "CLEAN":
            known_cols = node_columns.get(raw_pairs[0][0]) if raw_pairs else None
        else:
            known_cols = None

        assert category is not None, f"{filename} has an unrecognised node type"
        sql = t.SPLIT_GENERATORS[category](node, parent_pairs, "bigquery", "my_dataset", known_cols)
        _compare_or_update(GOLDEN_DIR / "split_bigquery" / filename, sql)


def test_no_residual_tableau_syntax_in_golden_output():
    """A cheap structural safety net independent of the golden fixtures
    themselves: whatever the current output is, it must never contain raw
    Tableau syntax that would make it invalid SQL."""
    nodes, order, parents_map = _load_flow()
    _filename, sql = t.build_combined(nodes, order, parents_map, "bigquery", "my_dataset", str(FLOW_PATH))[0]

    import re

    assert "IIF(" not in sql
    assert "ZN(" not in sql
    assert "{{" not in sql
    assert sql.count("(") == sql.count(")")
    assert sql.count("`") % 2 == 0
    # Tableau's IF <cond> THEN ... END never legitimately survives translation
    # (BigQuery's IF is a 3-arg function, "IF("); a bare "IF ... THEN" is a miss.
    assert not re.search(r"\bIF\b(?!\()[^\n]*\bTHEN\b", sql)
