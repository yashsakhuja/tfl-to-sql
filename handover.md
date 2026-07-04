# Handover: Tableau Prep (.tfl) to BigQuery SQL / Dataform SQLX Converter

## What to build

A Python CLI script that reads a Tableau Prep flow file (`.tfl`), parses its node
graph, and generates one SQL or SQLX file per node written to an output folder.
The files should be in dependency order and ready to paste into BigQuery or drop
into a Dataform project.

---

## Background

Tableau Prep saves flows as `.tfl` files. These are JSON, sometimes with a small
binary prefix before the first `{`. Every step in the flow is a node in a directed
acyclic graph. The script needs to walk that graph and turn each node into the
equivalent SQL transformation.

---

## CLI interface

```
python tfl_to_sql.py <flow.tfl> [--mode dataform|bigquery] [--out ./output_sql] [--dataset my_dataset]
```

| Argument | Default | Description |
|---|---|---|
| `flow.tfl` | required | Path to the Tableau Prep flow file |
| `--mode` | `dataform` | `dataform` emits `.sqlx` with config blocks and `${ref()}`. `bigquery` emits plain `.sql` |
| `--out` | `./output_sql` | Folder to write generated files into (create if missing) |
| `--dataset` | `my_dataset` | BigQuery dataset name to use in output table references |

---

## Step-by-step implementation plan

### Step 1: Load and parse the .tfl file

- Read the file as raw bytes
- Find the index of the first `{` character and slice from there
- Parse as JSON with `json.loads()`
- Raise a clear error if no JSON object is found or parsing fails

### Step 2: Extract nodes and edges

Nodes may be stored under any of these keys depending on Prep version:

```
flow["nodes"]
flow["nodesByName"]
flow["flowDocument"]["nodes"]
```

Each value in the dict (or item in the list) is a node object. Build:

```python
nodes: dict[str, dict]   # node_id -> node object
```

Edges (connections between steps) may be stored under:

```
flow["connections"]
flow["edges"]
flow["links"]
```

Each edge object contains source and destination node IDs under keys like:
`fromNodeId / toNodeId`, `source / target`, or `from / to`.

Build:

```python
edges: list[tuple[str, str]]   # (from_node_id, to_node_id)
```

### Step 3: Classify each node by type

Each node object has a `nodeType` or `type` field. Map it to one of these
internal categories:

| Prep nodeType value(s) | Internal category |
|---|---|
| `loadSql`, `input`, `superSimpleInput` | `INPUT` |
| `superTransform`, `cleanStep`, `transform` | `CLEAN` |
| `join` | `JOIN` |
| `union` | `UNION` |
| `aggregate`, `aggregation` | `AGGREGATE` |
| `pivot` | `PIVOT` |
| `writeToDatabaseStep`, `output`, `superOutput` | `OUTPUT` |

Any node with an unrecognised type should be collected and printed as a warning
at the end. Do not crash; emit a `-- TODO: unrecognised node type` comment in
the output file instead.

### Step 4: Topological sort

- Build an adjacency list and in-degree count from the edges list
- Run Kahn's algorithm (BFS-based topological sort)
- Process nodes in the resulting order so every upstream node is written before
  its downstream dependents
- Each node should know the sanitised name(s) of its direct parent(s) for use
  in SQL FROM / JOIN clauses

Name sanitisation rule: strip non-alphanumeric characters, replace spaces and
special characters with underscores, lowercase everything, collapse consecutive
underscores, strip leading/trailing underscores.

### Step 5: SQL generation per node type

Use the parent node's sanitised name wherever a table reference is needed.
In `dataform` mode use `${ref('parent_name')}`. In `bigquery` mode use the
name as a plain identifier.

#### INPUT

```sql
-- dataform mode
config {
  type: "declaration",
  schema: "raw_dataset",
  name: "source_orders"
}

-- bigquery mode
-- Source table: project.raw_dataset.source_orders
-- No SQL needed; reference this table directly in downstream steps.
```

Extract the source table name from the node's `connectionAttributes`,
`relation`, or `tableName` field.

#### CLEAN

```sql
config { type: "view", schema: "my_dataset", description: "..." }  -- dataform only

SELECT
  column_a,
  CAST(column_b AS INT64)            AS column_b,
  COALESCE(column_c, 0)              AS column_c,
  CASE WHEN x = 'y' THEN 1 ELSE 0 END AS flag
FROM ${ref('parent_step')}
WHERE filter_column IS NOT NULL
```

- Column list comes from the node's `columnList`, `fields`, or `outputColumns`
- Calculated fields are in a `calculatedColumns` or `expressions` array; map
  each expression string as best you can (see function mapping table below)
- Filters are in a `filterClause`, `filters`, or `conditions` array

#### JOIN

```sql
config { type: "view", schema: "my_dataset" }  -- dataform only

SELECT
  l.*,
  r.column_x,
  r.column_y
FROM ${ref('left_parent')} AS l
INNER JOIN ${ref('right_parent')} AS r
  ON l.join_key = r.join_key
```

- Join type comes from `joinType`: `inner`, `left`, `right`, `full`
- Join keys come from `joinExpressions` or `conditions`
- The two input branches come from the two incoming edges for this node

