#!/usr/bin/env python3
"""dry_run_validate.py — Validate generated SQL/SQLX files actually compile in
BigQuery, using dryRun query jobs (no data is read, no cost beyond a plan).

This is the difference between "no leftover Tableau syntax" (what
tfl_to_sql.py's own checks guarantee) and "this runs" (what only BigQuery
itself can confirm) — Phase 1 of the platform roadmap.

Usage:
    python tools/dry_run_validate.py <output_dir> --project my-gcp-project \\
        [--report dry_run_report.json]

Dataform .sqlx files have their `config {...}` header stripped and any
${ref('x')} replaced with a syntactic placeholder table name before
validation, since dryRun needs plain BigQuery SQL, not a Dataform template —
this only proves the SQL body compiles, not that the referenced Dataform
declarations line up (Dataform's own compiler is the source of truth for
that).

Requires: pip install "tfl-to-sql[bigquery]"
Requires: BigQuery job-user + dataViewer credentials for --project.
"""

import argparse
import json
import re
import sys
from pathlib import Path

_REF_RE = re.compile(r"\$\{ref\('([^']+)'\)\}")
_CONFIG_BLOCK_RE = re.compile(r"^\s*config\s*\{.*?\}\s*", re.DOTALL)


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


def to_plain_bigquery_sql(text: str, dataset: str) -> str:
    """Best-effort: strip a Dataform config block and replace ${ref('x')}
    with a plain `dataset.x` reference so dryRun has something to parse.
    Not a substitute for Dataform's own compiler — see module docstring."""
    text = _CONFIG_BLOCK_RE.sub("", text, count=1)
    text = _REF_RE.sub(lambda m: f"`{dataset}`.`{m.group(1)}`", text)
    return text


def dry_run_one(client, project: str, sql: str) -> tuple:
    """Returns (ok: bool, message: str)."""
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        client.query(sql, job_config=job_config, project=project)
        return True, "OK"
    except Exception as exc:
        return False, str(exc)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out_dir", help="Folder of generated .sql/.sqlx files (e.g. --out from tfl_to_sql.py)")
    parser.add_argument("--project", required=True, help="GCP project to run the dry-run job against")
    parser.add_argument("--dataset", default="my_dataset",
                        help="Dataset to substitute for ${ref()} placeholders (default: my_dataset)")
    parser.add_argument("--report", metavar="PATH", help="Also write results as JSON")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        raise SystemExit(f"ERROR: Not a directory: {args.out_dir}")

    files = sorted(out_dir.glob("*.sql")) + sorted(out_dir.glob("*.sqlx"))
    if not files:
        raise SystemExit(f"ERROR: No .sql/.sqlx files found in {args.out_dir}")

    client = _require_bigquery_client()

    results = []
    failures = 0
    for path in files:
        raw = path.read_text(encoding="utf-8")
        sql = to_plain_bigquery_sql(raw, args.dataset) if path.suffix == ".sqlx" else raw
        ok, message = dry_run_one(client, args.project, sql)
        results.append({"file": path.name, "ok": ok, "message": message})
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {path.name}" + ("" if ok else f" — {message.splitlines()[0][:160]}"))
        if not ok:
            failures += 1

    print(f"\n{len(files) - failures}/{len(files)} file(s) passed dry-run validation.")

    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Report written to {args.report}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
