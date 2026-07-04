# tfl-to-sql

Turns a Tableau Prep flow into ready-to-use SQL — so logic built in Tableau's
drag-and-drop flow editor doesn't have to be rebuilt by hand in a data
warehouse.

If you've ever needed a Tableau Prep flow to "become" a SQL pipeline (for
Dataform, dbt, or just plain BigQuery), this reads the flow file directly and
writes out the equivalent SQL — one clearly-labeled section per step in the
flow, in the same order the flow runs in.

---

## For anyone: what this actually does

A Tableau Prep flow is a chain of steps: pull in a table, clean it up, join
it to another table, group it, and so on. This tool reads that chain and
writes each step as a piece of SQL, in order, so the whole flow becomes one
readable SQL file (or one file per step, if you prefer).

Most of what Tableau Prep can do translates automatically and correctly:
calculated fields (however deeply nested), `IF/THEN/ELSE` logic, date math,
joins, unions, groupings, pivots, and column rename rules. The handful of
things that genuinely *can't* be figured out automatically — a value that
only lives inside Tableau, or a calculation the tool doesn't recognise — are
never silently guessed. They're clearly marked with a `TODO` comment right in
the SQL and listed in a summary at the end, so nothing wrong-but-plausible
slips through unnoticed.

This has been run against 18 real production Tableau Prep flows with zero
crashes, zero invalid SQL, and 100% of calculated fields translating cleanly
end to end — see [Validating against a real flow collection](#validating-against-a-real-flow-collection).

## Two ways to use it

**If you just want to convert a flow and don't want to install anything:**
use the web app. Upload your `.tfl`/`.tflx` file, get SQL back, done. See
[The web app](#the-web-app) below.

**If you're automating this (CI, batch conversion, scripting):** use the
command-line tool. See [Quickstart (command line)](#quickstart-command-line)
below.

---

## The web app

```bash
cd App && ./run.sh
```

This starts a local Streamlit app in your browser. Drop in a flow file and
it shows you:
- The generated SQL, with anything needing a manual check highlighted right
  in the code (not just buried in a separate list)
- A summary of how many steps and formulas were found, and what fraction
  translated cleanly — hover the ⓘ on any of those numbers for a plain-English
  explanation of what it means
- A way to build a schema file from a simple list of column names (no
  database access needed) — see [Schema-awareness](#schema-awareness)
- Downloads for individual files or everything as a zip

No setup beyond `pip install -r App/requirements.txt` (which `run.sh` does
for you) — no API keys or credentials required for normal use.

**Want your own copy of the app running online for free?** See
[Publishing to GitHub and deploying for free](#publishing-to-github-and-deploying-for-free).

---

## Quickstart (command line)

```bash
python Code/tfl_to_sql.py "Tableau Prep Flows/FST Segmentation Prep.tfl" \
    --mode bigquery --out ./output_sql
```

`Code/tfl_to_sql.py` is the entire engine — a single Python file with no
external dependencies, so it can be copied anywhere and run with any Python
3.10+.

| Flag | Default | What it does |
|---|---|---|
| `--mode {dataform,bigquery}` | `dataform` | `.sqlx` with `config{}`/`${ref()}`, or plain `.sql` |
| `--out PATH` | `./output_sql` | Output folder |
| `--dataset NAME` | `my_dataset` | Dataset/schema name used in output |
| `--split` | off | One file per step instead of one combined file per output |
| `--overwrite` | off | Allow overwriting existing output files |
| `--schema PATH` | — | Real column names — see [Schema-awareness](#schema-awareness) |
| `--overrides PATH` | — | Hand-authored fixes — see [Overrides](#overrides) |
| `--warnings-json PATH` | — | Also write the review report as machine-readable JSON |

Every generated column reference is written `UPPER_CASE_WITH_UNDERSCORES`
regardless of how it was named in Tableau (e.g. `[Customer ID]` becomes
`` `CUSTOMER_ID` ``) — a consistent style applied everywhere except table
names and the Dataform `config {}` block, which are left as-is. This is
purely cosmetic: BigQuery matches column names without caring about case, so
it doesn't change what anything resolves to.

---

## Understanding what gets flagged (and why that's a feature)

Two categories of things show up in the "needs manual review" report:

1. **Things the tool is confident about but wants double-checked anyway.**
   Right now this is only `UNION` steps — Tableau almost never has identical
   columns on both sides of a union, so even when the tool successfully
   lines everything up by name, it can't tell if a column with the same name
   holds a different *type* of data on each side. Every union is flagged for
   a quick look, every time, on principle.
2. **Things the tool genuinely cannot know.** A Tableau *parameter* (a value
   a person typed into Tableau, never written down in the flow file itself),
   a formula the parser doesn't recognise, or a bulk column-rename rule
   applied to a table whose columns aren't known.

Both are surfaced the same way: a `NULL /* TODO: ... */` or `-- TODO: ...`
comment exactly where the issue is, plus one line in the end-of-run summary.
Nothing is ever silently approximated — if it isn't flagged, it was
translated with full confidence.

---

## Schema-awareness

Some things (fully applying a bulk column-rename rule, fully deduplicating
columns in a JOIN) can only be done correctly if the tool knows the *real*
column names in a source table. It can often work this out for free — the
columns coming out of a grouping or pivot step are declared right there in
the flow file — but a table read directly from a database needs an outside
source for that information.

**Easiest option — no database access needed:** in the web app, use "Build a
schema from a column list": pick the source table, paste in its column
names (or upload a CSV), done.

**From BigQuery directly**, if you have access:
```bash
python tools/fetch_schema.py "Tableau Prep Flows/my_flow.tfl" \
    --project my-gcp-project --dataset raw_dataset --out schema_cache.json

python Code/tfl_to_sql.py "Tableau Prep Flows/my_flow.tfl" \
    --schema schema_cache.json --out ./output_sql
```

Schema format (also what both of the above write):
```json
{ "raw_table_name": { "columns": ["order_id", "customer_id", "..."] } }
```
The key is the table name, lowercased with spaces/punctuation turned into
underscores.

## Overrides

For the handful of things that genuinely can't be worked out automatically —
a Tableau parameter's real value, a formula the parser doesn't recognise, a
rename rule you want applied without setting up a full schema — write the
fix once in an `overrides.json` and reuse it. An override always wins over
anything auto-detected, and survives re-running the tool after the source
flow changes — editing the generated SQL directly doesn't, since the next
run overwrites it.

```json
{
  "parameters": { "Parameters.192a3229-...": "208" },
  "expressions": { "{{ FIXED [x]: SUM([y]) }}": "SUM(`Y`) OVER ()" },
  "bulk_renames": { "Rename Fields 1": [["order id", "order_id"]] }
}
```

```bash
python Code/tfl_to_sql.py "Tableau Prep Flows/my_flow.tfl" --overrides overrides.json
```

---

## Tooling for engineers

The pieces below are for people maintaining or automating this converter,
not for someone just converting a single flow.

### Validating what actually gets generated

The tool guarantees its output is syntactically valid SQL. Whether it
*compiles against your real tables* is a stronger claim only BigQuery itself
can make:

```bash
python tools/dry_run_validate.py ./output_sql --project my-gcp-project
```

Runs a `dryRun` query job per file (no data read, no cost beyond planning)
and reports which files would actually fail to compile.

### Syncing into a Dataform repo

```bash
python tools/sync_to_repo.py ./output_sql --repo /path/to/dataform-repo \
    --dest definitions/tableau_prep --branch tfl-sync/my-flow
```

Creates a branch, copies the generated files in, and makes a local commit —
like `terraform plan` before `apply`. It never pushes or opens a PR unless
you pass `--push` / `--pr` explicitly.

### Validating against a real flow collection

```bash
python tools/batch_convert_report.py "/path/to/a/folder/of/.tfl/files" \
    --mode bigquery --out-root /tmp/batch_out --min-accuracy 95 \
    --json batch_report.json
```

Runs the converter against every `.tfl`/`.tflx` in a folder and scores each
one: did it crash, is the output structurally valid SQL, and what fraction
of calculated fields translated with nothing flagged. Exit code is non-zero
if anything crashed, produced invalid SQL, or a flow fell below
`--min-accuracy` — point this at a folder of your own company's flows to use
it as a CI gate.

### Testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The suite covers: one unit test per Tableau-function translation, golden-file
regression tests against the sample flow in `Tableau Prep Flows/` (any
unintended output change fails CI), the schema/overrides mechanism, CLI
behaviour, and mocked tests for the BigQuery-touching tools. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to extend the converter and keep
the test suite meaningful as you do.

### Project layout

```
Code/tfl_to_sql.py             # the converter — stdlib only, no pip installs, copy-and-run anywhere
tools/fetch_schema.py          # BigQuery INFORMATION_SCHEMA -> schema_cache.json
tools/dry_run_validate.py      # BigQuery dryRun validation of generated output
tools/sync_to_repo.py          # branch + commit (+ optional push/PR) into a target repo
tools/batch_convert_report.py  # convert + score a whole folder of .tfl files at once
App/                           # Streamlit web app (streamlit_app.py) + its non-UI logic (app_logic.py)
tests/                         # pytest suite + golden fixtures
Tableau Prep Flows/            # sample .tfl used by the test suite
CONTRIBUTING.md                # how to extend the converter for a new Tableau step type
handover.md                    # the original build spec this project started from (historical reference)
```

---

## Publishing to GitHub and deploying for free

A short version of the full walkthrough, for whoever inherits this repo next:

1. **Commit and push:**
   ```bash
   git add -A && git commit -m "Initial commit"
   ```
   Create a new repository on GitHub (don't initialize it with a README —
   this repo already has one), then:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git branch -M main && git push -u origin main
   ```
2. **Deploy the app for free** at [share.streamlit.io](https://share.streamlit.io):
   sign in with GitHub → **Create app** → pick the repo/branch → set the
   main file path to `App/streamlit_app.py` → **Deploy**. Streamlit finds
   `App/requirements.txt` automatically. No secrets or API keys are needed
   for normal use.
3. Every push to `main` redeploys the app automatically. Free-tier apps
   sleep after a period of inactivity and wake up on the next visit (roughly
   30–60 seconds) — check Streamlit's current Community Cloud terms for the
   exact limits, since they do change over time.

---

## What this can't fully automate

Three things are flagged rather than guessed, because guessing would mean
generating SQL that *looks* right but silently isn't:

- **LOD `INCLUDE`/`EXCLUDE` expressions** — these change the level of detail
  data is grouped at in a way that genuinely needs a person to choose the
  right SQL shape (a FIXED-style LOD is handled automatically; INCLUDE/EXCLUDE
  is not).
- **Tableau custom SQL steps** and **node types Tableau doesn't expose** in
  the flow file's JSON at all — there's nothing to read.
- **Values that only exist inside Tableau** (parameters) — not written down
  anywhere the tool can see, so they need a real value supplied via
  `--overrides`.

---

Designed by Yash Sakhuja | Data & AI Scientist
