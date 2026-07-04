"""
Non-UI logic for the Tableau Prep -> SQL Streamlit app.

Kept separate from streamlit_app.py so it can be imported and unit tested
without executing Streamlit's top-level script (Streamlit apps run their
whole module body as a script, which makes the UI file itself untestable
by import).
"""

import collections
import csv
import html
import io
import zipfile

import tfl_to_sql as _t
from tfl_to_sql import (
    OVERRIDES,
    PARSE_WARNINGS,
    SCHEMA,
    build_combined,
    extract_edges,
    extract_nodes,
    load_tfl,
    topological_sort,
)

# TRANSLATE_ATTEMPTS is a plain int, not a mutable container like the others
# above — `from tfl_to_sql import TRANSLATE_ATTEMPTS` would bind a stale copy
# at import time, so it's read/reset through the module (_t.TRANSLATE_ATTEMPTS)
# instead, exactly like the CLI's own reporting does.

# ---------------------------------------------------------------------------
# LLM helpers (optional cleanup pass for whatever the rule engine still flags)
# ---------------------------------------------------------------------------

_LLM_SYSTEM = """\
You are a BigQuery SQL expert. The SQL below was auto-generated from a Tableau \
Prep flow. A rule-based engine already translates most Tableau calc syntax \
(IF/THEN/ELSEIF/END -> CASE WHEN, LOD FIXED -> OVER(PARTITION BY ...), DATEADD, \
ZN, IFNULL, IIF, etc.) — only genuinely unresolved items are left as \
`NULL /* TODO: ... */` placeholders or `-- TODO` comments. Fix ONLY those \
flagged spots in place — do not restructure, rename, or "improve" anything \
that wasn't flagged.

Common flagged cases and how to resolve them:
1. `NULL /* TODO: manual LOD translation needed (INCLUDE/EXCLUDE): ... */` —
   infer the correct grain from the surrounding CTE and write the equivalent
   subquery or window function.
2. `NULL /* TODO: could not parse expression: ... */` — the original Tableau
   calc is included in the comment; translate it by hand.
3. Parameter references (`` `Parameters.<uuid>` ``) — replace with the actual
   constant if it's inferable from context, otherwise leave as NULL with the
   TODO comment intact so it stays visible.
4. `-- TODO: verify no other duplicate column names between left/right` on a
   JOIN — only act on this if you can see an actual name collision.

Return ONLY the corrected SQL — no markdown fences, no commentary."""

_PROVIDER_MODELS = {
    "Anthropic (Claude)": ["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"],
    "OpenAI": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    "Google (Gemini)": ["gemini-2.0-flash", "gemini-1.5-pro"],
    "OpenAI-compatible (custom)": ["--enter below--"],
}


def _call_anthropic(api_key: str, model: str, sql: str) -> str:
    import anthropic  # type: ignore

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=_LLM_SYSTEM,
        messages=[{"role": "user", "content": sql}],
    )
    return resp.content[0].text.strip()


def _call_openai(api_key: str, model: str, sql: str, base_url: str | None = None) -> str:
    import openai  # type: ignore

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = openai.OpenAI(**kwargs)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": sql},
        ],
        temperature=0,
    )
    return resp.choices[0].message.content.strip()


def _call_gemini(api_key: str, model: str, sql: str) -> str:
    import google.generativeai as genai  # type: ignore

    genai.configure(api_key=api_key)
    m = genai.GenerativeModel(model_name=model, system_instruction=_LLM_SYSTEM)
    return m.generate_content(sql).text.strip()


def call_llm(provider: str, api_key: str, model: str, sql: str, base_url: str | None = None) -> str:
    if provider == "Anthropic (Claude)":
        return _call_anthropic(api_key, model, sql)
    if provider in ("OpenAI", "OpenAI-compatible (custom)"):
        return _call_openai(api_key, model, sql, base_url)
    if provider == "Google (Gemini)":
        return _call_gemini(api_key, model, sql)
    raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Schema helpers — let a user build a schema.json without ever touching
# BigQuery: pick a source table from the uploaded flow, paste/upload its
# column names, done.
# ---------------------------------------------------------------------------

def list_input_tables(tfl_bytes: bytes) -> dict:
    """Return {sanitised_key: real_table_name} for every source table in an
    uploaded flow, so the UI can offer a dropdown instead of asking someone
    to type the exact internal key by hand."""
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tfl", delete=False) as f:
        f.write(tfl_bytes)
        tmp_path = f.name
    try:
        flow = load_tfl(tmp_path)
        nodes = extract_nodes(flow)
        tables = {}
        for node in nodes.values():
            cat, _raw = _t.classify_node(node)
            if cat != "INPUT":
                continue
            attrs = node.get("connectionAttributes", {})
            table = (attrs.get("datasourceName") or attrs.get("tableName")
                     or attrs.get("dbname") or node.get("name", ""))
            if table:
                tables[_t.sanitise(table)] = table
        return tables
    except SystemExit:
        return {}
    finally:
        os.unlink(tmp_path)


