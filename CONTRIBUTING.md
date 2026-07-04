# Contributing to tfl-to-sql

Tableau adds new Prep step types almost every release. This doc is the
"how do I teach the converter about one" walkthrough — the extension points
already exist in the code as small registries/dispatch tables; you're adding
an entry, not restructuring anything.

Run `pytest` before and after any change. If you change output for the
sample flow intentionally, regenerate the golden fixtures with
`UPDATE_GOLDEN=1 pytest tests/test_golden.py` and look at the diff before
committing it — that diff *is* the review.

## Adding a new top-level node type (INPUT/CLEAN/JOIN/UNION/AGGREGATE/PIVOT/OUTPUT)

1. Add the raw `nodeType` string (lowercased, version-prefix stripped) to
   `NODE_TYPE_MAP` in `Code/tfl_to_sql.py`, mapped to one of the seven
   categories.
2. If it needs genuinely new SQL shape (not just a new spelling of an
   existing category), add a `_cte_<category>()` generator alongside the
   existing ones (`_cte_clean`, `_cte_join`, ...) and wire it into the `cat ==`
   dispatch in `build_combined()`.
3. Add the matching `_split_<category>()` for `--split` mode and register it
   in `SPLIT_GENERATORS`.
4. Add a golden-file case if you have a real (or realistic, hand-built)
   `.tfl` exercising it — see `tests/test_golden.py`.

## Adding a new CLEAN sub-step type (inside a loomContainer)

This is the most common addition — Tableau's clean-step palette grows over
time. Sub-steps are dispatched by `_strip_version(nodeType).lower()` inside
`_extract_clean_steps()`:

1. Add an `elif stype == '...':` branch there. Build a step tuple — reuse the
   existing shapes if it fits: `('rename', old, new)`, `('add', col, sql)`,
   `('remove', [cols])`, `('keep', [cols])`, `('replace', col, sql)`,
   `('filter', sql)`. Adding a genuinely new *kind* of step means also adding
   a case to `_layer_body()` (how it renders as SQL) and, if it changes the
   column set, to `_apply_steps_to_columns()` (how schema-tracking projects
   through it) and `_clean_container_result_columns()` (the pure/no-warnings
   mirror used by the schema pre-pass — see "Two implementations of the same
   logic" below).
2. If the step type doesn't affect data (e.g. `ReorderColumns`), just
   `continue` — no step, no warning.
3. If you genuinely can't translate it (needs external input, like
   `BulkRenameColumns` without a known schema), append to `PARSE_WARNINGS`
   with a specific, actionable message — **never guess**. That's the one
   rule that matters more than any other in this codebase: an unresolved
   item flagged in the review report is fine; wrong SQL that looks right is
   not.

## Adding a new Tableau calc function (ZN, IIF, DATEADD, ...)

Function-name-to-BigQuery mapping lives in `_emit_call()`. Add a branch:

```python
if n == 'YOUR_FUNC' and len(eargs) == <expected arg count>:
    return f'YOUR_BIGQUERY_EQUIVALENT({eargs[0]}, ...)'
```

Add a unit test in `tests/test_expression_parser.py` pinning the exact
translation — one function, one test, matching the existing pattern.

## Two implementations of the same logic (and why)

`_extract_clean_steps()` (real, emits warnings, calls `translate_expr`) and
`_clean_container_result_columns()` (pure, no warnings, used only by the
schema pre-pass in `compute_node_columns()`) both walk a CLEAN container's
steps. This duplication is deliberate, not an oversight: the pre-pass runs
*before* real generation to figure out what schema is knowable, and must
never emit a warning or call `translate_expr` — if it did, everything would
be double-counted the one time generation runs for real. If you add a step
type that changes the column set, update both.

## What "flag, don't guess" means in practice

Before adding a fallback, ask: is this mechanically derivable from the
flow's own JSON (or from `--schema`/`--overrides`), or am I about to guess?
If it's a guess, it belongs in `PARSE_WARNINGS`, not in the generated SQL.
The whole point of this tool is that a data engineer can trust everything
that *isn't* flagged.

## Local dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check Code tools tests App
mypy Code/tfl_to_sql.py
```
