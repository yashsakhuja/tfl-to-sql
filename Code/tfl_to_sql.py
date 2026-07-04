#!/usr/bin/env python3
"""tfl_to_sql.py — Convert a Tableau Prep .tfl flow to a single end-to-end SQL/SQLX file.

Default: one combined CTE file per output node showing the complete logic end-to-end.
Use --split to produce individual files per node instead.

Expressions are translated by a small tokenizer/parser for the Tableau calc
language (not flat regexes), so nested function calls and IF/CASE blocks
translate correctly. Anything that can't be mechanically translated (LOD
INCLUDE/EXCLUDE, unparsable expressions, unknown node types) is emitted as a
syntactically-valid NULL placeholder with a TODO comment and flagged in the
"needs manual review" report printed at the end of a run — it is never
silently guessed.
"""

import argparse
import collections
import json
import os
import re
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Node classification
# ---------------------------------------------------------------------------

NODE_TYPE_MAP = {
    # INPUT
    'loadsqlproxy': 'INPUT',
    'loadsql': 'INPUT',
    'supersimpleinput': 'INPUT',
    'input': 'INPUT',
    'loadcsv': 'INPUT',
    'loadsqlstep': 'INPUT',
    # CLEAN
    'container': 'CLEAN',
    'supertransform': 'CLEAN',
    'cleantransform': 'CLEAN',
    'transform': 'CLEAN',
    # JOIN
    'superjoin': 'JOIN',
    'join': 'JOIN',
    # UNION
    'superunion': 'UNION',
    'union': 'UNION',
    # AGGREGATE
    'superaggregate': 'AGGREGATE',
    'aggregate': 'AGGREGATE',
    'aggregation': 'AGGREGATE',
    # PIVOT
    'superpivot': 'PIVOT',
    'pivot': 'PIVOT',
    # UNPIVOT
    'superunpivot': 'UNPIVOT',
    'superunpivotextended': 'UNPIVOT',
    'unpivot': 'UNPIVOT',
    'unpivotextended': 'UNPIVOT',
    # OUTPUT — covers every write-step variant seen across Tableau versions
    'writetocsvstep': 'OUTPUT',
    'writetocsv': 'OUTPUT',
    'writetodatabasestep': 'OUTPUT',
    'writetodb': 'OUTPUT',
    'writetohyper': 'OUTPUT',
    'writetotde': 'OUTPUT',
    'writetoexcel': 'OUTPUT',
    'writeto': 'OUTPUT',
    'superoutput': 'OUTPUT',
    'outputstep': 'OUTPUT',
    'output': 'OUTPUT',
}

# baseType fallback — used when nodeType string isn't in NODE_TYPE_MAP.
# Tableau's baseType is stable across versions and reliable for broad categories.
_BASE_TYPE_MAP = {
    'input': 'INPUT',
    'output': 'OUTPUT',
    'container': 'CLEAN',
    'transform': 'CLEAN',
}


def _strip_version(raw: str) -> str:
    return re.sub(r'^\.v[\d_]+\.', '', raw)


def classify_node(node: dict) -> tuple:
    """Return (category, raw_type_string). category is None if unrecognised."""
    raw = node.get('nodeType', node.get('type', ''))
    key = _strip_version(raw).lower()
    category = NODE_TYPE_MAP.get(key)
    if category is None:
        # Fall back to the more stable baseType field
        category = _BASE_TYPE_MAP.get(node.get('baseType', '').lower())
    return category, raw


# ---------------------------------------------------------------------------
# Name sanitisation
# ---------------------------------------------------------------------------

def sanitise(name: str) -> str:
    name = re.sub(r'[^a-zA-Z0-9]+', '_', name)
    name = name.lower().strip('_')
    name = re.sub(r'_+', '_', name)
    return name or 'unnamed'


# ---------------------------------------------------------------------------
# Load .tfl file
# ---------------------------------------------------------------------------

def load_tfl(path: str) -> dict:
    raw = Path(path).read_bytes()

    if raw[:2] == b'PK':
        try:
            with zipfile.ZipFile(path) as zf:
                if 'flow' in zf.namelist():
                    data = zf.read('flow')
                    start = data.index(b'{')
                    return json.loads(data[start:].decode('utf-8', errors='replace'))
        except (zipfile.BadZipFile, ValueError):
            pass

    try:
        start = raw.index(b'{')
        return json.loads(raw[start:].decode('utf-8', errors='replace'))
    except (ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"ERROR: Could not parse JSON from {path}: {exc}\n"
            "The file may be corrupted or an unsupported format."
        )


# ---------------------------------------------------------------------------
# Extract nodes and edges
# ---------------------------------------------------------------------------

def extract_nodes(flow: dict) -> dict:
    for key in ('nodes', 'nodesByName'):
        val = flow.get(key)
        if isinstance(val, dict) and val:
            return val
        if isinstance(val, list) and val:
            return {n.get('id', str(i)): n for i, n in enumerate(val)}
    doc = flow.get('flowDocument', {})
    val = doc.get('nodes', {})
    if isinstance(val, dict):
        return val
    return {}


def extract_edges(nodes: dict) -> list:
    """Return (src, dst, namespace) tuples. namespace is the destination-side
    label Tableau stamps on the edge ("Left"/"Right"/"Default" etc.) — it is
    how join nodes know which incoming edge is which side."""
    edges = []
    for node_id, node in nodes.items():
        for nxt in node.get('nextNodes', []):
            target = nxt.get('nextNodeId')
            if target:
                edges.append((node_id, target, nxt.get('nextNamespace', 'Default')))
    return edges


# ---------------------------------------------------------------------------
# Topological sort — Kahn's algorithm
# ---------------------------------------------------------------------------

