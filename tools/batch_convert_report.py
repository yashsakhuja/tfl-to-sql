#!/usr/bin/env python3
"""batch_convert_report.py — Run the converter against every .tfl in a folder
and score each one: did it crash, is the output structurally valid SQL, and
what's the expression-level translation accuracy (see
tfl_to_sql.build_review_report). Built for regression-testing the converter
against a real collection of production flows, not just the one sample flow
checked into the repo.

Usage:
    python tools/batch_convert_report.py "Test Collection" \\
        --mode bigquery --out-root /tmp/batch_out --min-accuracy 95 \\
        --json /tmp/batch_report.json

Exit code is non-zero if any flow crashed, produced structurally invalid
SQL, or fell below --min-accuracy — so this can gate CI once a --schema/
--overrides pair is established for the real flows it's pointed at.
"""

import argparse
import collections
import json
import re
import sys
import traceback
from pathlib import Path

_CODE_DIR = Path(__file__).parent.parent / "Code"
sys.path.insert(0, str(_CODE_DIR))

import tfl_to_sql as t  # noqa: E402

_RAW_TABLEAU_TOKENS = ("IIF(", "ZN(", "ISNULL(", "{{")
_BARE_IF_THEN_RE = re.compile(r"\bIF\b(?!\()[^\n]*\bTHEN\b")
_BRACKET_FIELD_RE = re.compile(r"\[[A-Za-z]")
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _strip_string_literals(sql: str) -> str:
    """Real data can legitimately contain a bare '(' or unmatched quote
    character inside a string literal (e.g. splitting text on a literal
    ' (' substring) — checks below care about SQL *structure*, so string
    contents need to be blanked out first or those are false positives."""
    return _STRING_LITERAL_RE.sub("''", sql)


def structural_issues(sql: str) -> list:
    """Cheap, mechanical checks that don't need BigQuery: is this even
    plausible SQL, independent of whether it happens to match a golden file."""
    issues = []
    code_only = _strip_string_literals(sql)
    for token in _RAW_TABLEAU_TOKENS:
        if token in code_only:
            issues.append(f"residual raw Tableau token {token!r}")
    if code_only.count("(") != code_only.count(")"):
        issues.append(f"unbalanced parens ({code_only.count('(')} open, {code_only.count(')')} close)")
    if code_only.count("`") % 2 != 0:
        issues.append("odd number of backticks")
    if _BARE_IF_THEN_RE.search(code_only):
        issues.append("bare Tableau IF...THEN survived translation")
    for line in code_only.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        if _BRACKET_FIELD_RE.search(line):
            issues.append(f"residual [Bracket] field reference: {line.strip()[:80]!r}")
            break
    return issues


def _reset_engine_state():
    t.PARSE_WARNINGS.clear()
    t.OVERRIDES["expressions"].clear()
    t.OVERRIDES["parameters"].clear()
    t.OVERRIDES["bulk_renames"].clear()
    t.SCHEMA.clear()
    t.TRANSLATE_ATTEMPTS = 0