def parse_column_names(raw_text: str) -> list[str]:
    """Turn a CSV upload or pasted text into an ordered, de-duplicated list
    of column names. Deliberately permissive about shape — a single column
    of names (one per line), a single comma-separated row, or a full grid
    copied from somewhere else all work the same way: every non-empty cell
    is treated as one candidate column name."""
    names: list[str] = []
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(raw_text)):
        for cell in row:
            cleaned = cell.strip().strip('"').strip("'")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                names.append(cleaned)
    return names


# ---------------------------------------------------------------------------
# Warning highlighting — render generated SQL so flagged lines stand out
# in place, instead of only showing up in a separate list.
# ---------------------------------------------------------------------------

def render_sql_with_warnings(sql: str) -> str:
    """Return an HTML block (for st.markdown(..., unsafe_allow_html=True))
    with every line that needs manual review highlighted in place."""
    rendered = []
    for line in sql.split("\n"):
        escaped = html.escape(line) if line.strip() else "&nbsp;"
        if "TODO" in line:
            rendered.append(
                '<div style="background:rgba(255,180,0,0.20);'
                "border-left:3px solid #e6a700;margin:1px 0;"
                'padding:1px 8px;white-space:pre-wrap;">'
                f"⚠️&nbsp;{escaped}</div>"
            )
        else:
            rendered.append(
                f'<div style="padding:1px 8px 1px 14px;white-space:pre-wrap;">{escaped}</div>'
            )
    return (
        '<div style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;'
        "font-size:0.82rem;line-height:1.5;background:#0e1117;color:#e6e6e6;"
        'border-radius:8px;padding:10px 2px;overflow-x:auto;max-height:600px;'
        'overflow-y:auto;">' + "".join(rendered) + "</div>"
    )


# ---------------------------------------------------------------------------
# In-memory conversion wrapper
# ---------------------------------------------------------------------------

def convert_tfl(
    tfl_bytes: bytes,
    mode: str,
    dataset: str,
    filename: str,
    overrides: dict | None = None,
    schema: dict | None = None,
) -> tuple[list, list, dict]:
    """Convert uploaded .tfl bytes to SQL.

    overrides/schema are the parsed contents of an overrides.json / schema.json
    (the same format --overrides/--schema take on the CLI) — pass None for
    neither. Every call starts from a clean slate: the engine's OVERRIDES/
    SCHEMA/PARSE_WARNINGS are module-level state, and a Streamlit process
    stays alive across reruns, so leftover state from a previous upload in
    the same session must never leak into this one.

    Returns (results, warnings, coverage):
      results  -- [(output_filename, sql_string), ...]
      warnings -- PARSE_WARNINGS entries recorded during this conversion only
      coverage -- {"nodes", "flagged", "expression_attempts", "expression_accuracy_pct"}
    """
    import os
    import tempfile

    PARSE_WARNINGS.clear()
    OVERRIDES["expressions"].clear()
    OVERRIDES["parameters"].clear()
    OVERRIDES["bulk_renames"].clear()
    SCHEMA.clear()
    _t.TRANSLATE_ATTEMPTS = 0
    if overrides:
        OVERRIDES["expressions"].update(overrides.get("expressions", {}))
        OVERRIDES["parameters"].update(overrides.get("parameters", {}))
        OVERRIDES["bulk_renames"].update(overrides.get("bulk_renames", {}))
    if schema:
        for key, val in schema.items():
            SCHEMA[key] = list(val.get("columns", [])) if isinstance(val, dict) else list(val)

    with tempfile.NamedTemporaryFile(suffix=".tfl", delete=False) as f:
        f.write(tfl_bytes)
        tmp_path = f.name

    try:
        flow = load_tfl(tmp_path)
        nodes = extract_nodes(flow)
        if not nodes:
            raise RuntimeError("No nodes found in the flow file.")
        edges = extract_edges(nodes)
        order = topological_sort(nodes, edges)
        parents_map: dict = collections.defaultdict(list)
        for src, dst, ns in edges:
            parents_map[dst].append((src, ns))
        results = build_combined(nodes, order, parents_map, mode, dataset, filename)
        warnings = list(PARSE_WARNINGS)
        report = _t.build_review_report([])
        coverage = {
            "nodes": len(nodes),
            "flagged": len(warnings),
            "expression_attempts": report["summary"]["expression_translate_attempts"],
            "expression_accuracy_pct": report["summary"]["expression_accuracy_pct"],
        }
        return results, warnings, coverage
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc
    finally:
        os.unlink(tmp_path)


def make_zip(results: list[tuple[str, str]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname, sql in results:
            zf.writestr(fname, sql)
    return buf.getvalue()
