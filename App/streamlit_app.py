"""
Tableau Prep Flow → SQL Converter
Streamlit app: upload a .tfl/.tflx flow, get a ready-to-use Dataform SQLX file.
Optional LLM cleanup fixes whatever the rule engine still flags for review.
"""

import json
import sys
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Import core conversion logic
# ---------------------------------------------------------------------------
_CODE_DIR = Path(__file__).parent.parent / "Code"
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
_APP_DIR = Path(__file__).parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from app_logic import (  # noqa: E402
    convert_tfl,
    list_input_tables,
    make_zip,
    parse_column_names,
    render_sql_with_warnings,
)

MODE = "dataform"
DATASET = "my_dataset"

# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Tableau Prep → SQL",
    page_icon="🔄",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stDownloadButton > button { width: 100%; }
    .block-container { padding-top: 2rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Header ───────────────────────────────────────────────────────────────────
title_col, credit_col = st.columns([3, 2])
with title_col:
    st.title("🔄 Tableau Prep → SQL")
with credit_col:
    st.markdown(
        "<div style='text-align:right; padding-top:1.8rem; opacity:0.6; "
        "font-size:0.85rem; white-space:nowrap;'>Designed by Yash Sakhuja | Data &amp; AI Scientist</div>",
        unsafe_allow_html=True,
    )
st.caption(
    "Upload a Tableau Prep flow (`.tfl` or `.tflx`) and get one ready-to-use "
    "Dataform file, with every step in the flow turned into its own "
    "clearly-labeled, readable section of SQL."
)
st.caption(
    f"Generated files use `{DATASET}` as a placeholder schema name — update "
    "that before deploying, or ask whoever runs this tool from the command "
    "line to set a fixed one for you."
)

# ── File upload (kept above the sidebar so the schema helper below can list
#    this flow's source tables) ───────────────────────────────────────────────
uploaded = st.file_uploader(
    "Drop your Tableau Prep flow file here (.tfl or .tflx)",
    type=["tfl", "tflx"],
)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Output settings")
    st.caption("Files are generated for **Dataform** (`.sqlx`), ready to drop into your Dataform project.")

    st.divider()
    st.caption(
        "**Translated automatically:**  \n"
        "Tableau `IF/THEN/ELSEIF/ELSE/END` logic, calculated fields at any "
        "level of nesting, `{FIXED ...}` running totals, date math, "
        "null-handling functions, text functions, joins, unions, "
        "aggregations, pivots, and column-rename rules wherever the column "
        "list is knowable (from a schema you provide below, or automatically "
        "when it comes from an earlier grouping/pivot step in the same flow).  \n\n"
        "**Flagged for someone to check:**  \n"
        "A Tableau *parameter* (a value only known inside Tableau, never "
        "written down in the flow file), a calculation the tool didn't "
        "recognise, or a column-rename rule with no way to know the real "
        "column names."
    )

    # ── Schema ────────────────────────────────────────────────────────────
    st.divider()
    schema_label_col, schema_info_col = st.columns([5, 1])
    with schema_label_col:
        st.markdown("**Schema** _(optional)_")
    with schema_info_col:
        with st.popover("ℹ️", use_container_width=True):
            st.markdown(
                "**What it's for**\n\n"
                "Tells the converter the *real* column names in your source "
                "table. Without it, the converter can still figure out "
                "columns that come from a grouping or pivot step earlier in "
                "the same flow — but it can't see past a raw source table "
                "on its own.\n\n"
                "**What it unlocks**\n"
                "- Applies column-rename rules for real, instead of "
                "flagging them for someone to do by hand\n"
                "- Fully removes duplicate column names when two tables are "
                "joined together, not just the obvious one\n"
                "- Catches a misspelled column name before the warehouse does\n\n"
                "**Easiest way to make one:** use the *\"Build a schema from "
                "a column list\"* box below — no BigQuery access needed.\n\n"
                "**Already have a schema.json?** Upload it directly below. "
                "Format: one entry per source table —\n"
                "```json\n"
                "{\n"
                '  "fst_ecom_retail_sales": {\n'
                '    "columns": ["customer_id", "order_id", "..."]\n'
                "  }\n"
                "}\n"
                "```\n"
                "Advanced: `tools/fetch_schema.py` can also pull this "
                "straight from BigQuery if you have access to it."
            )

    with st.expander("🧩 Build a schema from a column list (no BigQuery needed)"):
        st.caption(
            "Pick a source table from your flow, then give it a list of "
            "column names — by uploading a CSV or just pasting them in. "
            "One column name per line, or comma-separated; don't include a "
            "header row."
        )

        if "built_schema" not in st.session_state:
            st.session_state.built_schema = {}

        input_tables = list_input_tables(uploaded.getvalue()) if uploaded is not None else {}

        if input_tables:
            table_key = st.selectbox(
                "Which source table are these columns for?",
                options=list(input_tables.keys()),
                format_func=lambda k: input_tables[k],
                key="schema_table_select",
            )
        else:
            st.caption("Upload your flow above to pick a table from a list, or type its name here:")
            table_key = st.text_input(
                "Table name", placeholder="e.g. fst_ecom_retail_sales", key="schema_table_text",
            )

        col_csv = st.file_uploader("Upload a CSV of column names", type=["csv"], key="col_csv_upload")
        col_paste = st.text_area(
            "...or paste column names here",
            placeholder="customer_id\norder_id\namount_paid_excl_tax",
            height=100,
            key="col_paste_area",
        )

        raw_text = col_csv.getvalue().decode("utf-8-sig", errors="replace") if col_csv is not None else col_paste
        preview_cols = parse_column_names(raw_text) if raw_text and raw_text.strip() else []

        if preview_cols:
            shown = ", ".join(preview_cols[:8]) + (", ..." if len(preview_cols) > 8 else "")
            st.caption(f"Found {len(preview_cols)} column name(s): {shown}")

        if st.button("➕ Add to schema", disabled=not (table_key and preview_cols), use_container_width=True):
            st.session_state.built_schema[table_key.strip()] = preview_cols
            st.success(f"Added {len(preview_cols)} column(s) for `{table_key}`.")

        if st.session_state.built_schema:
            st.markdown("**Tables added so far:**")
            for key, cols in list(st.session_state.built_schema.items()):
                row_label, row_remove = st.columns([5, 1])
                row_label.caption(f"`{key}` — {len(cols)} column(s)")
                if row_remove.button("✕", key=f"remove_schema_{key}"):
                    del st.session_state.built_schema[key]
                    st.rerun()

            st.download_button(
                "⬇ Download schema.json",
                data=json.dumps(
                    {k: {"columns": v} for k, v in st.session_state.built_schema.items()}, indent=2,
                ).encode("utf-8"),
                file_name="schema.json",
                mime="application/json",
                use_container_width=True,
            )

    schema_file = st.file_uploader(
        "...or upload an existing schema.json", type=["json"], key="schema_upload",
    )

    # ── Overrides ─────────────────────────────────────────────────────────
    st.divider()
    overrides_label_col, overrides_info_col = st.columns([5, 1])
    with overrides_label_col:
        st.markdown("**Overrides** _(optional)_")
    with overrides_info_col:
        with st.popover("ℹ️", use_container_width=True):
            st.markdown(
                "**What it's for**\n\n"
                "A place to hand-write the fix, once, for anything the "
                "converter genuinely can't work out on its own — a Tableau "
                "parameter's real value (it has no way to know this — "
                "Tableau doesn't store it in the flow file), a calculation "
                "it couldn't read, or a column-rename rule on a table with "
                "no known column list.\n\n"
                "**Why use this instead of just editing the generated SQL**\n"
                "An override always wins over anything auto-detected, and "
                "it survives re-running the converter after the flow "
                "changes — editing the generated file directly doesn't, "
                "since the next conversion overwrites it.\n\n"
                "**Format** — JSON, three optional sections:\n"
                "```json\n"
                "{\n"
                '  "parameters": {\n'
                '    "Parameters.192a3229-...": "208"\n'
                "  },\n"
                '  "expressions": {\n'
                '    "some calc the tool couldn\'t read": "`fixed_sql`"\n'
                "  },\n"
                '  "bulk_renames": {\n'
                '    "Rename Fields 1": [["order id", "order_id"]]\n'
                "  }\n"
                "}\n"
                "```\n"
                "You only need to include the section(s) you're actually using."
            )
    overrides_file = st.file_uploader(
        "overrides.json", type=["json"], key="overrides_upload", label_visibility="collapsed",
    )

if not uploaded:
    st.info("Upload a `.tfl` or `.tflx` file to get started.")
    st.stop()

# ── Convert button ────────────────────────────────────────────────────────────
col_btn, col_info = st.columns([1, 5])
with col_btn:
    run = st.button("▶  Convert", type="primary", use_container_width=True)
with col_info:
    st.markdown(f"**{uploaded.name}** &nbsp;·&nbsp; {uploaded.size:,} bytes")

if not run:
    st.stop()

# ── Parse optional schema/overrides ──────────────────────────────────────────
schema_dict: dict = {}
overrides_dict = None
try:
    if schema_file is not None:
        schema_dict.update(json.loads(schema_file.getvalue()))
    if st.session_state.get("built_schema"):
        schema_dict.update({k: {"columns": v} for k, v in st.session_state.built_schema.items()})
    if overrides_file is not None:
        overrides_dict = json.loads(overrides_file.getvalue())
except json.JSONDecodeError as exc:
    st.error(f"❌ Could not parse schema/overrides JSON: {exc}")
    st.stop()

# ── Run conversion ────────────────────────────────────────────────────────────
with st.spinner("Reading the flow and generating SQL…"):
    try:
        results, warnings, coverage = convert_tfl(
            tfl_bytes=uploaded.getvalue(),
            mode=MODE,
            dataset=DATASET,
            filename=uploaded.name,
            overrides=overrides_dict,
            schema=schema_dict or None,
        )
    except (Exception, SystemExit) as exc:
        st.error(f"❌ Conversion failed: {exc}")
        st.stop()

raw_results = list(results)

# ── Coverage summary ──────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
c1.metric(
    "Nodes in flow", coverage["nodes"],
    help="Every step in your Tableau Prep flow — inputs, cleaning steps, "
         "joins, groupings, pivots, and outputs — that this tool read and "
         "converted to SQL.",
)
c2.metric(
    "Flagged for review", coverage["flagged"],
    help="Things the tool could not work out on its own and needs a human "
         "to check — for example a Tableau parameter it doesn't know the "
         "value of, or a rename rule that needs the real column list. See "
         "the list below for exactly what and where.",
)
c3.metric(
    "Calc expressions", coverage["expression_attempts"],
    help="The total number of Tableau formulas found anywhere in the flow — "
         "calculated fields, filter conditions, join conditions, and so on.",
)
c4.metric(
    "Expression accuracy", f"{coverage['expression_accuracy_pct']:.0f}%",
    help="The share of those formulas that were translated automatically "
         "with nothing flagged. 100% means every formula converted cleanly; "
         "lower than that means some need a quick look (see 'Flagged for "
         "review'). This is a different count from 'Nodes in flow' since "
         "one step can contain several formulas.",
)

if warnings:
    with st.expander(f"⚠️ {len(warnings)} item(s) need a manual check", expanded=False):
        for kind, detail in warnings:
            st.markdown(f"- **{kind}** — `{detail}`")

# ── Results ───────────────────────────────────────────────────────────────────
display_results = raw_results

st.success(f"✅ Generated {len(display_results)} file(s) from **{uploaded.name}**")

for fname, sql in display_results:
    flagged_here = sql.count("TODO")
    badge = f" · {flagged_here} to check" if flagged_here else ""
    with st.expander(f"📄 {fname}{badge}", expanded=True):
        if flagged_here:
            st.caption("🟡 Highlighted lines below need a manual check before you run this.")
        st.markdown(render_sql_with_warnings(sql), unsafe_allow_html=True)

        st.download_button(
            label=f"⬇  Download {fname}",
            data=sql.encode("utf-8"),
            file_name=fname,
            mime="text/plain",
            key=f"dl_{fname}",
        )

# ── Bulk download ─────────────────────────────────────────────────────────────
if len(display_results) > 1:
    st.divider()
    zip_bytes = make_zip(display_results)
    stem = Path(uploaded.name).stem
    st.download_button(
        label=f"⬇  Download all as {stem}_sql.zip",
        data=zip_bytes,
        file_name=f"{stem}_sql.zip",
        mime="application/zip",
    )