def convert_one(flow_path: Path, mode: str) -> dict:
    """Never raises — any exception becomes {"crashed": True, ...}."""
    _reset_engine_state()
    result = {
        "flow": flow_path.name,
        "crashed": False,
        "crash_message": None,
        "node_count": 0,
        "category_counts": {},
        "unrecognised_nodes": [],
        "structural_issues_by_file": {},
        "files": {},
        "report": None,
    }
    try:
        flow = t.load_tfl(str(flow_path))
        nodes = t.extract_nodes(flow)
        if not nodes:
            raise RuntimeError("no nodes found in flow")
        result["node_count"] = len(nodes)

        cat_counts = collections.Counter()
        unrecognised = []
        for node in nodes.values():
            cat, raw_type = t.classify_node(node)
            cat_counts[cat or "UNKNOWN"] += 1
            if cat is None:
                unrecognised.append(f'"{node.get("name", "?")}" (type: {raw_type})')
        result["category_counts"] = dict(cat_counts)
        result["unrecognised_nodes"] = unrecognised

        edges = t.extract_edges(nodes)
        order = t.topological_sort(nodes, edges)
        parents_map: dict = collections.defaultdict(list)
        for src, dst, ns in edges:
            parents_map[dst].append((src, ns))

        results = t.build_combined(nodes, order, parents_map, mode, "my_dataset", str(flow_path))
        for filename, sql in results:
            result["files"][filename] = sql
            issues = structural_issues(sql)
            if issues:
                result["structural_issues_by_file"][filename] = issues

        result["report"] = t.build_review_report(unrecognised)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: this is a crash probe
        result["crashed"] = True
        result["crash_message"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("flows_dir", help="Folder of .tfl files (searched non-recursively)")
    parser.add_argument("--mode", choices=["bigquery", "dataform"], default="bigquery")
    parser.add_argument("--out-root", help="If set, write each flow's generated SQL under <out-root>/<flow>/")
    parser.add_argument("--min-accuracy", type=float, default=95.0,
                        help="Expression-level accuracy threshold to pass (default: 95.0)")
    parser.add_argument("--json", metavar="PATH", help="Write the full per-flow report as JSON")
    args = parser.parse_args()

    flows_dir = Path(args.flows_dir)
    flow_paths = sorted(flows_dir.glob("*.tfl"))
    if not flow_paths:
        raise SystemExit(f"ERROR: no .tfl files found in {args.flows_dir}")

    all_results = []
    for flow_path in flow_paths:
        print(f"Converting {flow_path.name} ...", end=" ", flush=True)
        res = convert_one(flow_path, args.mode)
        all_results.append(res)

        if res["crashed"]:
            print(f"CRASHED: {res['crash_message']}")
            continue

        pct = res["report"]["summary"]["expression_accuracy_pct"]
        n_issues = sum(len(v) for v in res["structural_issues_by_file"].values())
        flag = "OK" if pct >= args.min_accuracy and not n_issues else "BELOW TARGET"
        print(f"{pct:.1f}% expression accuracy, {n_issues} structural issue(s) [{flag}]")

        if args.out_root:
            out_dir = Path(args.out_root) / flow_path.stem
            out_dir.mkdir(parents=True, exist_ok=True)
            for filename, sql in res["files"].items():
                (out_dir / filename).write_text(sql + "\n", encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"{'FLOW':<55} {'NODES':>6} {'ACCURACY':>9} {'FLAGGED':>8} {'STRUCT':>7} {'STATUS':>8}")
    print("-" * 100)
    n_crash = n_below = n_struct = 0
    accuracies = []
    for res in all_results:
        if res["crashed"]:
            print(f"{res['flow']:<55} {'--':>6} {'--':>9} {'--':>8} {'--':>7} {'CRASH':>8}")
            n_crash += 1
            continue
        pct = res["report"]["summary"]["expression_accuracy_pct"]
        flagged = res["report"]["summary"]["flagged_item_count"]
        n_issues = sum(len(v) for v in res["structural_issues_by_file"].values())
        accuracies.append(pct)
        ok = pct >= args.min_accuracy and not n_issues and not res["unrecognised_nodes"]
        if not ok:
            n_below += 1
        if n_issues:
            n_struct += 1
        status = "OK" if ok else "REVIEW"
        print(f"{res['flow']:<55} {res['node_count']:>6} {pct:>8.1f}% {flagged:>8} {n_issues:>7} {status:>8}")

    print("-" * 100)
    avg = sum(accuracies) / len(accuracies) if accuracies else 0.0
    worst = min(accuracies) if accuracies else 0.0
    print(f"{len(all_results)} flow(s): {n_crash} crashed, {n_struct} with structural issues, "
          f"{n_below} below {args.min_accuracy}% target")
    print(f"Expression accuracy — average: {avg:.1f}%, worst: {worst:.1f}%")

    if args.json:
        # Drop the full generated SQL text from the JSON dump — that's what
        # --out-root is for; the report should stay small enough to diff.
        slim = []
        for res in all_results:
            slim_res = {k: v for k, v in res.items() if k != "files"}
            slim.append(slim_res)
        Path(args.json).write_text(json.dumps(slim, indent=2), encoding="utf-8")
        print(f"\nFull report written to {args.json}")

    if n_crash or n_struct or n_below:
        sys.exit(1)


if __name__ == "__main__":
    main()
