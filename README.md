# tfl-to-sql

Converts a Tableau Prep flow (`.tfl`) into BigQuery SQL or Dataform SQLX —
one CTE per step, in dependency order, ready to run or drop into a Dataform
project.

The core converter (`Code/tfl_to_sql.py`) parses the flow's calc language
with a real tokenizer/parser (not regexes), so nested function calls,
`IF/THEN/ELSEIF/ELSE/END` blocks, `{FIXED ...}` LOD expressions, and
Tableau Prep's `{PARTITION dims: {ORDERBY cols: ROW_NUMBER()/RANK()/LOOKUP()}}`
window-calc syntax (used for duplicate-row detection and sequencing) all
translate correctly to their BigQuery equivalents. Node types covered:
INPUT, CLEAN (including `ChangeColumnType`, `Remap`, `BulkRenameColumns`,
rich range/discrete filters, and everything in between), JOIN, UNION,
AGGREGATE, PIVOT, UNPIVOT, OUTPUT. Verified against 18 real production
flows (see [Validating against a real flow collection](#validating-against-a-real-flow-collection)):
0 crashes, 0 structurally invalid output, 100% expression-level translation
accuracy.

Anything it genuinely can't resolve on its own — a Tableau `Parameters.*`
reference, an `INCLUDE`/`EXCLUDE` LOD expression, a `BulkRenameColumns` step
on a branch with no knowable column set — is left as a clearly marked
`NULL /* TODO: ... */` and listed in a "needs manual review" report at the
end of every run. It never silently guesses.

## Quickstart

```bash
python Code/tfl_to_sql.py "Tableau Prep Flows/FST Segmentation Prep.tfl" \
    --mode bigquery --out ./output_sql
```

| Flag | Default | What it does |
|---|---|---|
| `--mode {dataform,bigquery}` | `dataform` | `.sqlx` with `config{}`/`${ref()}`, or plain `.sql` |
| `--out PATH` | `./output_sql` | Output folder |
| `--dataset NAME` | `my_dataset` | Dataset/schema name used in output |
| `--split` | off | One file per node instead of one combined CTE chain per output |
| `--overwrite` | off | Allow overwriting existing output files |
| `--schema PATH` | — | JSON column-name cache — see [Schema-awareness](#schema-awareness) |
| `--overrides PATH` | — | Hand-authored fixes — see [Overrides](#overrides) |
| `--warnings-json PATH` | — | Also write the review report as JSON |

## Schema-awareness

Some things (a `BulkRenameColumns` step, full JOIN column dedup) can only be
done correctly if the converter knows the real column names of the source
table. It's often knowable for free — an AGGREGATE or PIVOT node's output
columns are computable straight from the flow's own JSON — but a branch
that traces straight back to an INPUT table needs an external source.

```bash
python tools/fetch_schema.py "Tableau Prep Flows/my_flow.tfl" \
    --project my-gcp-project --dataset raw_dataset --out schema_cache.json

python Code/tfl_to_sql.py "Tableau Prep Flows/my_flow.tfl" \
    --schema schema_cache.json --out ./output_sql
```

`tools/fetch_schema.py` needs `pip install "tfl-to-sql[bigquery]"` and
BigQuery credentials; the `--schema` flag on the converter itself only reads
a JSON file, so generation stays reproducible and offline without it.

Schema format (also what `fetch_schema.py` writes):
```json
{ "raw_table_name": { "columns": ["order_id", "customer_id", "..."] } }
```

## Overrides

For anything that isn't mechanically derivable — a Tableau parameter's real
value, an expression the parser can't handle, a bulk-rename rule you want to
apply without wiring up a full schema — write an `overrides.json` once and
reuse it. Overrides always take precedence over schema-driven behaviour and
survive re-running the converter after the source flow changes upstream.

```json
{
  "parameters": { "Parameters.192a3229-...": "208" },
  "expressions": { "{{ FIXED [x]: SUM([y]) }}": "SUM(`y`) OVER ()" },
  "bulk_renames": { "Rename Fields 1": [["order id", "order_id"]] }
}
```

```bash
python Code/tfl_to_sql.py "Tableau Prep Flows/my_flow.tfl" --overrides overrides.json
```

## Validating what actually gets generated

`tfl_to_sql.py` guarantees the output is syntactically valid SQL. Whether it
*compiles against your real tables* is a stronger claim only BigQuery can
make:

```bash
python tools/dry_run_validate.py ./output_sql --project my-gcp-project
```

Runs a `dryRun` query job per file (no data read, no cost beyond planning)
and reports which files would actually fail to compile.

## Syncing into a Dataform repo

```bash
python tools/sync_to_repo.py ./output_sql --repo /path/to/dataform-repo \
    --dest definitions/tableau_prep --branch tfl-sync/my-flow
```

Creates a branch, copies the generated files in, and makes a local commit —
like `terraform plan` before `apply`. It never pushes or opens a PR unless
you pass `--push` / `--pr` explicitly.

## Validating against a real flow collection

```bash
python tools/batch_convert_report.py "/path/to/a/folder/of/.tfl/files" \
    --mode bigquery --out-root /tmp/batch_out --min-accuracy 95 \
    --json batch_report.json
```

Runs the converter against every `.tfl` in a folder and scores each one:
did it crash, is the output structurally valid SQL (balanced parens/
backticks, no residual Tableau syntax — string literal contents are
excluded from these checks so real data containing a literal `(` doesn't
trip a false positive), and what fraction of Tableau calc expressions
translated with no flag (`expression_accuracy_pct` — the same metric
`build_review_report()` computes for a single run). Exit code is non-zero
if anything crashed, produced structurally invalid SQL, or a flow fell
below `--min-accuracy`, so this can gate CI once pointed at a real
collection of company flows.

## Streamlit app

```bash
cd App && ./run.sh
```

Upload a `.tfl`, get a live preview with a translation-coverage summary,
inline "needs manual review" list, optional schema/overrides upload, and an
optional LLM cleanup pass (only ever applied to items the rule engine
already flagged — never to SQL it didn't flag). See `App/requirements.txt`.

## Testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The suite is: unit tests per Tableau-function translation
(`tests/test_expression_parser.py`), golden-file regression tests against
the real flow in `Tableau Prep Flows/` (`tests/test_golden.py` — any
unintended output change fails CI), schema/overrides mechanism tests, CLI
integration tests, and mocked tests for the BigQuery-touching tools. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to extend the converter and keep
the test suite meaningful as you do.

## Project layout

```
Code/tfl_to_sql.py          # the converter — stdlib only, no pip installs, copy-and-run anywhere
tools/fetch_schema.py       # BigQuery INFORMATION_SCHEMA -> schema_cache.json
tools/dry_run_validate.py   # BigQuery dryRun validation of generated output
tools/sync_to_repo.py       # branch + commit (+ optional push/PR) into a target repo
tools/batch_convert_report.py  # convert + score a whole folder of .tfl files at once
App/                        # Streamlit UI (streamlit_app.py) + its non-UI logic (app_logic.py)
tests/                      # pytest suite + golden fixtures
Tableau Prep Flows/         # sample .tfl input
handover.md                 # original build spec (historical reference)
```

## What this can't fully automate

LOD `INCLUDE`/`EXCLUDE` expressions (different aggregation grain — genuinely
needs a human to choose the right subquery shape), Tableau custom SQL
steps, and node types Tableau doesn't expose in this JSON schema at all. All
three are flagged, not guessed. See the roadmap this platform was built
from for the longer-term plan (override-survival across re-generation,
PR-automated workflow integration, coverage dashboards).