def topological_sort(nodes: dict, edges: list) -> list:
    in_degree = {nid: 0 for nid in nodes}
    children = collections.defaultdict(list)

    for src, dst, _ns in edges:
        if src in in_degree and dst in in_degree:
            in_degree[dst] += 1
            children[src].append(dst)

    queue = collections.deque(nid for nid in nodes if in_degree[nid] == 0)
    order = []

    while queue:
        nid = queue.popleft()
        order.append(nid)
        for child in children[nid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if len(order) != len(nodes):
        remaining = [nid for nid in nodes if nid not in order]
        raise SystemExit(f"ERROR: Circular dependency detected among nodes: {remaining}")

    return order


# ---------------------------------------------------------------------------
# Tableau calc expression parser (tokenizer -> AST -> BigQuery emitter)
# ---------------------------------------------------------------------------

PARSE_WARNINGS: list[tuple[str, str]] = []  # (category, detail) pairs surfaced in the end-of-run report
TRANSLATE_ATTEMPTS = 0  # count of translate_expr() calls — denominator for expression-level accuracy

# Populated by load_overrides()/load_schema() from optional CLI-supplied files.
# Both stay empty (no-op) unless --overrides/--schema is passed, so default
# behaviour is unchanged. Kept stdlib-only (plain dicts loaded from JSON) so
# the core converter has no new dependency.
OVERRIDES: dict[str, dict] = {'expressions': {}, 'parameters': {}, 'bulk_renames': {}}
SCHEMA: dict[str, list] = {}  # sanitised source-table name -> list[str] of real column names


def load_overrides(path: str) -> None:
    """Load hand-authored fixes for items the parser/schema can't resolve on
    their own — keyed by the exact raw Tableau expression text (from a TODO
    comment), a Parameters.* name, or a BulkRenameColumns step name. These
    take precedence over anything schema-driven, and survive re-generation
    since they live in a file the engineer maintains, not in generated SQL."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    OVERRIDES['expressions'].update(data.get('expressions', {}))
    OVERRIDES['parameters'].update(data.get('parameters', {}))
    OVERRIDES['bulk_renames'].update(data.get('bulk_renames', {}))


def load_schema(path: str) -> None:
    """Load a {table_name: {"columns": [...]}} (or {table_name: [...]}) JSON
    cache — see tools/fetch_schema.py to generate one from BigQuery, or
    hand-write one for tables that don't exist yet."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    for key, val in data.items():
        cols = val.get('columns', []) if isinstance(val, dict) else val
        SCHEMA[key] = list(cols)


class _ParseError(Exception):
    pass


_TOKEN_RE = re.compile(r"""
    (?P<WS>\s+)
  | (?P<STRING>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
  | (?P<DATELIT>\#[^#]+\#)
  | (?P<NUMBER>\d+\.\d+|\.\d+|\d+)
  | (?P<FIELD>\[[^\]]+\])
  | (?P<COMMENT>//[^\n]*)
  | (?P<LBRACE>\{)
  | (?P<RBRACE>\})
  | (?P<LPAREN>\()
  | (?P<RPAREN>\))
  | (?P<COMMA>,)
  | (?P<COLON>:)
  | (?P<OP>==|!=|<>|<=|>=|[=<>+\-*/%])
  | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
""", re.VERBOSE)

_KEYWORDS = {
    'IF', 'THEN', 'ELSEIF', 'ELSE', 'END', 'CASE', 'WHEN',
    'AND', 'OR', 'NOT', 'NULL', 'TRUE', 'FALSE',
    'FIXED', 'INCLUDE', 'EXCLUDE',
}


def _tokenize(text: str) -> list:
    tokens = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise _ParseError(f'unexpected character {text[pos]!r} at position {pos}')
        pos = m.end()
        kind = m.lastgroup
        if kind in ('WS', 'COMMENT'):
            continue
        value = m.group()
        if kind == 'IDENT' and value.upper() in _KEYWORDS:
            kind = value.upper()
        tokens.append((kind, value, m.start(), m.end()))
    tokens.append(('EOF', '', len(text), len(text)))
    return tokens


class _Parser:
    """Recursive-descent parser for the Tableau calc mini-language."""

    def __init__(self, text: str, tokens: list):
        self.text = text
        self.tokens = tokens
        self.i = 0

    def _peek(self):
        return self.tokens[self.i]

    def _at(self, *kinds):
        return self.tokens[self.i][0] in kinds

    def _advance(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _expect(self, kind):
        tok = self._advance()
        if tok[0] != kind:
            raise _ParseError(f'expected {kind}, got {tok[0]} ({tok[1]!r})')
        return tok

    def parse(self):
        node = self._or()
        if not self._at('EOF'):
            raise _ParseError(f'unexpected trailing token {self._peek()[1]!r}')
        return node

    def _or(self):
        node = self._and()
        while self._at('OR'):
            self._advance()
            node = ('bin', 'OR', node, self._and())
        return node

    def _and(self):
        node = self._not()
        while self._at('AND'):
            self._advance()
            node = ('bin', 'AND', node, self._not())
        return node

    def _not(self):
        if self._at('NOT'):
            self._advance()
            return ('unary', 'NOT', self._not())
        return self._cmp()

    def _cmp(self):
        node = self._add()
        while self._at('OP') and self._peek()[1] in ('=', '==', '!=', '<>', '<', '<=', '>', '>='):
            op = self._advance()[1]
            node = ('bin', {'==': '='}.get(op, op), node, self._add())
        return node

    def _add(self):
        node = self._mul()
        while self._at('OP') and self._peek()[1] in ('+', '-'):
            op = self._advance()[1]
            node = ('bin', op, node, self._mul())
        return node

    def _mul(self):
        node = self._unary()
        while self._at('OP') and self._peek()[1] in ('*', '/', '%'):
            op = self._advance()[1]
            node = ('bin', op, node, self._unary())
        return node

    def _unary(self):
        if self._at('OP') and self._peek()[1] in ('-', '+'):
            op = self._advance()[1]
            return ('unary', op, self._unary())
        return self._primary()

    def _primary(self):
        kind, value, start, _end = self._peek()

        if kind == 'NUMBER':
            self._advance()
            return ('lit', 'num', value)
        if kind == 'STRING':
            self._advance()
            return ('lit', 'str', value)
        if kind == 'DATELIT':
            # Tableau's #2022-05-01# / #2022-05-01 10:30:00# literal syntax —
            # translate to a plain quoted string, which DATE()/DATETIME()/
            # TIMESTAMP() all accept directly.
            self._advance()
            return ('lit', 'str', f"'{value[1:-1].strip()}'")
        if kind == 'NULL':
            self._advance()
            return ('lit', 'null', 'NULL')
        if kind in ('TRUE', 'FALSE'):
            self._advance()
            return ('lit', 'bool', kind)
        if kind == 'FIELD':
            self._advance()
            return ('field', value[1:-1])
        if kind == 'LPAREN':
            self._advance()
            node = ('group', self._or())
            self._expect('RPAREN')
            return node
        if kind == 'LBRACE':
            return self._brace_block()
        if kind == 'IF':
            return self._if_expr()
        if kind == 'CASE':
            return self._case_expr()
        if kind == 'IDENT':
            self._advance()
            if self._at('LPAREN'):
                self._advance()
                args = []
                if not self._at('RPAREN'):
                    args.append(self._or())
                    while self._at('COMMA'):
                        self._advance()
                        args.append(self._or())
                self._expect('RPAREN')
                return ('call', value, args)
            # Tableau allows referencing a field without [brackets] when the
            # name is unambiguous, e.g. `fy_q1_demand / total_lifetime_demand`.
            return ('field', value)

        raise _ParseError(f'unexpected token {kind} ({value!r}) at position {start}')

    def _if_expr(self):
        self._expect('IF')
        branches = []
        cond = self._or()
        self._expect('THEN')
        result = self._or()
        branches.append((cond, result))
        while self._at('ELSEIF'):
            self._advance()
            cond = self._or()
            self._expect('THEN')
            result = self._or()
            branches.append((cond, result))
        else_ = None
        if self._at('ELSE'):
            self._advance()
            else_ = self._or()
        self._expect('END')
        return ('if', branches, else_)

    def _case_expr(self):
        self._expect('CASE')
        switch = self._or()
        branches = []
        while self._at('WHEN'):
            self._advance()
            val = self._or()
            self._expect('THEN')
            result = self._or()
            branches.append((val, result))
        else_ = None
        if self._at('ELSE'):
            self._advance()
            else_ = self._or()
        self._expect('END')
        return ('case', switch, branches, else_)

    def _brace_block(self):
        """Parses both LOD blocks ({FIXED/INCLUDE/EXCLUDE dims: expr}) and
        Tableau Prep's window-calc blocks ({PARTITION dims: {ORDERBY cols:
        call}}), used for ROW_NUMBER()/RANK()/LOOKUP() style duplicate
        detection and sequencing. Both share the same {KEYWORD ...: inner}
        shape, just with different keyword-specific list grammar."""
        start = self._peek()[2]
        open_count = 0
        while self._at('LBRACE'):
            self._advance()
            open_count += 1
        kind_tok = self._advance()
        kind = kind_tok[1].upper()

        def _close(end):
            # Consume exactly as many '}' as this block opened with — not
            # "every '}' available", which would swallow braces that belong
            # to an *outer* block when this one is nested inside a larger
            # expression (e.g. `({PARTITION ...: {ORDERBY ...}} = 1)`).
            closed = 0
            while closed < open_count and self._at('RBRACE'):
                end = self._advance()[3]
                closed += 1
            return end

        if kind == 'ORDERBY':
            order_list = []
            while (self._at('FIELD') or self._at('COMMA')
                   or (self._at('IDENT') and self._peek()[1].upper() in ('ASC', 'DESC'))):
                tok = self._advance()
                if tok[0] == 'FIELD':
                    order_list.append([tok[1][1:-1], 'ASC'])
                elif tok[0] == 'IDENT':
                    order_list[-1][1] = tok[1].upper()
            self._expect('COLON')
            inner = self._or()
            end = _close(self._peek()[3])
            return ('orderby', order_list, inner, self.text[start:end])

        # FIXED / INCLUDE / EXCLUDE / PARTITION all share plain-dims grammar.
        dims = []
        while self._at('FIELD') or self._at('COMMA'):
            tok = self._advance()
            if tok[0] == 'FIELD':
                dims.append(tok[1][1:-1])
        self._expect('COLON')
        inner = self._or()
        end = _close(self._peek()[3])

        if kind == 'PARTITION':
            return ('partition', dims, inner, self.text[start:end])
        return ('lod', kind, dims, inner, self.text[start:end])


def _emit_lit_word(node) -> str:
    """Extract the literal unit word out of a string-literal AST node, e.g. 'week' -> WEEK."""
    if node[0] == 'lit' and node[1] == 'str':
        return node[2].strip('\'"').upper()
    return _emit(node).strip('`\'"').upper()


def _emit_window_call(call_node) -> str:
    """Emit the function part of a PARTITION/ORDERBY window-calc block.
    LOOKUP is Tableau's table-calc offset function (negative = rows before,
    positive = rows after) and needs remapping to LAG/LEAD; everything else
    (ROW_NUMBER, RANK, ...) is already a valid BigQuery analytic function."""
    _, name, args = call_node
    if name.upper() == 'LOOKUP' and len(args) == 2:
        target = _emit(args[0])
        offset_text = _emit(args[1])
        try:
            offset = int(offset_text)
        except ValueError:
            offset = None
        if offset is not None and offset < 0:
            return f'LAG({target}, {abs(offset)})'
        if offset is not None and offset > 0:
            return f'LEAD({target}, {offset})'
        return target
    return _emit_call(name, args)


def _emit_call(name: str, args: list) -> str:
    n = name.upper()
    eargs = [_emit(a) for a in args]

    if n == 'ZN' and len(eargs) == 1:
        return f'COALESCE({eargs[0]}, 0)'
    if n == 'ISNULL' and len(eargs) == 1:
        return f'{eargs[0]} IS NULL'
    if n == 'IFNULL' and len(eargs) == 2:
        return f'COALESCE({eargs[0]}, {eargs[1]})'
    if n == 'IIF' and len(eargs) in (2, 3):
        return f'IF({", ".join(eargs)})'
    if n == 'COUNTD' and len(eargs) == 1:
        return f'COUNT(DISTINCT {eargs[0]})'
    if n == 'STDEV' and len(eargs) == 1:
        return f'STDDEV({eargs[0]})'
    if n == 'LEFT' and len(eargs) == 2:
        return f'SUBSTR({eargs[0]}, 1, {eargs[1]})'
    if n == 'CONTAINS' and len(eargs) == 2:
        return f'STRPOS({eargs[0]}, {eargs[1]}) > 0'
    if n == 'INT' and len(eargs) == 1:
        return f'CAST({eargs[0]} AS INT64)'
    if n == 'FLOAT' and len(eargs) == 1:
        return f'CAST({eargs[0]} AS FLOAT64)'
    if n == 'STR' and len(eargs) == 1:
        return f'CAST({eargs[0]} AS STRING)'
    if n == 'TODAY' and not eargs:
        return 'CURRENT_DATE()'
    if n == 'NOW' and not eargs:
        return 'CURRENT_TIMESTAMP()'
    if n == 'DATEADD' and len(args) == 3:
        unit = _emit_lit_word(args[0])
        return f'DATE_ADD({eargs[2]}, INTERVAL {eargs[1]} {unit})'
    if n == 'DATEDIFF' and len(args) == 3:
        unit = _emit_lit_word(args[0])
        return f'DATE_DIFF({eargs[2]}, {eargs[1]}, {unit})'
    if n == 'DATETRUNC' and len(args) in (2, 3):
        unit = _emit_lit_word(args[0])
        if len(args) == 3:
            start_day = _emit_lit_word(args[2])
            return f'DATE_TRUNC({eargs[1]}, {unit}({start_day}))'
        return f'DATE_TRUNC({eargs[1]}, {unit})'
    if n == 'DATEPARSE' and len(eargs) == 2:
        return f'PARSE_DATE({eargs[0]}, {eargs[1]})'

    return f'{name}({", ".join(eargs)})'


def _emit(node) -> str:
    tag = node[0]

    if tag == 'lit':
        return node[2]

    if tag == 'field':
        name = node[1]
        if name.startswith('Parameters.'):
            override = OVERRIDES['parameters'].get(name)
            if override is not None:
                return override
            PARSE_WARNINGS.append(('parameter reference (needs manual binding)', name))
            return f'`{name}`'
        return _col_id(name)

    if tag == 'group':
        return f'({_emit(node[1])})'

    if tag == 'unary':
        _, op, operand = node
        return f'{op} {_emit(operand)}' if op == 'NOT' else f'{op}{_emit(operand)}'

    if tag == 'bin':
        _, op, left, right = node
        return f'{_emit(left)} {op} {_emit(right)}'

    if tag == 'if':
        _, branches, else_ = node
        parts = ['CASE']
        for cond, result in branches:
            parts.append(f'WHEN {_emit(cond)} THEN {_emit(result)}')
        if else_ is not None:
            parts.append(f'ELSE {_emit(else_)}')
        parts.append('END')
        return '\n'.join(parts)

    if tag == 'case':
        _, switch, branches, else_ = node
        parts = [f'CASE {_emit(switch)}']
        for val, result in branches:
            parts.append(f'WHEN {_emit(val)} THEN {_emit(result)}')
        if else_ is not None:
            parts.append(f'ELSE {_emit(else_)}')
        parts.append('END')
        return '\n'.join(parts)

    if tag == 'lod':
        _, kind, dims, inner, raw = node
        if kind == 'FIXED' and inner[0] == 'call':
            dims_sql = ', '.join(_col_id(d) for d in dims)
            partition = f'PARTITION BY {dims_sql}' if dims_sql else ''
            return f'{_emit(inner)} OVER ({partition})'
        PARSE_WARNINGS.append((f'LOD expression ({kind}) needs manual translation', raw))
        return f'NULL /* TODO: manual LOD translation needed ({kind}): {raw} */'

    if tag == 'partition':
        # Tableau Prep's {PARTITION dims: {ORDERBY cols: FUNC()}} window-calc
        # block — used for ROW_NUMBER()/RANK()/LOOKUP() duplicate-detection
        # and sequencing steps. Maps directly onto a SQL analytic function.
        _, dims, inner, raw = node
        if inner[0] == 'orderby' and inner[2][0] == 'call':
            _, order_list, call_node, _inner_raw = inner
            dims_sql = ', '.join(_col_id(d) for d in dims)
            order_sql = ', '.join(f'{_col_id(f)} {d}' for f, d in order_list)
            over_parts = ' '.join(p for p in (
                f'PARTITION BY {dims_sql}' if dims_sql else '',
                f'ORDER BY {order_sql}' if order_sql else '',
            ) if p)
            return f'{_emit_window_call(call_node)} OVER ({over_parts})'
        PARSE_WARNINGS.append(('PARTITION window-calc needs manual translation', raw))
        return f'NULL /* TODO: manual window-calc translation needed: {raw} */'

    if tag == 'orderby':
        # Only reached if an ORDERBY block shows up somewhere other than
        # directly inside a PARTITION block — an unsupported shape, flagged
        # rather than guessed at.
        _, _order_list, _inner, raw = node
        PARSE_WARNINGS.append(('ORDERBY block outside PARTITION needs manual translation', raw))
        return f'NULL /* TODO: unexpected ORDERBY block: {raw} */'

    if tag == 'call':
        return _emit_call(node[1], node[2])

    raise _ParseError(f'unhandled AST node {tag}')


def translate_expr(expr: str, context: str = 'expression') -> str:
    """Translate a Tableau calc expression into BigQuery SQL text.

    Never raises: on any parse failure, returns a NULL placeholder carrying
    a TODO comment with the original text, and records the failure in
    PARSE_WARNINGS for the end-of-run summary.
    """
    if not expr or not expr.strip():
        return 'NULL'

    global TRANSLATE_ATTEMPTS
    TRANSLATE_ATTEMPTS += 1

    if expr in OVERRIDES['expressions']:
        return OVERRIDES['expressions'][expr]

    # // line comments are pulled out and re-emitted as leading SQL comments
    # above the translated expression rather than fed to the parser.
    comment_lines = []
    code_lines = []
    for line in expr.split('\n'):
        stripped = line.strip()
        if stripped.startswith('//'):
            comment_lines.append(f'-- {stripped[2:].strip()}')
        else:
            code_lines.append(line)
    code = '\n'.join(code_lines).strip()

    if not code:
        return '\n'.join(comment_lines) if comment_lines else 'NULL'

    try:
        tokens = _tokenize(code)
        node = _Parser(code, tokens).parse()
        translated = _emit(node)
    except _ParseError as exc:
        PARSE_WARNINGS.append((f'could not parse {context}', f'{expr!r} ({exc})'))
        translated = f'NULL /* TODO: could not parse expression: {expr!r} */'

    if comment_lines:
        return '\n'.join(comment_lines) + '\n' + translated
    return translated


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def ref(name: str, mode: str) -> str:
    return f"${{ref('{name}')}}" if mode == 'dataform' else f'`{name}`'


def _bracket_field_name(expr_text: str) -> str | None:
    """Pull the raw (untransformed) field name out of a simple `[Field Name]`
    join-condition expression — used for schema-set bookkeeping, which must
    stay in the original casing to match --schema/--overrides consistently.
    Returns None if the expression isn't a simple bracketed field ref."""
    m = re.search(r'\[([^\]]+)\]', expr_text or '')
    return m.group(1) if m else None


def _col_id(name) -> str:
    """Render a Tableau field/column name as the single canonical,
    UPPER_CASE / underscore-separated, backtick-quoted identifier used for
    every column reference in generated SQL (table/CTE names and the
    Dataform config block are untouched — this is only for fields).

    Applied at the point each generator turns a field name into literal SQL
    text; internal bookkeeping (schema tracking, --overrides/--schema
    matching, warning messages) keeps using the original Tableau name
    throughout, so existing schema.json/overrides.json files still match
    unchanged. BigQuery resolves column names case-insensitively, so this
    is purely cosmetic/consistency — it doesn't change what a reference
    actually resolves to, even against a real table whose column happens to
    be lowercase."""
    canonical = re.sub(r'[^0-9A-Za-z]+', '_', str(name or '')).strip('_').upper()
    return f'`{canonical or "_"}`'


def _commas(lines: list) -> list:
    return [l + ',' if i < len(lines) - 1 else l for i, l in enumerate(lines)]


def _layer_body(step: tuple, ref_name: str) -> str:
    """Render one rename/add/remove/filter step as a SELECT against ref_name."""
    kind = step[0]
    if kind == 'rename':
        _, old, new = step
        return f'SELECT * EXCEPT ({_col_id(old)}), {_col_id(old)} AS {_col_id(new)}\nFROM {ref_name}'
    if kind == 'add':
        _, col, expr_sql = step
        return f'SELECT *,\n  {expr_sql} AS {_col_id(col)}\nFROM {ref_name}'
    if kind == 'remove':
        _, cols = step
        excepts = ', '.join(_col_id(c) for c in cols)
        return f'SELECT * EXCEPT ({excepts})\nFROM {ref_name}'
    if kind == 'keep':
        _, cols = step
        col_list = ', '.join(_col_id(c) for c in cols)
        return f'SELECT {col_list}\nFROM {ref_name}'
    if kind == 'replace':
        _, col, expr_sql = step
        return f'SELECT * EXCEPT ({_col_id(col)}),\n  {expr_sql} AS {_col_id(col)}\nFROM {ref_name}'
    # 'filter'
    _, cond_sql = step
    return f'SELECT *\nFROM {ref_name}\nWHERE {cond_sql}'


def _layers_to_sql(steps: list, base_ref: str, prefix: str = 's') -> str:
    """Chain steps into nested subqueries so every later step can safely
    reference an earlier step's alias as a real, materialised column —
    BigQuery doesn't allow a WHERE clause or a sibling SELECT expression to
    reference another alias defined in the same SELECT list."""
    if not steps:
        return f'SELECT *\nFROM {base_ref}'

    lines = ['WITH']
    current_ref = base_ref
    n = len(steps)
    for idx, step in enumerate(steps):
        alias = f'{prefix}{idx}'
        body = _layer_body(step, current_ref)
        indented = '\n'.join('  ' + l if l.strip() else '' for l in body.split('\n'))
        lines.append(f'{alias} AS (')
        lines.append(indented)
        lines.append(')' if idx == n - 1 else '),')
        current_ref = alias
    lines.append(f'SELECT * FROM {current_ref}')
    return '\n'.join(lines)


def _annotation_steps(annotations: list) -> list:
    """Turn beforeActionAnnotations/afterActionAnnotations into rename/remove
    step tuples, same shape as clean-step tuples."""
    steps: list[tuple] = []
    for ann in annotations:
        ann_node = ann.get('annotationNode', {})
        atype = _strip_version(ann_node.get('nodeType', '')).lower()
        if atype == 'renamecolumn':
            old = ann_node.get('columnName', '')
            new = ann_node.get('rename', '')
            if old and new:
                steps.append(('rename', old, new))
        elif atype == 'removecolumns':
            cols = ann_node.get('columnNames', [])
            if cols:
                steps.append(('remove', cols))
    return steps


def _wrap_with_annotations(parent_ref: str, before_annotations: list, core_fn, after_annotations: list) -> str:
    """Apply pre-step renames, then core_fn(ref) -> SELECT body, then post-step
    renames/removes, chaining everything as nested subqueries."""
    before_steps = _annotation_steps(before_annotations)
    after_steps = _annotation_steps(after_annotations)

    if not before_steps and not after_steps:
        return core_fn(parent_ref)

    segments = []
    cur = parent_ref
    for idx, step in enumerate(before_steps):
        alias = f'pre{idx}'
        segments.append((alias, _layer_body(step, cur)))
        cur = alias

    segments.append(('core', core_fn(cur)))
    cur = 'core'

    for idx, step in enumerate(after_steps):
        alias = f'post{idx}'
        segments.append((alias, _layer_body(step, cur)))
        cur = alias

    lines = ['WITH']
    n = len(segments)
    for i, (alias, body) in enumerate(segments):
        indented = '\n'.join('  ' + l if l.strip() else '' for l in body.split('\n'))
        lines.append(f'{alias} AS (')
        lines.append(indented)
        lines.append(')' if i == n - 1 else '),')
    lines.append(f'SELECT * FROM {cur}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Clean-step container extraction
# ---------------------------------------------------------------------------

def _ordered_container_steps(node: dict) -> list:
    """Return a CLEAN container's sub-nodes in true execution order.

    Tableau stores a clean container's steps as a linear pipeline: each
    sub-node's own nextNodes[0] points at the next sub-node id within the
    same container. Iterating loomContainer.nodes.values() directly (the
    previous approach) reflects JSON/dict order, not execution order."""
    sub_nodes = node.get('loomContainer', {}).get('nodes', {})
    if not sub_nodes:
        return []

    targets = set()
    for s in sub_nodes.values():
        for nxt in s.get('nextNodes', []):
            tgt = nxt.get('nextNodeId')
            if tgt:
                targets.add(tgt)

    start_ids = [sid for sid in sub_nodes if sid not in targets]
    if not start_ids:
        return list(sub_nodes.values())  # unexpected shape — don't crash

    ordered = []
    seen = set()
    cur = start_ids[0]
    while cur and cur in sub_nodes and cur not in seen:
        seen.add(cur)
        ordered.append(sub_nodes[cur])
        nxt_list = sub_nodes[cur].get('nextNodes', [])
        cur = nxt_list[0].get('nextNodeId') if nxt_list else None

    for sid, s in sub_nodes.items():  # defensive: pick up any unreached sub-nodes
        if sid not in seen:
            ordered.append(s)

    return ordered


def _apply_steps_to_columns(cols, steps: list):
    """Project a column-name set forward through rename/add/remove/keep/replace
    steps. Returns None if cols is None (schema untracked from here on)."""
    if cols is None:
        return None
    cols = set(cols)
    for step in steps:
        kind = step[0]
        if kind == 'rename':
            _, old, new = step
            cols.discard(old)
            cols.add(new)
        elif kind in ('add', 'replace'):
            cols.add(step[1])
        elif kind == 'remove':
            cols.difference_update(step[1])
        elif kind == 'keep':
            cols = set(step[1])
        # 'filter' -> no column-shape effect
    return cols


def _resolve_bulk_rename(sub_node: dict, cols) -> list:
    """Pure (no warnings) resolution of a BulkRenameColumns rule against a
    known column set: which (old, new) pairs would it actually produce?
    Returns [] if cols is unknown or the rule's operation type isn't one of
    the ones below (Tableau has more; unhandled ones fall back to the
    flagged, needs-manual-translation path rather than guessing)."""
    if cols is None:
        return []
    sel = sub_node.get('columnsSelection', {})
    op = sub_node.get('columnsOperation', {})
    op_type = op.get('type', '')
    exempt = set(sel.get('exemptedColumns', []))
    included = set(sel.get('includedColumns') or [])

    def rename(col: str) -> str:
        if op_type == 'replaceColumnAllSubStringOperation':
            old_sub, new_sub = op.get('existingSubString', ''), op.get('newSubString', '')
            return col.replace(old_sub, new_sub) if old_sub and old_sub in col else col
        if op_type == 'addColumnPrefixOperation':
            return op.get('columnNamePrefix', '') + col
        if op_type == 'addColumnSuffixOperation':
            return col + op.get('columnNameSuffix', '')
        if op_type in ('upperCaseColumnOperation', 'toUpperCaseOperation'):
            return col.upper()
        if op_type in ('lowerCaseColumnOperation', 'toLowerCaseOperation'):
            return col.lower()
        return col

    if op_type not in (
        'replaceColumnAllSubStringOperation', 'addColumnPrefixOperation', 'addColumnSuffixOperation',
        'upperCaseColumnOperation', 'toUpperCaseOperation', 'lowerCaseColumnOperation', 'toLowerCaseOperation',
    ):
        return []

    pairs = []
    for col in sorted(cols):
        if col in exempt or (included and col not in included):
            continue
        new_name = rename(col)
        if new_name != col:
            pairs.append((col, new_name))
    return pairs


def _clean_container_result_columns(node: dict, known_cols):
    """Pure column-set projection through a CLEAN container (no translate_expr
    calls, no PARSE_WARNINGS side effects). Used only to seed node_columns in
    the schema pre-pass — the real, warning-emitting walk happens exactly
    once, in _extract_clean_steps, during actual SQL generation."""
    if known_cols is None:
        return None
    cols = set(known_cols)
    for sub_node in _ordered_container_steps(node):
        stype = _strip_version(sub_node.get('nodeType', '')).lower()
        if stype == 'renamecolumn':
            old, new = sub_node.get('columnName', ''), sub_node.get('rename', '')
            if old and new:
                cols.discard(old)
                cols.add(new)
        elif stype in ('addcolumn', 'duplicatecolumn', 'quickcalccolumn'):
            col = sub_node.get('columnName', '')
            if col:
                cols.add(col)
        elif stype == 'removecolumns':
            cols.difference_update(sub_node.get('columnNames', []))
        elif stype == 'keeponlycolumns':
            keep = sub_node.get('columnNames', [])
            if keep:
                cols = set(keep)
        elif stype == 'bulkrenamecolumns':
            sname = sub_node.get('name', stype)
            pairs = OVERRIDES['bulk_renames'].get(sname) or _resolve_bulk_rename(sub_node, cols)
            for old, new in pairs:
                cols.discard(old)
                cols.add(new)
    return cols


_TABLEAU_TYPE_TO_BQ = {
    'real': 'FLOAT64', 'float': 'FLOAT64', 'number (decimal)': 'FLOAT64',
    'integer': 'INT64', 'int': 'INT64', 'number (whole)': 'INT64',
    'string': 'STRING', 'text': 'STRING',
    'date': 'DATE', 'datetime': 'DATETIME',
    'boolean': 'BOOL', 'bool': 'BOOL',
}


def _extract_clean_steps(node: dict, known_cols=None) -> tuple:
    """Return (steps, result_cols) describing what a CLEAN container does, in
    the same order Tableau Prep applies them:
      ('rename', old, new) / ('add', col, sql) / ('remove', [col,...]) /
      ('keep', [col,...]) / ('replace', col, sql) / ('filter', sql)

    known_cols is the tracked incoming column set (or None if schema isn't
    known for this branch) — see --schema. When known, it unlocks:
      - real BulkRenameColumns application (falls back to a flagged warning
        when neither an override nor a tracked schema is available)
      - a check that renamed/removed/kept column names actually exist
    Unrecognised sub-step types are always skipped and logged to PARSE_WARNINGS."""
    steps: list[tuple] = []
    cols = set(known_cols) if known_cols is not None else None

    for sub_node in _ordered_container_steps(node):
        stype = _strip_version(sub_node.get('nodeType', '')).lower()
        sname = sub_node.get('name', stype)

        if stype == 'renamecolumn':
            old = sub_node.get('columnName', '')
            new = sub_node.get('rename', '')
            if old and new:
                if cols is not None and old not in cols:
                    PARSE_WARNINGS.append(('renamed column not found in tracked schema — verify manually', f'{sname}: "{old}" -> "{new}"'))
                steps.append(('rename', old, new))
                cols = _apply_steps_to_columns(cols, [('rename', old, new)])

        elif stype == 'addcolumn':
            col = sub_node.get('columnName', '')
            expr = sub_node.get('expression', '')
            if col:
                steps.append(('add', col, translate_expr(expr, context=f'AddColumn "{col}"')))
                cols = _apply_steps_to_columns(cols, [('add', col)])

        elif stype == 'removecolumns':
            remove_cols = sub_node.get('columnNames', [])
            if remove_cols:
                if cols is not None:
                    missing = [c for c in remove_cols if c not in cols]
                    if missing:
                        PARSE_WARNINGS.append(('removed column not found in tracked schema — verify manually', f'{sname}: {missing}'))
                steps.append(('remove', remove_cols))
                cols = _apply_steps_to_columns(cols, [('remove', remove_cols)])

        elif stype == 'richrangefilter':
            col = sub_node.get('columnName', '')
            data_type = (sub_node.get('dataType') or '').lower()
            applied_min, applied_max = sub_node.get('appliedMin'), sub_node.get('appliedMax')
            if col and applied_min is not None and applied_max is not None:
                if data_type == 'datetime':
                    min_sql = f'TIMESTAMP_MILLIS(CAST({applied_min} AS INT64))'
                    max_sql = f'TIMESTAMP_MILLIS(CAST({applied_max} AS INT64))'
                elif data_type == 'date':
                    min_sql = f'DATE(TIMESTAMP_MILLIS(CAST({applied_min} AS INT64)))'
                    max_sql = f'DATE(TIMESTAMP_MILLIS(CAST({applied_max} AS INT64)))'
                else:
                    min_sql, max_sql = applied_min, applied_max
                cond = f'{_col_id(col)} BETWEEN {min_sql} AND {max_sql}'
                if sub_node.get('includeNulls'):
                    cond = f'({cond} OR {_col_id(col)} IS NULL)'
                steps.append(('filter', cond))

        elif stype in ('valuefilter', 'richdiscretevaluefilter'):
            exclude = sub_node.get('exclude', False)
            for col, value_list in sub_node.get('values', {}).items():
                if not value_list:
                    continue
                nulls = [v for v in value_list if v is None]
                non_nulls = [v for v in value_list if v is not None]
                cleaned = [v.strip('"').strip("'") for v in non_nulls]
                parts = []
                if nulls:
                    parts.append(f'{_col_id(col)} IS {"NOT " if exclude else ""}NULL')
                if cleaned:
                    op = 'NOT IN' if exclude else 'IN'
                    if {v.lower() for v in cleaned} <= {'true', 'false'}:
                        bool_vals = ', '.join(v.upper() for v in cleaned)
                        parts.append(f'{_col_id(col)} {op} ({bool_vals})')
                    else:
                        quoted = ', '.join(f"'{v}'" for v in cleaned)
                        parts.append(f'{_col_id(col)} {op} ({quoted})')
                if parts:
                    joiner = ' AND ' if exclude else ' OR '
                    steps.append(('filter', joiner.join(parts)))

        elif stype in ('filteroperation', 'filter', 'rangefilter', 'calculationfilter'):
            expr = sub_node.get('filterExpression', sub_node.get('expression', sub_node.get('filterClause', '')))
            if expr:
                steps.append(('filter', translate_expr(expr, context=f'Filter "{sname}"')))

        elif stype == 'keeponlycolumns':
            keep_cols = sub_node.get('columnNames', [])
            if keep_cols:
                if cols is not None:
                    missing = [c for c in keep_cols if c not in cols]
                    if missing:
                        PARSE_WARNINGS.append(('kept column not found in tracked schema — verify manually', f'{sname}: {missing}'))
                steps.append(('keep', keep_cols))
                cols = _apply_steps_to_columns(cols, [('keep', keep_cols)])

        elif stype == 'duplicatecolumn':
            col = sub_node.get('columnName', '')
            expr = sub_node.get('expression', '')
            if col:
                steps.append(('add', col, translate_expr(expr, context=f'DuplicateColumn "{col}"')))
                cols = _apply_steps_to_columns(cols, [('add', col)])

        elif stype == 'quickcalccolumn':
            col = sub_node.get('columnName', '')
            expr = sub_node.get('expression', '')
            if col:
                steps.append(('replace', col, translate_expr(expr, context=f'QuickCalc "{col}"')))
                cols = _apply_steps_to_columns(cols, [('replace', col)])

        elif stype == 'changecolumntype':
            for col, info in sub_node.get('fields', {}).items():
                calc = info.get('calc')
                if calc:
                    # Tableau already emits a full self-contained conversion
                    # expression (e.g. a DATEPARSE fallback chain) — use it as-is.
                    sql = translate_expr(calc, context=f'ChangeColumnType "{col}"')
                else:
                    bq_type = _TABLEAU_TYPE_TO_BQ.get((info.get('type') or '').lower(), 'STRING')
                    sql = f'CAST({_col_id(col)} AS {bq_type})'
                steps.append(('replace', col, sql))
                cols = _apply_steps_to_columns(cols, [('replace', col)])

        elif stype == 'remap':
            col = sub_node.get('columnName', '')
            value_groups = sub_node.get('values', {})
            if col and value_groups:
                when_parts = []
                for new_val, old_vals in value_groups.items():
                    cleaned_olds = [v.strip('"').strip("'") for v in old_vals]
                    quoted_olds = ', '.join(f"'{v}'" for v in cleaned_olds)
                    new_clean = new_val.strip('"').strip("'")
                    when_parts.append(f"  WHEN {_col_id(col)} IN ({quoted_olds}) THEN '{new_clean}'")
                case_sql = 'CASE\n' + '\n'.join(when_parts) + f'\n  ELSE {_col_id(col)}\nEND'
                steps.append(('replace', col, case_sql))
                cols = _apply_steps_to_columns(cols, [('replace', col)])

        elif stype == 'bulkrenamecolumns':
            override_pairs = OVERRIDES['bulk_renames'].get(sname)
            pairs = override_pairs if override_pairs is not None else _resolve_bulk_rename(sub_node, cols)
            if pairs:
                source = 'override' if override_pairs is not None else 'tracked schema'
                for old, new in pairs:
                    steps.append(('rename', old, new))
                cols = _apply_steps_to_columns(cols, [('rename', o, n) for o, n in pairs])
                PARSE_WARNINGS.append((f'BulkRenameColumns applied from {source} — spot-check the result', f'{sname}: {pairs}'))
            else:
                sel = sub_node.get('columnsSelection', {})
                op = sub_node.get('columnsOperation', {})
                detail = (
                    f'{sname}: column-NAME substring replace '
                    f'("{op.get("existingSubString", "")}" -> "{op.get("newSubString", "")}"), '
                    f'exempted: {sel.get("exemptedColumns", [])} '
                    '— needs an --overrides entry or a --schema for this branch to apply safely'
                )
                PARSE_WARNINGS.append(('BulkRenameColumns needs manual translation (renames column NAMES via a rule)', detail))
                cols = None  # can no longer be trusted past this point

        elif stype in ('reordercolumns', 'reordercolumn'):
            continue  # cosmetic only, no effect on data

        else:
            PARSE_WARNINGS.append(('unhandled clean-step type — verify manually', f'{sname} ({stype})'))
            cols = None  # unknown effect on schema — stop tracking past this point

    return steps, cols


# ---------------------------------------------------------------------------
# CTE body generators  (no config block; suitable for embedding in WITH clause)
# ---------------------------------------------------------------------------

def _cte_clean(node: dict, parent_ref: str, known_cols=None) -> str:
    steps, _result_cols = _extract_clean_steps(node, known_cols)
    return _layers_to_sql(steps, parent_ref)


def _cte_aggregate(node: dict, parent_ref: str) -> str:
    action = node.get('actionNode', node)
    group_fields = action.get('groupByFields', action.get('dimensions', []))
    agg_fields = action.get(
        'aggregateFields',
        action.get('aggregateExpressions', action.get('measures', []))
    )

    _AGG_FUNC_MAP = {
        'COUNTD': 'COUNT(DISTINCT {col})',
        'STDEV': 'STDDEV({col})',
    }

    def core(cur_ref: str) -> str:
        select_parts = []
        for gf in group_fields:
            col = gf.get('columnName', gf) if isinstance(gf, dict) else str(gf)
            select_parts.append(_col_id(col))

        for af in agg_fields:
            if isinstance(af, dict):
                col = af.get('columnName', af.get('name', 'unknown'))
                func = af.get('function', 'SUM').upper()
                out_col = af.get('newColumnName') or col
                if func in _AGG_FUNC_MAP:
                    call = _AGG_FUNC_MAP[func].format(col=_col_id(col))
                    select_parts.append(f'{call} AS {_col_id(out_col)}')
                else:
                    select_parts.append(f'{func}({_col_id(col)}) AS {_col_id(out_col)}')
            else:
                select_parts.append(f'-- TODO: {af}')

        lines = ['SELECT']
        lines += _commas(['  ' + p for p in select_parts])
        lines.append(f'FROM {cur_ref}')
        if group_fields:
            group_cols = ', '.join(
                _col_id(gf.get('columnName', gf)) if isinstance(gf, dict) else _col_id(gf)
                for gf in group_fields
            )
            lines.append(f'GROUP BY {group_cols}')
        return '\n'.join(lines)

    return _wrap_with_annotations(
        parent_ref,
        node.get('beforeActionAnnotations', []),
        core,
        node.get('afterActionAnnotations', []),
    )


def _cte_join(node: dict, left_ref: str, right_ref: str, left_cols=None, right_cols=None) -> str:
    action = node.get('actionNode', node)
    raw_jt = action.get('joinType', 'inner').upper()
    join_type = {'LEFT': 'LEFT', 'RIGHT': 'RIGHT', 'FULL': 'FULL OUTER'}.get(raw_jt, 'INNER')

    before = node.get('beforeActionAnnotations', [])
    left_before = _annotation_steps([a for a in before if a.get('namespace') == 'Left'])
    right_before = _annotation_steps([a for a in before if a.get('namespace') == 'Right'])
    after_steps = _annotation_steps(node.get('afterActionAnnotations', []))

    # Project the tracked column sets through this join's own before-rename
    # steps so dedup reasons about the same names the final l./r. refs use.
    left_cols = _apply_steps_to_columns(left_cols, left_before)
    right_cols = _apply_steps_to_columns(right_cols, right_before)

    segments = []
    left_final, right_final = left_ref, right_ref
    for idx, step in enumerate(left_before):
        alias = f'jl{idx}'
        segments.append((alias, _layer_body(step, left_final)))
        left_final = alias
    for idx, step in enumerate(right_before):
        alias = f'jr{idx}'
        segments.append((alias, _layer_body(step, right_final)))
        right_final = alias

    conditions = action.get('joinExpressions', action.get('conditions', []))
    on_parts, right_keys = [], []
    for cond in conditions:
        lk = translate_expr(cond.get('leftExpression', cond.get('leftKey', '')), context='join condition')
        rk = translate_expr(cond.get('rightExpression', cond.get('rightKey', '')), context='join condition')
        if lk and rk:
            on_parts.append(f'l.{lk} = r.{rk}')
        # Kept in the *original* casing (not translate_expr's already-
        # canonicalised rk) so it matches left_cols/right_cols, which come
        # from schema tracking and are still in their original casing.
        raw_right_key = _bracket_field_name(cond.get('rightExpression', cond.get('rightKey', '')))
        if raw_right_key:
            right_keys.append(raw_right_key)
    on_clause = '\n  AND '.join(on_parts) if on_parts else '-- TODO: specify join condition'

    if left_cols is not None and right_cols is not None:
        # Full schema known on both sides: dedupe every overlapping column
        # name, not just the join key — no guesswork left for the reviewer.
        dup_cols = (set(right_cols) & set(left_cols)) | set(right_keys)
        right_select = f'r.* EXCEPT ({", ".join(_col_id(k) for k in sorted(dup_cols))})' if dup_cols else 'r.*'
        dup_note = ''
    else:
        right_select = 'r.*'
        if right_keys:
            excepts = ', '.join(_col_id(k) for k in right_keys)
            right_select = f'r.* EXCEPT ({excepts})'
        dup_note = '  -- TODO: verify no other duplicate column names between left/right'

    core_body = '\n'.join([
        'SELECT',
        '  l.*,',
        f'  {right_select}{dup_note}',
        f'FROM {left_final} AS l',
        f'{join_type} JOIN {right_final} AS r',
        f'  ON {on_clause}',
    ])

    if not segments and not after_steps:
        return core_body

    segments.append(('core', core_body))
    cur = 'core'
    for idx, step in enumerate(after_steps):
        alias = f'jpost{idx}'
        segments.append((alias, _layer_body(step, cur)))
        cur = alias

    lines = ['WITH']
    n = len(segments)
    for i, (alias, body) in enumerate(segments):
        indented = '\n'.join('  ' + l if l.strip() else '' for l in body.split('\n'))
        lines.append(f'{alias} AS (')
        lines.append(indented)
        lines.append(')' if i == n - 1 else '),')
    lines.append(f'SELECT * FROM {cur}')
    return '\n'.join(lines)


def _cte_union(node: dict, parent_refs: list, parent_cols: list | None = None) -> str:
    """UNION branches almost never have identical columns in Tableau — and
    BigQuery's UNION aligns purely by ordinal *position*, not by name, so a
    blind `SELECT * ... UNION ALL SELECT * ...` either errors out on a
    column-count mismatch or, worse, silently pairs up columns that don't
    actually mean the same thing.

    When every branch's column set is known (tracked schema), this still
    builds one explicit, name-aligned column list shared by every branch,
    filling in `NULL AS col` for whichever columns a given branch doesn't
    have — that's real, useful work, not a guess. But name-matching can't
    catch everything (a data-type mismatch under the same column name, for
    instance), so every UNION is flagged for a manual check every time,
    regardless of whether the automatic alignment succeeded — honestly
    reflecting that this is the one node type worth a human's eyes on
    every single run, not just when the tool is unsure."""
    action = node.get('actionNode', node)
    kw = 'UNION DISTINCT' if action.get('unionType', '').lower() == 'distinct' else 'UNION ALL'
    name = node.get('name', '')
    if not parent_refs:
        return '-- TODO: no upstream nodes found'

    all_parent_cols = parent_cols or []
    known_cols = [c for c in all_parent_cols if c is not None]
    aligned = bool(all_parent_cols) and len(known_cols) == len(all_parent_cols)

    if aligned:
        all_cols = sorted(set().union(*known_cols))
        PARSE_WARNINGS.append((
            'UNION — test and verify every time',
            f'"{name}": columns auto-aligned by name across {len(parent_refs)} branch(es) — '
            'still double-check the column list is complete and that matching column names '
            'actually share the same data type before trusting this.',
        ))
        lines = [f'-- TODO: UNION "{name}" — auto-aligned by column name; '
                 'verify the column list and data types before relying on this']
        for i, pr in enumerate(parent_refs):
            if i > 0:
                lines.append(kw)
            branch_cols = known_cols[i]
            select_parts = [
                _col_id(col) if col in branch_cols else f'NULL AS {_col_id(col)}'
                for col in all_cols
            ]
            lines.append('SELECT')
            lines += _commas(['  ' + p for p in select_parts])
            lines.append(f'FROM {pr}')
        return '\n'.join(lines)

    PARSE_WARNINGS.append((
        'UNION — test and verify every time',
        f'"{name}": column set unknown for at least one branch; BigQuery UNION matches '
        'columns by position, not by name — a plain SELECT * from mismatched branches can '
        'silently pair up the wrong columns.',
    ))
    lines = [f'-- TODO: UNION "{name}" — could not verify all branches share the same '
             'columns in the same order; BigQuery aligns by position, not name — check this manually']
    for i, pr in enumerate(parent_refs):
        if i > 0:
            lines.append(kw)
        lines.append(f'SELECT * FROM {pr}')
    return '\n'.join(lines)


def _cte_pivot(node: dict, parent_ref: str) -> str:
    action = node.get('actionNode', node)

    pivot_col = action.get('pivotColumnName') or action.get('pivotColumn') or action.get('onColumn')
    value_col = action.get('aggregateColumnName') or action.get('valueColumn') or action.get('aggregateColumn')
    agg_func = (action.get('defaultAggregation') or action.get('aggregateFunction') or 'SUM').upper()

    new_cols = action.get('newPivotColumns', [])
    pivot_vals = [c.get('newColumnName') for c in new_cols if isinstance(c, dict) and c.get('newColumnName')]
    if not pivot_vals:
        pivot_vals = action.get('pivotValues', [])

    if not pivot_col or not value_col:
        PARSE_WARNINGS.append(('PIVOT node missing pivot/value column — verify manually', node.get('name', '')))
        return f'SELECT *  -- TODO: could not determine pivot/value column for "{node.get("name", "")}"\nFROM {parent_ref}'

    # pivot_vals are literal *data values* matched against the real column
    # (e.g. actual category names) — not identifiers, so they're left exactly
    # as Tableau recorded them rather than case/underscore-normalised.
    vals_str = ', '.join(f"'{v}'" for v in pivot_vals) if pivot_vals else '-- TODO: list pivot values'
    return '\n'.join([
        'SELECT *',
        f'FROM {parent_ref}',
        'PIVOT(',
        f'  {agg_func}({_col_id(value_col)})',
        f'  FOR {_col_id(pivot_col)} IN ({vals_str})',
        ')',
    ])


def _unpivot_group(node: dict) -> tuple:
    """Pull (names_col, source_cols, unpivot_cols) out of an UNPIVOT node,
    tolerating the couple of shapes Tableau Prep has used across versions."""
    action = node.get('actionNode', node)
    grp = action.get('unpivotGroup', action.get('unpivotGroups', {}))
    literal_col = grp.get('literalColumn', {})
    names_col = literal_col.get('literalColumnName', '')
    source_cols = literal_col.get('names') or literal_col.get('literals') or []
    unpivot_cols = grp.get('unpivotColumns', [])
    return names_col, source_cols, unpivot_cols


def _cte_unpivot(node: dict, parent_ref: str) -> str:
    names_col, source_cols, unpivot_cols = _unpivot_group(node)

    if not (names_col and source_cols and unpivot_cols):
        PARSE_WARNINGS.append(('UNPIVOT node missing names/value column info — verify manually', node.get('name', '')))

        def core(ref: str) -> str:
            return f'SELECT *  -- TODO: could not determine unpivot columns for "{node.get("name", "")}"\nFROM {ref}'
    else:
        def core(ref: str) -> str:
            if len(unpivot_cols) == 1:
                values_col = unpivot_cols[0].get('unpivotColumnName', 'unpivot_values')
                cols_list = ', '.join(_col_id(c) for c in source_cols)
                return '\n'.join([
                    'SELECT *',
                    f'FROM {ref}',
                    'UNPIVOT(',
                    f'  {_col_id(values_col)} FOR {_col_id(names_col)} IN ({cols_list})',
                    ')',
                ])
            # Multiple value columns unpivoted in lockstep from the same wide
            # columns -> BigQuery's tuple UNPIVOT form. src_name becomes a
            # literal *data value* in the resulting names column, not an
            # identifier, so it's left exactly as Tableau recorded it.
            value_names = [uc.get('unpivotColumnName', f'value_{i}') for i, uc in enumerate(unpivot_cols)]
            bindings = [uc.get('columnInformation', {}).get('manualBindings', []) for uc in unpivot_cols]
            rows = []
            for i, src_name in enumerate(source_cols):
                tup = ', '.join(_col_id(b[i]) for b in bindings if i < len(b))
                rows.append(f"({tup}) AS '{src_name}'")
            values_tuple = ', '.join(_col_id(v) for v in value_names)
            return '\n'.join([
                'SELECT *',
                f'FROM {ref}',
                'UNPIVOT(',
                f'  ({values_tuple}) FOR {_col_id(names_col)} IN (',
                '    ' + ',\n    '.join(rows),
                '  )',
                ')',
            ])

    return _wrap_with_annotations(
        parent_ref,
        node.get('beforeActionAnnotations', []),
        core,
        node.get('afterActionAnnotations', []),
    )


# ---------------------------------------------------------------------------
# Schema pre-pass — tracks the known column set through the whole DAG so
# CLEAN/JOIN generation can use real column names instead of guessing.
# Only produces a result where --schema (or an override) actually supplies
# one; every node falls back to None (untracked) otherwise, which reproduces
# today's behaviour exactly. No PARSE_WARNINGS side effects happen here —
# those only come from the real generation pass later, so nothing is
# double-counted in the manual-review report.
# ---------------------------------------------------------------------------

def compute_node_columns(nodes: dict, order: list, parents_map: dict) -> dict:
    node_columns = {}

    for nid in order:
        node = nodes[nid]
        cat, _raw = classify_node(node)
        raw_parents = parents_map.get(nid, [])
        parent_ids = [pid for pid, _ns in raw_parents]

        if cat == 'INPUT':
            attrs = node.get('connectionAttributes', {})
            src = (attrs.get('datasourceName') or attrs.get('tableName')
                   or attrs.get('dbname') or node.get('name', ''))
            key = sanitise(src)
            node_columns[nid] = set(SCHEMA[key]) if key in SCHEMA else None

        elif cat == 'CLEAN':
            parent_cols = node_columns.get(parent_ids[0]) if parent_ids else None
            node_columns[nid] = _clean_container_result_columns(node, parent_cols)

        elif cat == 'AGGREGATE':
            action = node.get('actionNode', node)
            group_fields = action.get('groupByFields', action.get('dimensions', []))
            agg_fields = action.get('aggregateFields', action.get('aggregateExpressions', action.get('measures', [])))
            cols = set()
            for gf in group_fields:
                cols.add(gf.get('columnName', gf) if isinstance(gf, dict) else str(gf))
            for af in agg_fields:
                if isinstance(af, dict):
                    col = af.get('columnName', af.get('name', 'unknown'))
                    cols.add(af.get('newColumnName') or col)
            after_steps = _annotation_steps(node.get('afterActionAnnotations', []))
            node_columns[nid] = _apply_steps_to_columns(cols, after_steps)

        elif cat == 'JOIN':
            by_ns = {ns: pid for pid, ns in raw_parents}
            left_pid = by_ns.get('Left', parent_ids[0] if parent_ids else None)
            right_pid = by_ns.get('Right', parent_ids[1] if len(parent_ids) > 1 else None)
            left_cols = node_columns.get(left_pid) if left_pid else None
            right_cols = node_columns.get(right_pid) if right_pid else None

            before = node.get('beforeActionAnnotations', [])
            left_cols = _apply_steps_to_columns(left_cols, _annotation_steps([a for a in before if a.get('namespace') == 'Left']))
            right_cols = _apply_steps_to_columns(right_cols, _annotation_steps([a for a in before if a.get('namespace') == 'Right']))

            if left_cols is not None and right_cols is not None:
                action = node.get('actionNode', node)
                conditions = action.get('joinExpressions', action.get('conditions', []))
                right_keys = set()
                for cond in conditions:
                    right_key_name = _bracket_field_name(cond.get('rightExpression', cond.get('rightKey', '')))
                    if right_key_name:
                        right_keys.add(right_key_name)
                merged = left_cols | (right_cols - right_keys)
                node_columns[nid] = _apply_steps_to_columns(merged, _annotation_steps(node.get('afterActionAnnotations', [])))
            else:
                node_columns[nid] = None

        elif cat == 'UNION':
            branch_cols = [node_columns.get(pid) for pid in parent_ids]
            known_branch_cols = [c for c in branch_cols if c is not None]
            if branch_cols and len(known_branch_cols) == len(branch_cols):
                # A null-filled UNION carries every column from every branch,
                # not just the first one — matching what _cte_union emits.
                node_columns[nid] = set().union(*known_branch_cols)
            else:
                node_columns[nid] = None

        elif cat == 'PIVOT':
            parent_cols = node_columns.get(parent_ids[0]) if parent_ids else None
            if parent_cols is not None:
                action = node.get('actionNode', node)
                pivot_col = action.get('pivotColumnName') or action.get('pivotColumn') or action.get('onColumn')
                value_col = action.get('aggregateColumnName') or action.get('valueColumn') or action.get('aggregateColumn')
                new_cols = action.get('newPivotColumns', [])
                pivot_vals = {c.get('newColumnName') for c in new_cols if isinstance(c, dict) and c.get('newColumnName')}
                node_columns[nid] = (parent_cols - {pivot_col, value_col}) | pivot_vals
            else:
                node_columns[nid] = None

        elif cat == 'UNPIVOT':
            parent_cols = node_columns.get(parent_ids[0]) if parent_ids else None
            names_col, source_cols, unpivot_cols = _unpivot_group(node)
            if parent_cols is not None and names_col and source_cols and unpivot_cols:
                value_names = {uc.get('unpivotColumnName') for uc in unpivot_cols if uc.get('unpivotColumnName')}
                result = (parent_cols - set(source_cols)) | {names_col} | value_names
                node_columns[nid] = _apply_steps_to_columns(result, _annotation_steps(node.get('afterActionAnnotations', [])))
            else:
                node_columns[nid] = None

        else:  # OUTPUT and anything unrecognised: passthrough of first parent
            node_columns[nid] = node_columns.get(parent_ids[0]) if parent_ids else None

    return node_columns


# ---------------------------------------------------------------------------
# Combined (end-to-end) SQL builder
# ---------------------------------------------------------------------------

def build_combined(nodes, order, parents_map, mode, dataset, flow_path):
    """Build one combined CTE SQL per OUTPUT node in the flow."""
    flow_name = Path(flow_path).stem
    node_columns = compute_node_columns(nodes, order, parents_map)

    output_nids = [nid for nid in order if classify_node(nodes[nid])[0] == 'OUTPUT']
    if not output_nids:
        # Fallback: treat sink nodes (no outgoing edges) as implicit output endpoints
        has_children = set()
        for nid in nodes:
            for nxt in nodes[nid].get('nextNodes', []):
                target = nxt.get('nextNodeId')
                if target:
                    has_children.add(target)
        sink_nids = [nid for nid in order if nid not in has_children]
        output_nids = sink_nids if sink_nids else [order[-1]]

    results = []  # [(filename, sql)]

    for out_nid in output_nids:
        out_node = nodes[out_nid]
        out_name = sanitise(out_node.get('name', 'output'))

        # Collect all ancestor node IDs in topological order
        ancestors_set = set()
        stack = [pid for pid, _ns in parents_map.get(out_nid, [])]
        while stack:
            nid = stack.pop()
            if nid in ancestors_set:
                continue
            ancestors_set.add(nid)
            stack.extend(pid for pid, _ns in parents_map.get(nid, []))

        ancestors_ordered = [nid for nid in order if nid in ancestors_set]

        input_nids = {nid for nid in ancestors_ordered if classify_node(nodes[nid])[0] == 'INPUT'}
        cte_nids = [nid for nid in ancestors_ordered if nid not in input_nids]

        # Pre-assign unique CTE names (deduplicate by appending _2, _3 …)
        _name_counts: dict = {}
        nid_to_cte_name: dict = {}
        for nid in cte_nids:
            base = sanitise(nodes[nid].get('name', nid))
            _name_counts[base] = _name_counts.get(base, 0) + 1
            suffix = f'_{_name_counts[base]}' if _name_counts[base] > 1 else ''
            nid_to_cte_name[nid] = base + suffix

        # Build a helper that returns the right reference for a parent node:
        # - INPUT nodes  → ${ref()} in dataform, bare table name in bigquery
        # - CTE nodes    → plain CTE name (deduplicated) — no ref() wrapper needed
        def node_ref(parent_nid: str) -> str:
            pnode = nodes.get(parent_nid, {})
            if parent_nid in input_nids:
                pname = sanitise(pnode.get('name', parent_nid))
                return ref(pname, mode)
            return nid_to_cte_name.get(parent_nid, sanitise(pnode.get('name', parent_nid)))

        # Build the file header
        lines = [f'-- Flow: {flow_name}']
        for inp_nid in [nid for nid in order if nid in input_nids]:
            inp_node = nodes[inp_nid]
            attrs = inp_node.get('connectionAttributes', {})
            src = (attrs.get('datasourceName') or attrs.get('tableName')
                   or attrs.get('dbname') or inp_node.get('name', ''))
            lines.append(f'-- Source: {src}')
        lines.append(f"-- Output: {out_node.get('name', out_name)}")
        lines.append('')

        # Dataform config block / BigQuery DDL header
        if mode == 'dataform':
            lines += [
                'config {',
                '  type: "table",',
                f'  schema: "{dataset}",',
                f'  name: "{out_name}",',
                '  description: "Final output from Tableau Prep flow"',
                '}',
                '',
            ]
        else:
            lines += [
                f'CREATE OR REPLACE TABLE `{dataset}`.`{out_name}` AS',
                '',
            ]

        # Build CTEs
        if cte_nids:
            lines.append('WITH')
            lines.append('')
            for i, nid in enumerate(cte_nids):
                node = nodes[nid]
                cat, raw_type = classify_node(node)
                cte_name = nid_to_cte_name[nid]
                orig_name = node.get('name', nid)

                raw_parents = parents_map.get(nid, [])
                pid_list = [pid for pid, _ns in raw_parents]
                parent_refs = [node_ref(pid) for pid in pid_list if pid in nodes]
                parent_ref = parent_refs[0] if parent_refs else 'UNKNOWN_PARENT'

                if cat == 'CLEAN':
                    known_cols = node_columns.get(pid_list[0]) if pid_list else None
                    body = _cte_clean(node, parent_ref, known_cols)
                elif cat == 'AGGREGATE':
                    body = _cte_aggregate(node, parent_ref)
                elif cat == 'JOIN':
                    by_ns = {ns: pid for pid, ns in raw_parents}
                    left_pid = by_ns.get('Left')
                    right_pid = by_ns.get('Right')
                    left = node_ref(left_pid) if left_pid else (parent_refs[0] if len(parent_refs) > 0 else 'LEFT_PARENT')
                    right = node_ref(right_pid) if right_pid else (parent_refs[1] if len(parent_refs) > 1 else 'RIGHT_PARENT')
                    left_cols = node_columns.get(left_pid) if left_pid else None
                    right_cols = node_columns.get(right_pid) if right_pid else None
                    body = _cte_join(node, left, right, left_cols, right_cols)
                elif cat == 'UNION':
                    branch_cols = [node_columns.get(pid) for pid in pid_list]
                    body = _cte_union(node, parent_refs, branch_cols)
                elif cat == 'PIVOT':
                    body = _cte_pivot(node, parent_ref)
                elif cat == 'UNPIVOT':
                    body = _cte_unpivot(node, parent_ref)
                else:
                    body = f'-- TODO: unrecognised node type "{raw_type}"'

                is_last = (i == len(cte_nids) - 1)
                indented = '\n'.join('  ' + l if l.strip() else '' for l in body.split('\n'))

                lines.append(f'-- [{cat or "UNKNOWN"}] {orig_name}')
                lines.append(f'{cte_name} AS (')
                lines.append(indented)
                lines.append(')' if is_last else '),')
                lines.append('')

        # Final SELECT (output node)
        out_parent_ids = [pid for pid, _ns in parents_map.get(out_nid, [])]
        out_parent_names = [sanitise(nodes[pid].get('name', pid))
                            for pid in out_parent_ids if pid in nodes]
        final_from = out_parent_names[0] if out_parent_names else 'UNKNOWN'

        lines += ['SELECT *', f'FROM {final_from}']

        ext = '.sqlx' if mode == 'dataform' else '.sql'
        results.append((f'{out_name}{ext}', '\n'.join(lines)))

    return results


# ---------------------------------------------------------------------------
# Per-node split generators  (used with --split flag)
# parents: list of (sanitised_name, edge_namespace) tuples
# ---------------------------------------------------------------------------

def _split_input(node, parents, mode, dataset, known_cols=None):
    attrs = node.get('connectionAttributes', {})
    table = (attrs.get('datasourceName') or attrs.get('tableName')
             or attrs.get('dbname') or node.get('name', 'unknown_source'))
    name = sanitise(node.get('name', table))
    if mode == 'dataform':
        schema = sanitise(attrs.get('dbname', 'raw_dataset'))
        return (f'config {{\n  type: "declaration",\n  schema: "{schema}",\n  name: "{name}"\n}}')
    return f'-- Source: {table}\n-- No SQL needed; reference this table directly in downstream steps.'


def _split_clean(node, parents, mode, dataset, known_cols=None):
    parent_ref = ref(parents[0][0], mode) if parents else 'UNKNOWN_PARENT'
    body = _cte_clean(node, parent_ref, known_cols)
    if mode == 'dataform':
        return f'config {{ type: "view", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_aggregate(node, parents, mode, dataset, known_cols=None):
    parent_ref = ref(parents[0][0], mode) if parents else 'UNKNOWN_PARENT'
    body = _cte_aggregate(node, parent_ref)
    if mode == 'dataform':
        return f'config {{ type: "table", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_join(node, parents, mode, dataset, known_cols=None):
    by_ns = {ns: name for name, ns in parents}
    names = [name for name, _ns in parents]
    left_name = by_ns.get('Left', names[0] if names else 'LEFT_PARENT')
    right_name = by_ns.get('Right', names[1] if len(names) > 1 else 'RIGHT_PARENT')
    left_cols, right_cols = known_cols if known_cols else (None, None)
    body = _cte_join(node, ref(left_name, mode), ref(right_name, mode), left_cols, right_cols)
    if mode == 'dataform':
        return f'config {{ type: "view", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_union(node, parents, mode, dataset, known_cols=None):
    parent_refs = [ref(name, mode) for name, _ns in parents]
    body = _cte_union(node, parent_refs, known_cols)
    if mode == 'dataform':
        return f'config {{ type: "view", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_pivot(node, parents, mode, dataset, known_cols=None):
    parent_ref = ref(parents[0][0], mode) if parents else 'UNKNOWN_PARENT'
    body = _cte_pivot(node, parent_ref)
    if mode == 'dataform':
        return f'config {{ type: "view", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_unpivot(node, parents, mode, dataset, known_cols=None):
    parent_ref = ref(parents[0][0], mode) if parents else 'UNKNOWN_PARENT'
    body = _cte_unpivot(node, parent_ref)
    if mode == 'dataform':
        return f'config {{ type: "view", schema: "{dataset}" }}\n\n{body}'
    return body


def _split_output(node, parents, mode, dataset, known_cols=None):
    parent_ref = ref(parents[0][0], mode) if parents else 'UNKNOWN_PARENT'
    name = sanitise(node.get('name', 'output'))
    if mode == 'dataform':
        return '\n'.join([
            'config {', '  type: "table",',
            f'  schema: "{dataset}",', f'  name: "{name}",',
            '  description: "Final output from Tableau Prep flow"', '}',
            '', 'SELECT *', f'FROM {parent_ref}',
        ])
    return '\n'.join([f'CREATE OR REPLACE TABLE `{dataset}`.`{name}` AS',
                      'SELECT *', f'FROM {parent_ref}'])


SPLIT_GENERATORS = {
    'INPUT':     _split_input,
    'CLEAN':     _split_clean,
    'JOIN':      _split_join,
    'UNION':     _split_union,
    'AGGREGATE': _split_aggregate,
    'PIVOT':     _split_pivot,
    'UNPIVOT':   _split_unpivot,
    'OUTPUT':    _split_output,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Kinds of PARSE_WARNINGS that represent one Tableau calc expression not
# translating cleanly (as opposed to a structural/step-level issue like an
# unhandled node type) — the numerator for expression_accuracy_pct below.
_EXPRESSION_LEVEL_PREFIXES = ('could not parse', 'parameter reference', 'LOD expression')


def build_review_report(node_warnings: list) -> dict:
    """Pure summary of everything flagged during this run — used both for the
    printed report and for --warnings-json (so CI/dashboards can gate on
    counts instead of scraping stdout)."""
    by_kind = collections.Counter(kind for kind, _detail in PARSE_WARNINGS)
    expr_flags = sum(1 for kind, _detail in PARSE_WARNINGS if kind.startswith(_EXPRESSION_LEVEL_PREFIXES))
    attempts = max(TRANSLATE_ATTEMPTS, 1)
    return {
        'unrecognised_nodes': list(node_warnings),
        'flagged_items': [{'kind': kind, 'detail': detail} for kind, detail in PARSE_WARNINGS],
        'summary': {
            'unrecognised_node_count': len(node_warnings),
            'flagged_item_count': len(PARSE_WARNINGS),
            'flagged_by_kind': dict(by_kind),
            'expression_translate_attempts': TRANSLATE_ATTEMPTS,
            'expression_level_flags': expr_flags,
            'expression_accuracy_pct': round(100.0 * (attempts - expr_flags) / attempts, 1),
        },
    }


def _print_review_report(report: dict) -> None:
    node_warnings = report['unrecognised_nodes']
    summary = report['summary']
    print(
        f"\nExpression-level accuracy: {summary['expression_accuracy_pct']}% "
        f"({summary['expression_translate_attempts'] - summary['expression_level_flags']}/"
        f"{summary['expression_translate_attempts']} calc expressions translated with no flag)"
    )

    if not node_warnings and not report['flagged_items']:
        print('No manual-review items flagged — still spot-check the generated SQL before running it.')
        return

    print('\n--- Needs manual review ---')
    if node_warnings:
        print(f'{len(node_warnings)} unrecognised node type(s):')
        for w in node_warnings:
            print(f'  - {w}')
    if report['flagged_items']:
        print(f'{summary["flagged_item_count"]} item(s) could not be mechanically translated:')
        for kind, count in summary['flagged_by_kind'].items():
            print(f'  - {kind}: {count}')
        print('(search generated files for "TODO" to find exact locations)')


def main():
    parser = argparse.ArgumentParser(
        description='Convert a Tableau Prep .tfl flow to SQL/SQLX.\n'
                    'Default: one combined CTE file per output showing the full pipeline.\n'
                    'LOD INCLUDE/EXCLUDE, custom SQL steps, and unrecognised node types are\n'
                    'flagged as TODOs in the output and summarised at the end of the run —\n'
                    'they are not silently guessed.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('flow', help='Path to the .tfl file')
    parser.add_argument('--mode', choices=['dataform', 'bigquery'], default='dataform',
                        help='Output format (default: dataform)')
    parser.add_argument('--out', default='./output_sql',
                        help='Output folder (default: ./output_sql)')
    parser.add_argument('--dataset', default='my_dataset',
                        help='BigQuery dataset name (default: my_dataset)')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing output files')
    parser.add_argument('--split', action='store_true',
                        help='Write one file per node instead of a single combined file')
    parser.add_argument('--schema', metavar='PATH',
                        help='JSON {table: {"columns": [...]}} cache (see tools/fetch_schema.py) — '
                             'unlocks real BulkRenameColumns application, full JOIN column dedup, '
                             'and missing-column detection for whichever branches it covers')
    parser.add_argument('--overrides', metavar='PATH',
                        help='JSON file of hand-authored fixes (expressions/parameters/bulk_renames) '
                             'for items the parser or schema can\'t resolve — see README for the format')
    parser.add_argument('--warnings-json', metavar='PATH',
                        help='Also write the manual-review report as machine-readable JSON')
    args = parser.parse_args()

    if not os.path.exists(args.flow):
        raise SystemExit(f"ERROR: File not found: {args.flow}")

    if args.overrides:
        if not os.path.exists(args.overrides):
            raise SystemExit(f"ERROR: Overrides file not found: {args.overrides}")
        load_overrides(args.overrides)

    if args.schema:
        if not os.path.exists(args.schema):
            raise SystemExit(f"ERROR: Schema file not found: {args.schema}")
        load_schema(args.schema)

    flow = load_tfl(args.flow)
    nodes = extract_nodes(flow)
    if not nodes:
        raise SystemExit("ERROR: No nodes found in the flow file.")

    edges = extract_edges(nodes)
    order = topological_sort(nodes, edges)

    parents_map: dict = collections.defaultdict(list)
    for src, dst, ns in edges:
        parents_map[dst].append((src, ns))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = '.sqlx' if args.mode == 'dataform' else '.sql'
    warnings = []

    if args.split:
        # ---- Per-node files ----
        node_columns = compute_node_columns(nodes, order, parents_map)
        summary = []
        for idx, nid in enumerate(order, start=1):
            node = nodes[nid]
            category, raw_type = classify_node(node)
            node_name = node.get('name', nid)
            filename = f'{idx:02d}_{sanitise(node_name)}{ext}'
            out_path = out_dir / filename

            if out_path.exists() and not args.overwrite:
                print(f'WARNING: {filename} already exists, skipping (use --overwrite to replace)')
                continue

            raw_pairs = parents_map.get(nid, [])
            parent_pairs = [(sanitise(nodes[pid].get('name', pid)), ns)
                            for pid, ns in raw_pairs if pid in nodes]

            if category == 'JOIN':
                by_ns = {ns: pid for pid, ns in raw_pairs}
                node_known_cols = (node_columns.get(by_ns.get('Left')), node_columns.get(by_ns.get('Right')))
            elif category == 'CLEAN':
                node_known_cols = node_columns.get(raw_pairs[0][0]) if raw_pairs else None
            elif category == 'UNION':
                node_known_cols = [node_columns.get(pid) for pid, _ns in raw_pairs]
            else:
                node_known_cols = None

            if category is None:
                warnings.append(
                    f'Node "{node_name}" (id: {nid}) has unrecognised type "{raw_type}" — skipped.')
                sql = f'-- TODO: unrecognised node type "{raw_type}"\n-- Node: {node_name}'
            else:
                sql = SPLIT_GENERATORS[category](node, parent_pairs, args.mode, args.dataset, node_known_cols)

            out_path.write_text(sql + '\n', encoding='utf-8')
            summary.append((filename, category or 'UNKNOWN'))

        print(f'\nGenerated {len(summary)} files in {args.out}/\n')
        col_w = max((len(f) for f, _ in summary), default=10) + 2
        for fname, cat in summary:
            print(f'  {fname:<{col_w}} {cat}')

    else:
        # ---- Combined end-to-end file per output ----
        results = build_combined(nodes, order, parents_map, args.mode, args.dataset, args.flow)
        summary = []
        for filename, sql in results:
            out_path = out_dir / filename
            if out_path.exists() and not args.overwrite:
                print(f'WARNING: {filename} already exists, skipping (use --overwrite to replace)')
                continue
            out_path.write_text(sql + '\n', encoding='utf-8')
            summary.append(filename)

        print(f'\nGenerated {len(summary)} combined file(s) in {args.out}/\n')
        for fname in summary:
            print(f'  {fname}')

    if warnings:
        print('\nWarnings:')
        for w in warnings:
            print(f'  {w}')

    report = build_review_report(warnings)
    _print_review_report(report)

    if args.warnings_json:
        Path(args.warnings_json).write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'\nMachine-readable report written to {args.warnings_json}')


if __name__ == '__main__':
    main()
