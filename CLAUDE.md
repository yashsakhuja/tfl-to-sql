# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python CLI that reads a Tableau Prep flow file (`.tfl`), parses its node graph, and generates one SQL or SQLX file per node in dependency order — ready for BigQuery or Dataform.

## Running the Script

```bash
python tfl_to_sql.py <flow.tfl> [--mode dataform|bigquery] [--out ./output_sql] [--dataset my_dataset] [--overwrite]
```

- Default mode: `dataform` (emits `.sqlx` with `config {}` blocks and `${ref()}`)
- `bigquery` mode emits plain `.sql` with `CREATE OR REPLACE TABLE`
- Output files are zero-padded and named by topological order: `01_source_name.sqlx`

## Architecture

The script is a **single file** (`tfl_to_sql.py`) using **stdlib only** — no third-party packages (`json`, `os`, `re`, `argparse`, `pathlib`, `collections`).

### Pipeline (7 steps)

1. **Load** — read raw bytes, find first `{`, parse JSON
2. **Extract nodes/edges** — nodes from `flow["nodes"]` / `flow["nodesByName"]` / `flow["flowDocument"]["nodes"]`; edges from `flow["connections"]` / `flow["edges"]` / `flow["links"]`
3. **Classify** — map `nodeType`/`type` field to: `INPUT`, `CLEAN`, `JOIN`, `UNION`, `AGGREGATE`, `PIVOT`, `OUTPUT`
4. **Topological sort** — Kahn's algorithm (BFS); each node tracks its sanitised parent name(s)
5. **SQL generation** — per-type templates (see handover.md for full spec per type)
6. **Expression translation** — Tableau calc functions → BigQuery equivalents (e.g. `ZN(x)` → `COALESCE(x, 0)`)
7. **Write files** — zero-padded filenames, print summary + warnings for unknown node types

### Name sanitisation

Strip non-alphanumeric chars, replace spaces/specials with `_`, lowercase, collapse consecutive `_`, strip leading/trailing `_`.

### Key field locations (nodes are version-dependent)

| Data | Field paths to try |
|---|---|
| Node type | `nodeType`, `type` |
| Source table | `connectionAttributes`, `relation`, `tableName` |
| Columns | `columnList`, `fields`, `outputColumns` |
| Calculated fields | `calculatedColumns`, `expressions` |
| Filters | `filterClause`, `filters`, `conditions` |
| Group-by | `groupByFields`, `dimensions` |
| Measures | `aggregateExpressions`, `measures` |
| Join keys | `joinExpressions`, `conditions` |

## File Layout

```
tfl_to_sql.py               # main script (to be created in Code/)
Tableau Prep Flows/
  Prep ETL Steps.tfl        # sample flow for testing
SQL Output Files/           # generated SQL lands here
handover.md                 # full spec and acceptance criteria
```

## Constraints

- No pip installs — stdlib only
- Do not hardcode dataset/project names; always use CLI args
- Do not overwrite existing output files unless `--overwrite` is passed
- On circular dependency: print the cycle and exit with code 1
- Unknown node types: warn and emit `-- TODO: unrecognised node type` comment, do not crash
