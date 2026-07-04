#!/usr/bin/env python3
"""fetch_schema.py — Pull real column names from BigQuery for a flow's INPUT
tables and write a schema_cache.json that `tfl_to_sql.py --schema` consumes.

This is what unlocks Phase 2 of the platform roadmap for real: without it,
tfl_to_sql.py still runs fine (schema-aware features degrade gracefully to
today's flagged-not-guessed behaviour), but BulkRenameColumns on a branch
whose columns can't be derived from the flow's own JSON (i.e. anything
tracing straight back to an INPUT node) stays unresolved, and JOIN dedup
stays key-only, until a real schema is supplied.

Usage:
    python tools/fetch_schema.py <flow.tfl> --project my-gcp-project \\
        --dataset raw_dataset [--out schema_cache.json]

Requires: pip install "tfl-to-sql[bigquery]"  (or: pip install google-cloud-bigquery)
Requires: BigQuery read + metadata credentials for --project (gcloud auth
application-default login, or GOOGLE_APPLICATION_CREDENTIALS pointing at a
service account key with roles/bigquery.metadataViewer + roles/bigquery.jobUser).
"""

import argparse
import json
import sys
from pathlib import Path

_CODE_DIR = Path(__file__).parent.parent / "Code"
sys.path.insert(0, str(_CODE_DIR))

import tfl_to_sql as t  # noqa: E402


def _require_bigquery_client():
    try:
        from google.cloud import bigquery
    except ImportError:
        raise SystemExit(
            "ERROR: google-cloud-bigquery isn't installed.\n"
            "Install it with:  pip install \"tfl-to-sql[bigquery]\"\n"
            "(or just: pip install google-cloud-bigquery)"
        )
    try:
        return bigquery.Client()
    except Exception as exc:
        raise SystemExit(
            f"ERROR: could not create a BigQuery client: {exc}\n"
            "Check that you're authenticated (gcloud auth application-default "
            "login) and have a default project set, or pass --project."
        )


def input_table_names(flow_path: str) -> dict:
    """Return {sanitised_name: real_table_name} for every INPUT node."""
    flow = t.load_tfl(flow_path)
    nodes = t.extract_nodes(flow)
    tables = {}
    for node in nodes.values():
        cat, _raw = t.classify_node(node)
        if cat != "INPUT":
            continue
        attrs = node.get("connectionAttributes", {})
        table = (attrs.get("datasourceName") or attrs.get("tableName")
                 or attrs.get("dbname") or node.get("name", ""))
        if table:
            tables[t.sanitise(table)] = table
    return tables


def fetch_columns(client, project: str, dataset: str, table: str) -> list:
    """Query INFORMATION_SCHEMA.COLUMNS for one table. Returns [] (with a
    warning printed, not raised) if the table doesn't exist or isn't
    accessible — a missing table shouldn't abort fetching the rest."""
    query = f"""
        SELECT column_name
        FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
        WHERE table_name = @table_name
        ORDER BY ordinal_position
    """
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("table_name", "STRING", table)]
    )
    try:
        rows = list(client.query(query, job_config=job_config).result())
    except Exception as exc:
        print(f'WARNING: could not fetch columns for "{table}": {exc}')
        return []
    if not rows:
        print(f'WARNING: no columns found for "{table}" in {project}.{dataset} — check the table name/dataset.')
    return [row.column_name for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("flow", help="Path to the .tfl file")
    parser.add_argument("--project", required=True, help="GCP project that owns the BigQuery dataset")
    parser.add_argument("--dataset", required=True, help="BigQuery dataset the INPUT tables live in")
    parser.add_argument("--out", default="schema_cache.json", help="Output path (default: schema_cache.json)")
    args = parser.parse_args()

    if not Path(args.flow).exists():
        raise SystemExit(f"ERROR: File not found: {args.flow}")

    tables = input_table_names(args.flow)
    if not tables:
        raise SystemExit("ERROR: No INPUT nodes found in the flow file.")

    client = _require_bigquery_client()

    schema = {}
    for sanitised_name, real_table in tables.items():
        print(f"Fetching columns for {real_table} ...")
        columns = fetch_columns(client, args.project, args.dataset, real_table)
        if columns:
            schema[sanitised_name] = {"columns": columns}
            print(f"  {len(columns)} column(s)")

    if not schema:
        raise SystemExit("ERROR: Fetched zero usable schemas — nothing written.")

    # Merge with any existing cache rather than clobbering entries for other flows.
    out_path = Path(args.out)
    existing = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text(encoding="utf-8"))
    existing.update(schema)
    out_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    print(f"\nWrote {len(schema)} table schema(s) to {args.out}")
    print(f"Use it with: python Code/tfl_to_sql.py {args.flow} --schema {args.out} ...")


if __name__ == "__main__":
    main()