#### UNION

```sql
config { type: "view", schema: "my_dataset" }  -- dataform only

SELECT * FROM ${ref('branch_one')}
UNION ALL
SELECT * FROM ${ref('branch_two')}
```

- All incoming edges are union branches
- If the node has a `unionType` of `distinct` use `UNION DISTINCT` instead

#### AGGREGATE

```sql
config { type: "table", schema: "my_dataset" }  -- dataform only

SELECT
  dimension_a,
  dimension_b,
  SUM(measure_x)   AS total_x,
  COUNT(order_id)  AS order_count,
  AVG(amount)      AS avg_amount
FROM ${ref('parent_step')}
GROUP BY
  dimension_a,
  dimension_b
```

- Dimensions come from `groupByFields` or `dimensions`
- Measures come from `aggregateExpressions` or `measures`; map `SUM`, `COUNT`,
  `AVG`, `MIN`, `MAX` directly

#### PIVOT

```sql
config { type: "view", schema: "my_dataset" }  -- dataform only

SELECT *
FROM ${ref('parent_step')}
PIVOT(
  SUM(value_column)
  FOR pivot_column IN ('val1', 'val2', 'val3')
)
```

- Use BigQuery `PIVOT` syntax
- Pivot column, value column, and the list of pivot values come from the node's
  `pivotColumn`, `valueColumn`, and `pivotValues` fields
- If unpivoting, use `UNPIVOT` syntax instead

#### OUTPUT

```sql
-- dataform mode
config {
  type: "table",
  schema: "my_dataset",
  name: "final_output_name",
  description: "Final output from Tableau Prep flow"
}

SELECT *
FROM ${ref('last_upstream_step')}

-- bigquery mode
CREATE OR REPLACE TABLE my_dataset.final_output_name AS
SELECT *
FROM last_upstream_step
```

### Step 6: Tableau Prep to BigQuery function mapping

Apply these substitutions when converting calculated field expressions:

| Tableau Prep / Calc | BigQuery equivalent |
|---|---|
| `ZN(x)` | `COALESCE(x, 0)` |
| `ISNULL(x)` | `x IS NULL` |
| `IFNULL(x, y)` | `COALESCE(x, y)` |
| `IIF(cond, a, b)` | `IF(cond, a, b)` |
| `DATEPARSE(fmt, x)` | `PARSE_DATE(fmt, x)` |
| `DATETRUNC('month', x)` | `DATE_TRUNC(x, MONTH)` |
| `DATEDIFF('day', a, b)` | `DATE_DIFF(b, a, DAY)` |
| `TODAY()` | `CURRENT_DATE()` |
| `NOW()` | `CURRENT_TIMESTAMP()` |
| `LEFT(x, n)` | `SUBSTR(x, 1, n)` |
| `RIGHT(x, n)` | `RIGHT(x, n)` |
| `CONTAINS(x, y)` | `x LIKE '%y%'` or `STRPOS(x, y) > 0` |
| `INT(x)` | `CAST(x AS INT64)` |
| `FLOAT(x)` | `CAST(x AS FLOAT64)` |
| `STR(x)` | `CAST(x AS STRING)` |

### Step 7: Write output files

- Create the output directory if it does not exist
- Name each file using the node's sanitised name: `01_source_orders.sqlx`
- Prefix with a zero-padded index matching topological order so files sort
  correctly in a file browser
- Use `.sqlx` extension in `dataform` mode and `.sql` in `bigquery` mode
- After writing all files, print a summary:

```
Generated 7 files in ./output_sql/

  01_source_orders.sqlx       INPUT
  02_source_products.sqlx     INPUT
  03_cleaned_orders.sqlx      CLEAN
  04_joined_data.sqlx         JOIN
  05_aggregated_sales.sqlx    AGGREGATE
  06_pivoted_regions.sqlx     PIVOT
  07_final_output.sqlx        OUTPUT

Warnings:
  Node "experimental_step" (id: abc123) has unrecognised type "customScript" - skipped.
```

---

## File structure to produce

```
tfl_to_sql.py        # main script (single file, stdlib only, no pip installs needed)
README.md            # brief usage instructions
```

---

## Error handling rules

- If the `.tfl` file does not exist: print a clear message and exit with code 1
- If JSON parsing fails: print the error and suggest the file may be corrupted
- If a node has missing expected fields: skip that field with a `-- TODO` comment
  in the SQL, do not crash
- If a circular dependency is detected in the graph: print the cycle and exit
  with code 1
- Unrecognised node types: warn but continue

---

## What not to do

- Do not use any third-party libraries (stdlib only: `json`, `os`, `re`,
  `argparse`, `pathlib`, `collections`)
- Do not hardcode any dataset or project names (take them from CLI args)
- Do not attempt to execute or validate the generated SQL
- Do not overwrite existing files without warning; add a `--overwrite` flag to
  allow it

---

## Acceptance criteria

Running the script against a real `.tfl` file should:

1. Produce one file per node in the output folder
2. Files are ordered by dependency (upstream first)
3. Every `FROM` clause uses `${ref()}` in dataform mode or a plain name in
   bigquery mode
4. No unhandled exceptions for any of the listed node types
5. A warning is printed for any node type not in the classification table
