#!/usr/bin/env python3
"""Extract financial HTML tables in ONE Item of two complete 10-K filings to JSON.

Python 3.10+; dependency: lxml. No LLM, API key, browser, pandas, or PDF reader.
Default: table -> year/date -> section -> optional subsection -> value.
Use --output-format csv or both to run the existing annotation comparison too.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from table_titles import normalize_table_title

try:
    from lxml import etree, html
except ImportError:
    raise SystemExit('Install the dependency first: python -m pip install lxml')

VERSION = '1.5.0'
# Edit this list, or pass --columns columns.json, to reorder/remove/rename exports.
ANNOTATION_COLUMNS = [
    'Company', 'Industry', 'Split', 'Filing Form', 'Previous Fiscal Year',
    'Current Fiscal Year', 'Item', 'Change ID', 'Table Pair ID',
    'Previous Table ID', 'Current Table ID', 'Previous Table Title',
    'Current Table Title', 'Change Level', 'Change Detail',
    'Previous Row Label', 'Current Row Label', 'Previous Column Header',
    'Current Column Header', 'Previous Data Period', 'Current Data Period',
    'Previous Unit', 'Current Unit', 'Previous Evidence', 'Current Evidence',
    'Match Status', 'Change Taxonomy', 'Content Taxonomy', 'Materiality',
    'Rationale / Evidence', 'Previous Source URL', 'Current Source URL',
    'Review Notes',
]
BLOCK_TAGS = {'div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
ITEM_RE = re.compile(r'^ITEM\s+(\d{1,2}[A-C]?)\s*[.:\-–—]?\s*(.*)$', re.I)
YEAR_RE = re.compile(r'\b(?:19|20)\d{2}\b')
NUMBER_RE = re.compile(r'^[+\-−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)$')
DASHES = {'—', '–', '-', '−'}
CURRENCY = {'$': 'USD', '€': 'EUR', '£': 'GBP', '¥': 'JPY'}


def clean(value: str) -> str:
    """Normalize extraction whitespace only; retain case, punctuation and symbols."""
    return re.sub(r'\s+', ' ', value.replace('\u200b', '').replace('\u00ad', '')).strip()


def text(node) -> str:
    # Preserve inline text joins, but separate explicit HTML line breaks.
    parts = []
    for event, token in etree.iterwalk(node, events=('start', 'end')):
        if event == 'start':
            if str(token.tag).lower() == 'br':
                parts.append(' ')
            if token.text:
                parts.append(token.text)
        elif token is not node and token.tail:
            parts.append(token.tail)
    return clean(''.join(parts))


def norm(value: str) -> str:
    return clean(value).casefold()


def slug(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', value.lower()).strip('_') or 'company'


def numeric(raw: str) -> tuple[str, str]:
    """Return (kind, exact decimal string). A dash and an empty value stay distinct."""
    s = clean(raw)
    for symbol in CURRENCY:
        s = s.replace(symbol, '')
    s = s.replace('%', '').strip()
    if not s:
        return 'blank', ''
    if s in DASHES:
        return 'dash', ''
    negative = s.startswith('(') and s.endswith(')')
    if negative:
        s = s[1:-1].strip()
    s = s.replace(' ', '')
    if not NUMBER_RE.fullmatch(s):
        return 'text', ''
    try:
        number = Decimal(s.replace(',', '').replace('−', '-'))
        if negative:
            number = -number
        return 'number', format(number, 'f')
    except InvalidOperation:
        return 'text', ''


@dataclass
class Origin:
    row: int
    start: int
    end: int
    raw: str
    node: Any = field(repr=False)


@dataclass
class Column:
    key: str
    header: str
    period: str
    measure: str
    physical_end: int
    group: str


@dataclass
class Row:
    row_id: str
    key: str
    label: str
    group: str
    physical_row: int
    synthetic_label: bool = False
    inferred_label: str = ''
    label_inference: str = ''


@dataclass
class Cell:
    row_id: str
    column_key: str
    raw: str
    kind: str
    number: str
    unit: str
    locator: str
    raw_fragments: list[str] = field(default_factory=list)


@dataclass
class Table:
    table_id: str
    title: str
    page: str
    locator: str
    source: str
    year: int
    item: str
    index: int
    columns: list[Column]
    rows: list[Row]
    cells: list[Cell]
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unit_evidence: str = ''
    raw_grid: list[list[str]] = field(default_factory=list)
    header_context: str = ''
    page_chunk_index: int | None = None
    source_section: str = ''
    introductory_sentence: str = ''
    title_basis: str = ''


def expand_grid(table) -> tuple[list[list[Origin | None]], list[list[Origin]]]:
    """Expand colspan/rowspan once; references retain their original cell identity."""
    trs = [r for r in table.iter('tr') if next(r.iterancestors('table'), None) is table]
    grid: list[list[Origin | None]] = [[] for _ in trs]
    origins = [[] for _ in trs]
    for ri, tr in enumerate(trs):
        ci = 0
        for node in tr:
            if node.tag not in {'td', 'th'}:
                continue
            while ci < len(grid[ri]) and grid[ri][ci] is not None:
                ci += 1
            try:
                width = max(1, int(node.get('colspan', '1')))
                height = max(1, int(node.get('rowspan', '1')))
            except ValueError:
                raise ValueError('Invalid HTML rowspan/colspan')
            if width > 500 or height > 2000:
                raise ValueError('Unreasonable HTML span; review this table')
            cell = Origin(ri, ci, ci + width, text(node), node)
            origins[ri].append(cell)
            for rr in range(ri, min(len(trs), ri + height)):
                grid[rr].extend([None] * max(0, ci + width - len(grid[rr])))
                for cc in range(ci, ci + width):
                    if grid[rr][cc] is not None:
                        raise ValueError('Overlapping HTML spans; review this table')
                    grid[rr][cc] = cell
            ci += width
    width = max((len(r) for r in grid), default=0)
    for r in grid:
        r.extend([None] * (width - len(r)))
    return grid, origins


def dedupe_origins(cells) -> list[Origin]:
    seen, result = set(), []
    for c in cells:
        if c is not None and id(c) not in seen:
            seen.add(id(c))
            result.append(c)
    return result


def separate_inline_prose(root):
    """Expose prose sharing a div with a table without including table text.

    Move existing nodes, preserving their identity and previously captured
    source XPaths. Separate runs on either side of each table/block boundary so
    text after a table cannot be used as its introductory heading.
    """
    for block in reversed(list(root.iter())):
        if block.tag not in BLOCK_TAGS or any(a.tag == 'table' for a in block.iterancestors()):
            continue
        children = list(block)
        boundary = lambda child: child.tag in BLOCK_TAGS or child.tag == 'table'
        if not any(boundary(child) for child in children):
            continue
        if not ((block.text or '').strip() or any(
                not boundary(child) or (child.tail or '').strip() for child in children)):
            continue
        pending = html.Element('div')
        pending.text, block.text = block.text, None
        for child in children:
            block.remove(child)
        for child in children:
            if boundary(child):
                if text(pending):
                    block.append(pending)
                block.append(child)
                pending = html.Element('div')
                pending.text, child.tail = child.tail, None
            else:
                pending.append(child)
        if text(pending):
            block.append(pending)


def primitive_blocks(root) -> list[Any]:
    # Bottom-up marking avoids repeatedly flattening the entire document.
    has_block, has_table = {}, {}
    result = []
    for e in reversed(list(root.iter())):
        below = any(c.tag in BLOCK_TAGS or has_block.get(c, False) for c in e)
        has_block[e] = below
        has_table[e] = any(c.tag == 'table' or has_table.get(c, False) for c in e)
        if e.tag in BLOCK_TAGS and not below and not has_table[e] and not any(a.tag == 'table' for a in e.iterancestors()):
            result.append(e)
    return list(reversed(result))


def bold_block(node) -> bool:
    return node.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} or any(
        e.tag in {'b', 'strong'} or re.search(r'font-weight\s*:\s*(?:bold|[6-9]00)', e.get('style', ''), re.I)
        for e in node.iter()
    )


def table_heading(value, block):
    """Require a standalone heading, not a paragraph with one bold phrase."""
    if not value or len(value) > 160 or ITEM_RE.match(value):
        return False
    return block.tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'} or any(
        (e.tag in {'b', 'strong'} or re.search(
            r'font-weight\s*:\s*(?:bold|[6-9]00)', e.get('style', ''), re.I))
        and text(e) == value for e in block.iter())


def title_furniture(value):
    return (not value or value.casefold() == 'table of contents'
            or re.fullmatch(r'\d+(?:\s*\|.*)?', value)
            or re.match(r'^(?:\*|\(\d+\)|Note:)', value, re.I)
            or re.fullmatch(r'\(?(?:All(?: tabular)?(?: dollar)? amounts(?: are)? )?In '
                            r'(?:millions|billions|thousands|percentages)[^)]*\)?', value, re.I))


def last_intro_sentence(value):
    value = clean(value)
    start = 0
    abbreviation = re.compile(
        r'\b(?:(?:[A-Za-z]\.){2,}|(?:Co|Corp|Inc|Ltd|No|Mr|Mrs|Ms|Dr|Jr|Sr|'
        r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.)$', re.I)
    for boundary in re.finditer(r'''[.!?]["”’')\]]*\s+(?=["“‘'(\[]?[A-Z0-9])''', value):
        if not abbreviation.search(value[:boundary.start() + 1]):
            start = boundary.end()
    return value[start:]


def inline_table_label(value, block):
    """Return a formatted lead-in and separator, including an external colon."""
    for element in block.iterdescendants():
        styled = element.tag in {'i', 'em', 'b', 'strong'} or re.search(
            r'(?:font-style\s*:\s*italic|font-weight\s*:\s*(?:bold|[6-9]00))',
            element.get('style', ''), re.I)
        label = text(element)
        if styled and label and len(label) <= 80 and value.startswith(label):
            remainder = value[len(label):].strip()
            if remainder:
                if label.endswith(('–', '—', '-', ':')):
                    return label[:-1].strip(), label[-1]
                if re.match(r'^[–—\-:]', remainder):
                    return label.strip(), remainder[0]
    return '', ''


def explicit_table_heading(value, block):
    if title_furniture(value):
        return ''
    if table_heading(value, block):
        return value.rstrip(':')
    label, separator = inline_table_label(value, block)
    return label if separator == ':' else ''


def choose_table_title(node, previous, heading, previous_table_end):
    caption = node.find('caption')
    if caption is not None and text(caption):
        return text(caption)
    intro = ''
    for position, value, block in reversed(previous):
        if position <= previous_table_end:
            break
        if title_furniture(value) or ITEM_RE.match(value):
            continue
        if table_heading(value, block):
            break
        intro = last_intro_sentence(value)
        # An explicit inline label can introduce a new table within a broader
        # section. Ordinary sentences never override an existing section title.
        if (not heading or position > heading[0]) and inline_table_label(value, block)[0]:
            return intro
        break
    return heading[1].rstrip(':') if heading else intro


def preceding_table_sentence(previous, heading, previous_table_end):
    """Read only prose between this section/previous table and the current table."""
    for position, value, block in reversed(previous):
        if position <= previous_table_end or (heading and position < heading[0]):
            break
        if title_furniture(value) or ITEM_RE.match(value):
            continue
        if table_heading(value, block):
            break
        return last_intro_sentence(value)
    return ''


def name_section_tables(tables, contexts, section_counts):
    """Split multi-table sections; preserve the section title for single tables."""
    ordinals = Counter()
    for table in tables:
        section_key, caption = contexts[table.table_id]
        ordinals[section_key] += 1
        if section_counts[section_key] > 1:
            if table.introductory_sentence:
                table.title, table.title_basis = table.introductory_sentence, 'preceding_sentence'
            elif caption:
                table.title, table.title_basis = caption, 'caption'
            else:
                table.title = (f'{table.source_section} [Table {ordinals[section_key]}]'
                               if table.source_section else f'Untitled table {table.index}')
                table.title_basis = 'numbered_fallback'
        elif table.source_section:
            table.title, table.title_basis = caption or table.source_section, 'caption' if caption else 'section_heading'
        else:
            table.title_basis = 'caption' if caption else 'preceding_sentence' if table.introductory_sentence else 'fallback'
    # Identical introductory sentences still represent separate physical tables.
    counts = Counter(norm(t.title) for t in tables)
    reserved = {norm(t.title) for t in tables}
    used, repeats = set(), Counter()
    for table in tables:
        base, key = table.title, norm(table.title)
        if counts[key] > 1 or key in used:
            repeats[key] += 1
            candidate = f'{base} [Table {repeats[key]}]'
            while norm(candidate) in used or norm(candidate) in reserved:
                repeats[key] += 1
                candidate = f'{base} [Table {repeats[key]}]'
            table.title = candidate
        used.add(norm(table.title))


def find_item(blocks, order, item: str) -> tuple[int, int, list[dict]]:
    headings = []
    for b in blocks:
        s = text(b)
        m = ITEM_RE.match(s)
        if not m or len(s) > 260:
            continue
        # Contents links are not body headings. Prose "Item 8. ..." references
        # are not boundaries unless formatted as headings.
        if b.xpath('.//a[starts-with(@href,"#")]'):
            continue
        title = m.group(2)
        if not (bold_block(b) or (title and title.isupper())):
            continue
        if title.lower().startswith(('see ', 'refer ')):
            continue
        headings.append({'item': m.group(1).upper(), 'position': order[b], 'text': s})
    starts = [h for h in headings if h['item'] == item]
    if len(starts) != 1:
        raise ValueError(f'Expected one body Item {item} heading; found {len(starts)}. '
                         'Use the full filing HTML, not the SEC viewer/index page. '
                         'For unusual headings, use --previous-item-xpath/--current-item-xpath.')
    start = starts[0]['position']
    end = next((h['position'] for h in headings if h['position'] > start), 10**12)
    return start, end, headings


def detect_unit(context: str) -> tuple[str, str]:
    """Only infer a scale when the source explicitly states it."""
    matches = list(re.finditer(r'(?:all tabular dollar amounts are in|\bin)\s+(millions|thousands|billions)\b', context, re.I))
    if not matches:
        return '', ''
    m = matches[-1]
    return m.group(1).lower(), clean(context[max(0, m.start()-25):m.end()+60])


def infer_footer_total(rows, cells, columns, grid):
    """Name an unlabelled final row only when source layout AND sums support it.

    Keep the raw row label and matching key unchanged. A blank label alone is
    insufficient: require double-bottom rules (or tfoot), at least two reporting
    periods, exact sums in every amount column, and no displayed footer rates.
    Dashes/blank amounts are not assumed to mean zero during this inference.
    """
    if len(rows) < 3 or rows[-1].label:
        return
    footer, details = rows[-1], rows[:-1]
    if any(not row.label or row.group != footer.group for row in details):
        return  # ambiguous subtotals/group scopes remain explicitly unlabelled
    lookup = {(cell.row_id, cell.column_key): cell for cell in cells}
    amounts = [col for col in columns if col.measure != 'Percentage']
    if len({col.period for col in amounts if col.period}) < 2:
        return

    def total_rule(node):
        for part in [node] + list(node.iterancestors()):
            if part.tag == 'table':
                break
            if part.tag == 'tfoot':
                return True
            if re.search(r'border(?:-bottom|-bottom-style)?\s*:[^;]*\bdouble\b',
                         part.get('style', ''), re.I):
                return True
        return False

    checked = []
    for col in columns:
        total = lookup[(footer.row_id, col.key)]
        if col.measure == 'Percentage':
            if total.kind != 'blank':
                return
            continue
        components = [lookup[(row.row_id, col.key)] for row in details]
        if total.kind != 'number' or total.unit == '%' or any(
                c.kind != 'number' or c.unit != total.unit for c in components):
            return
        origin = grid[footer.physical_row - 1][col.physical_end - 1]
        if origin is None or not total_rule(origin.node):
            return
        if sum((Decimal(c.number) for c in components), Decimal(0)) != Decimal(total.number):
            return
        checked.append(col.period or col.header)
    footer.inferred_label = 'Total'
    footer.label_inference = (
        f'Unlabelled final row with double-bottom rules or tfoot; each of {len(checked)} '
        f'amount columns equals the exact sum of the {len(details)} preceding labelled rows '
        f'in the same group ({", ".join(checked)}). Source label remains blank.'
    )


def parse_table(node, title: str, scale: str, unit_evidence: str, source_paths=None) -> tuple[list, list, list, list, str]:
    source_paths = source_paths or {}
    grid, origins = expand_grid(node)
    if not grid:
        raise ValueError('Empty table')
    year_row = None
    shared_period = ''
    for ri, row in enumerate(origins[:12]):
        ys = [c for c in row if YEAR_RE.search(c.raw) and len(c.raw) < 80]
        # A year heading contains just years/dates, not ordinary numeric values.
        nums = [c for c in row if numeric(c.raw)[0] in {'number', 'dash'}]
        if ys and all(
            numeric(c.raw)[0] != 'number' or YEAR_RE.fullmatch(c.raw) for c in nums
        ):
            year_row = ri
            break
    if year_row is not None:
        year_cells = [c for c in origins[year_row] if YEAR_RE.search(c.raw) and len(c.raw) < 80]
        stub_end = min(c.start for c in year_cells)
        # Segment columns share a year printed in the stub header, e.g.
        # "For the year ended 2025 | CMBU | CDBU | ... | Total".
        stub_header = year_cells[0]
        if (len(year_cells) == 1 and stub_header.start == 0
                and re.match(r'^(?:for (?:the )?year ended|year ended|as of)\b', stub_header.raw, re.I)
                and len(YEAR_RE.findall(stub_header.raw)) == 1
                and any(c.start >= stub_header.end and c.raw
                        and numeric(c.raw)[0] == 'text' for c in origins[year_row])):
            stub_end = stub_header.end
            shared_period = YEAR_RE.search(stub_header.raw).group()
        scan_start = year_row + 1
    else:
        # Generic numerical tables with text headers are supported too.
        first = next((ri for ri, rr in enumerate(origins) if len(rr) > 1
                      and any(numeric(c.raw)[0] in {'number', 'dash'} for c in rr[1:])
                      and numeric(rr[0].raw)[0] == 'text'), None)
        if first is None or first == 0:
            raise ValueError('No usable year or column headers')
        stub_end = origins[first][0].end
        scan_start = first

    data = []
    group = ''
    for ri in range(scan_start, len(grid)):
        row = dedupe_origins(grid[ri])
        values = [c for c in row if c.start >= stub_end and numeric(c.raw)[0] in {'number', 'dash'}]
        labels = [c.raw for c in row if c.start < stub_end and c.raw]
        label = clean(' '.join(labels))
        text_values = [c for c in row if c.start >= stub_end and c.raw and c.raw not in {'$', '%', '€', '£', '¥'}]
        if values or (data and text_values):
            if label and re.match(r'^(?:for the|as of|year ended)', label, re.I) and all(YEAR_RE.fullmatch(c.raw) for c in values):
                continue  # repeated printed header
            data.append((ri, label, group, values))
        elif label and not re.match(r'^(?:for the|as of|\(?in millions|\(?in thousands)', label, re.I):
            # A full-width/standalone label inside the body defines a row group.
            if not any(c.raw and c.start >= stub_end for c in row):
                group = label
    if not data:
        raise ValueError('No numerical body rows')
    header_end = data[0][0]

    # SEC accounting markup often puts $ / number / % in separate cells.
    # Right edges align values despite different colspans for currency prefixes.
    lanes = sorted({c.end for _, _, _, values in data for c in values})

    def header_pieces(edge):
        pieces = []
        for rr in grid[:header_end]:
            c = rr[edge - 1] if edge <= len(rr) else None
            if c and c.raw and c.start >= stub_end and c.raw not in pieces:
                pieces.append(c.raw)
        return pieces

    # Some rows span the spacer cell as well as the amount cell. Coalesce such
    # edges only when they belong to the SAME header origins and never contain
    # two separate numerical values on one row. Amount/percentage lanes remain
    # separate even if they share a year heading.
    header_lanes = defaultdict(list)
    for edge in lanes:
        identity = tuple(id(rr[edge - 1]) for rr in grid[:header_end]
                         if edge <= len(rr) and rr[edge - 1] is not None
                         and rr[edge - 1].raw and rr[edge - 1].start >= stub_end)
        if identity:
            header_lanes[identity].append(edge)
    lane_alias = {}
    for edges in header_lanes.values():
        if len(edges) > 1 and all(sum(c.end in edges for c in values) <= 1
                                  for _, _, _, values in data):
            lane_alias.update({edge: max(edges) for edge in edges})
    value_edge = lambda cell: lane_alias.get(cell.end, cell.end)
    lanes = sorted({lane_alias.get(edge, edge) for edge in lanes})
    for ri, _, _, _ in data:
        for c in dedupe_origins(grid[ri]):
            if (c.start >= stub_end and numeric(c.raw)[0] == 'text' and c.raw not in {'%', '$', '€', '£', '¥'}
                    and value_edge(c) not in lanes
                    and not any(c.start <= lane-1 < c.end for lane in lanes)):
                raise ValueError('A non-numerical data column could not be aligned; inspect the preserved raw grid')
    headers = []
    for lane in lanes:
        pieces = header_pieces(lane)
        if not pieces:
            raise ValueError(f'Value column ending at physical column {lane} has no header')
        period = next((p for p in pieces if YEAR_RE.search(p)), shared_period)
        leaf = ' / '.join(p for p in pieces if p != period)
        percent_flags = []
        for ri, _, _, values in data:
            for c in values:
                if value_edge(c) == lane:
                    next_lane = next((x for x in lanes if x > lane), len(grid[ri]))
                    suffix = ' '.join(x.raw for x in dedupe_origins(grid[ri][c.end:next_lane])
                                      if x.start >= c.end)
                    percent_flags.append('%' in c.raw or suffix.strip().startswith('%'))
        if percent_flags and all(percent_flags):
            measure = 'Percentage'
        elif any(percent_flags):
            measure = 'Value'  # e.g. income-tax table mixes amounts and rates by row
        else:
            measure = 'Amount'
        if leaf:
            measure = leaf
        header = ' / '.join(([shared_period] if shared_period else []) + pieces + ([measure] if not leaf else []))
        # Units are deliberately excluded from the key so unit changes are detectable.
        key = norm(period) + '|' + norm(measure)
        headers.append(Column(key, header, period, measure, lane, period or norm(header)))
    if len({c.key for c in headers}) != len(headers):
        raise ValueError('Ambiguous/repeated column keys or inconsistent numeric colspans; review raw grid')

    rows, cells, warnings = [], [], []
    has_dollar = '$' in text(node)
    for ordinal, (ri, label, group, values) in enumerate(data, 1):
        synthetic = not label
        label_key = norm(label) if label else '__unlabelled__'
        key = norm(group) + '|' + label_key
        # Multiple unlabelled rows are deliberately ambiguous, not paired by position.
        row = Row(f'r{ordinal:03d}', key, label, group, ri + 1, synthetic)
        rows.append(row)
        for col in headers:
            matches = [v for v in values if value_edge(v) == col.physical_end]
            if len(matches) > 1:
                raise ValueError('More than one value in a logical cell')
            value = matches[0] if matches else None
            if value is None:
                text_matches = [c for c in dedupe_origins(grid[ri])
                                if c.start >= stub_end and value_edge(c) == col.physical_end
                                and c.raw and c.raw not in CURRENCY and c.raw != '%']
                if len(text_matches) > 1:
                    raise ValueError('More than one text value in a logical cell')
                candidate = text_matches[0] if text_matches else grid[ri][col.physical_end - 1]
                if candidate and candidate.start >= stub_end and candidate.raw not in CURRENCY and candidate.raw != '%':
                    value = candidate
            # Capture each origin only once, and retain the source fragments.
            if value:
                lane = col.physical_end
                left = max([stub_end] + [x for x in lanes if x < lane])
                right = next((x for x in lanes if x > lane), len(grid[ri]))
                before = [c.raw for c in dedupe_origins(grid[ri][left:value.start])
                          if c.end <= value.start and c.raw in CURRENCY]
                after = []
                for c in dedupe_origins(grid[ri][value.end:right]):
                    if not c.raw:
                        continue
                    if c.start >= value.end and c.raw == '%':
                        after.append('%')
                    break
                fragments = before + [value.raw] + after
                raw = ''.join(fragments)
                kind, number = numeric(raw)
                if '%' in raw or (kind == 'blank' and col.measure == 'Percentage'):
                    unit = '%'
                else:
                    currency = next((CURRENCY[s] for s in CURRENCY if s in raw), '')
                    # The scale statement says 'dollar'; $ elsewhere confirms USD.
                    if not currency and (scale or has_dollar):
                        currency = 'USD' if has_dollar else ''
                    unit = clean(f'{currency} {scale}')
                locator = source_paths.get(value.node) or value.node.getroottree().getpath(value.node)
            else:
                raw, kind, number, unit, fragments = '', 'blank', '', '', []
                locator = node.getroottree().getpath(node) + f'/tr[{ri+1}]'
            cells.append(Cell(row.row_id, col.key, raw, kind, number, unit, locator, fragments))
    infer_footer_total(rows, cells, headers, grid)
    duplicates = [k for k, n in Counter(r.key for r in rows).items() if n > 1]
    if duplicates:
        warnings.append('Duplicate row labels within a group require explicit row mappings: ' + ', '.join(duplicates))
    if not scale and any(c.kind == 'number' and c.unit != '%' for c in cells):
        warnings.append('No explicit numerical scale found; check units before annotation.')
    context_labels = []
    for rr in grid[:header_end]:
        for cell in dedupe_origins(rr[:stub_end]):
            if cell.raw and cell.raw not in context_labels:
                context_labels.append(cell.raw)
    return headers, rows, cells, warnings, ' / '.join(context_labels)


def load_source(source: str, cache: Path, user_agent: str = '') -> bytes:
    if re.match(r'^https?://', source, re.I):
        if urlparse(source).hostname in {'sec.gov', 'www.sec.gov', 'data.sec.gov'} and not user_agent:
            raise ValueError('SEC URL downloads require --user-agent "Your name your-email@example.com". '
                             'Alternatively save the filing as 2024.html / 2025.html and use local files.')
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (hashlib.sha256(source.encode()).hexdigest() + '.html')
        if path.exists():
            return path.read_bytes()
        req = Request(source, headers={'User-Agent': user_agent or 'HTMLTableAnnotator/1.0',
                                      'Accept': 'text/html,application/xhtml+xml'})
        try:
            with urlopen(req, timeout=45) as response:
                data = response.read()
        except Exception as exc:
            raise ValueError(f'Could not download {source}: {exc}. Save the filing HTML locally instead.') from exc
        path.write_bytes(data)
        return data
    path = Path(source).expanduser()
    if not path.is_file():
        raise ValueError(f'File not found: {path}. Use .html or .htm, not PDF.')
    return path.read_bytes()


class SecClient:
    """SEC submissions API + original document retrieval. HTML stays in memory by default.

    Metadata cache expires after 24 hours; accession-specific documents are immutable.
    A per-client rate limit and bounded transient retries keep network behavior predictable.
    """
    def __init__(self, user_agent: str, cache_dir: Path | None = None, refresh=False):
        if not user_agent.strip():
            raise ValueError('SEC API mode requires --user-agent "Your name your-email@example.com" '
                             'or the SEC_USER_AGENT environment variable. No API key is needed.')
        self.user_agent = user_agent
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.refresh = refresh
        self.memo: dict[str, bytes] = {}
        self.last_request = 0.0

    def get(self, url: str, metadata=False) -> bytes:
        host = urlparse(url).hostname or ''
        if urlparse(url).scheme != 'https' or host not in {'www.sec.gov','data.sec.gov'}:
            raise ValueError(f'SEC client only accepts official SEC HTTPS URLs: {url}')
        if url in self.memo:
            return self.memo[url]
        cache_file = None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ('.json' if metadata else '.html'))
            if cache_file.exists() and not self.refresh:
                fresh = not metadata or time.time() - cache_file.stat().st_mtime < 86400
                if fresh:
                    data = cache_file.read_bytes()
                    self.memo[url] = data
                    return data
        for attempt in range(3):
            delay = 0.25 - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            request = Request(url, headers={'User-Agent': self.user_agent,
                                           'Accept': 'application/json' if metadata else 'text/html,application/xhtml+xml'})
            try:
                with urlopen(request, timeout=30) as response:
                    data = response.read()
                break
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise ValueError(f'SEC returned HTTP {exc.code} for {url}. '
                                     'Check your identifying User-Agent and try again later; access restrictions are not bypassed.') from exc
                retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
                if retry_after and not re.fullmatch(r'\d+(?:\.\d+)?', retry_after):
                    raise ValueError(f'SEC requested retry after {retry_after}. Retry the command after that time.') from exc
                wait = float(retry_after) if re.fullmatch(r'\d+(?:\.\d+)?', retry_after) else 2 ** attempt
                if wait > 30:
                    raise ValueError(f'SEC requested a {wait:g}-second pause. Retry the command later.') from exc
                time.sleep(max(wait, 0.25))
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise ValueError(f'Could not reach the SEC: {exc}. Retry the command when the connection is available.') from exc
                time.sleep(2 ** attempt)
        if cache_file:
            cache_file.write_bytes(data)
        self.memo[url] = data
        return data

    def json(self, url: str) -> dict:
        data = self.get(url, metadata=True)
        try:
            result = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f'SEC returned a non-JSON response for {url}. No filing selection was made.') from exc
        if not isinstance(result, dict):
            raise ValueError(f'Unexpected SEC metadata structure at {url}')
        return result


def normalize_cik(value: str) -> str:
    digits = re.sub(r'^CIK', '', str(value).strip(), flags=re.I)
    if not re.fullmatch(r'\d{1,10}', digits) or int(digits) == 0:
        raise ValueError('CIK must contain 1–10 digits, e.g. 723125 for Micron.')
    return f'{int(digits):010d}'


def submissions_rows(columnar: dict) -> list[dict]:
    """SEC uses parallel arrays, not a list of filing objects."""
    count = len(columnar.get('accessionNumber', []))
    required = ['accessionNumber', 'form', 'filingDate', 'primaryDocument']
    if any(len(columnar.get(k, [])) != count for k in required):
        raise ValueError('SEC submissions arrays have inconsistent lengths.')
    return [{k: v[i] for k, v in columnar.items() if isinstance(v, list) and i < len(v)}
            for i in range(count)]


def sec_document_url(cik: str, record: dict) -> str:
    accession = record.get('accessionNumber', '')
    document = record.get('primaryDocument', '')
    if not re.fullmatch(r'\d{10}-\d{2}-\d{6}', accession):
        raise ValueError(f'Invalid accession number in SEC response: {accession}')
    if not document or document.startswith('/') or '..' in document.split('/'):
        raise ValueError('SEC filing record has an invalid primary document path.')
    return f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{quote(document, safe="/")}'


def document_fiscal_year(data: bytes) -> int | None:
    # Read DEI before hidden Inline XBRL metadata is removed by table extraction.
    root = html.fromstring(data, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    values = set()
    for e in root.iter():
        if e.get('name', '').split(':')[-1].lower() == 'documentfiscalyearfocus':
            value = text(e)
            if re.fullmatch(r'\d{4}', value):
                values.add(int(value))
    if len(values) > 1:
        raise ValueError('The filing contains conflicting DocumentFiscalYearFocus tags; select the document explicitly.')
    return next(iter(values)) if values else None


def discover_sec_filings(client: SecClient, years: list[int], ticker='', cik='', accessions=None):
    """Return issuer metadata and year-indexed, verified original 10-K selections.

    10-K/A amendments are excluded. reportDate narrows candidates; the document's
    DEI fiscal-year tag verifies them. filingDate is never used as the fiscal year.
    """
    accessions = accessions or {}
    if ticker:
        wanted = ticker.strip().upper()
        directory = client.json('https://www.sec.gov/files/company_tickers.json')
        matches = [entry for entry in directory.values() if isinstance(entry, dict)
                   and str(entry.get('ticker', '')).upper() == wanted]
        if len(matches) != 1:
            raise ValueError(f'Ticker {wanted!r} did not resolve uniquely. Use --cik with the company SEC identifier.')
        cik = normalize_cik(str(matches[0]['cik_str']))
    else:
        cik = normalize_cik(cik)
    endpoint = f'https://data.sec.gov/submissions/CIK{cik}.json'
    company = client.json(endpoint)
    if company.get('cik') and normalize_cik(str(company['cik'])) != cik:
        raise ValueError('SEC issuer response does not match the requested CIK.')
    filings = company.get('filings', {})
    records = submissions_rows(filings.get('recent', {}))
    history = list(filings.get('files', []))
    loaded_history = []

    def load_history():
        while history:
            entry = history.pop(0)
            name = entry.get('name', '')
            if not re.fullmatch(r'CIK\d{10}-submissions-\d+\.json', name):
                raise ValueError(f'Unrecognized SEC historical submissions filename: {name}')
            old = client.json('https://data.sec.gov/submissions/' + name)
            records.extend(submissions_rows(old.get('filings', {}).get('recent', old)))
            loaded_history.append(name)

    def originals():
        seen = set()
        result = []
        for record in records:
            accession = record.get('accessionNumber')
            if record.get('form') == '10-K' and accession not in seen:
                seen.add(accession)
                result.append(record)
        return result

    def inspect(record, wanted):
        url = sec_document_url(cik, record)
        data = client.get(url)
        declared = document_fiscal_year(data)
        report_date = record.get('reportDate', '')
        report_year = int(report_date[:4]) if re.match(r'^\d{4}-\d{2}-\d{2}$', report_date) else None
        if declared is not None and declared != wanted:
            return None
        if declared is None and report_year != wanted:
            return None
        return {**record, 'url': url, 'requestedFiscalYear': wanted,
                'declaredFiscalYear': declared,
                'fiscalYearSelection': 'DocumentFiscalYearFocus' if declared is not None else 'reportDate fallback — verify issuer fiscal-year label',
                'cik': cik}

    selected = {}
    for year in years:
        accession = accessions.get(year)
        if accession:
            available = [r for r in originals() if r['accessionNumber'] == accession]
            if not available:
                load_history()
                available = [r for r in originals() if r['accessionNumber'] == accession]
            if len(available) != 1:
                raise ValueError(f'Accession {accession} is not an original 10-K for this issuer.')
            match = inspect(available[0], year)
            if match is None:
                raise ValueError(f'Accession {accession} does not match requested fiscal year {year}.')
            selected[year] = match
            continue

        def candidates(offset):
            return [r for r in originals() if re.match(r'^\d{4}-\d{2}-\d{2}$', r.get('reportDate', ''))
                    and abs(int(r['reportDate'][:4]) - year) == offset]

        if not candidates(0):
            load_history()
        matching = [m for r in candidates(0) if (m := inspect(r, year)) is not None]
        verified = [m for m in matching if m['declaredFiscalYear'] is not None]
        # Handle issuers whose named fiscal year differs from the end-date calendar year.
        if not verified:
            load_history()
            matching = [m for r in candidates(0) + candidates(1) if (m := inspect(r, year)) is not None]
            verified = [m for m in matching if m['declaredFiscalYear'] is not None]
        eligible = verified or matching
        if not eligible:
            available = ', '.join(sorted({r.get('reportDate', '') for r in originals() if r.get('reportDate')}))
            raise ValueError(f'No original 10-K for fiscal year {year} was found. Available report dates: {available}. '
                             'Use a verified --previous-accession or --current-accession for unusual filing histories.')
        if len(eligible) > 1:
            choices = '; '.join(f'{r["accessionNumber"]} (report {r.get("reportDate")}, filed {r.get("filingDate")})' for r in eligible)
            raise ValueError(f'Multiple original 10-Ks match fiscal year {year}: {choices}. '
                             'Choose --previous-accession or --current-accession explicitly.')
        selected[year] = eligible[0]
    if len({r['accessionNumber'] for r in selected.values()}) != len(selected):
        raise ValueError('The same filing was selected for both fiscal years; review the requested periods.')
    issuer = {'name': company.get('name', ''), 'cik': cik, 'tickers': company.get('tickers', []),
              'submissionsAPI': endpoint, 'historicalFilesRead': loaded_history,
              'retrievedAtUTC': datetime.now(timezone.utc).isoformat()}
    return issuer, selected


def extract_tables(data: bytes, company: str, year: int, item: str, source: str = '',
                   item_xpath: str = '', end_xpath: str = '', page_map: dict | None = None):
    if data.lstrip().startswith(b'%PDF'):
        raise ValueError('This program reads HTML, not PDF. Save the full SEC filing as HTML.')
    root = html.fromstring(data, parser=html.HTMLParser(encoding='utf-8', no_network=True))
    source_paths = {e: root.getroottree().getpath(e) for e in root.iter()
                    if e.tag in {'table', 'tr', 'td', 'th'}}
    explicit_start = root.xpath(item_xpath) if item_xpath else []
    explicit_end = root.xpath(end_xpath) if end_xpath else []
    for e in list(root.iter()):
        tag = str(e.tag).lower()
        style = e.get('style', '').replace(' ', '').lower()
        if tag in {'script', 'style', 'noscript', 'ix:header', 'ix:hidden'} or 'display:none' in style or 'visibility:hidden' in style:
            if e.getparent() is not None:
                e.drop_tree()
    separate_inline_prose(root)
    nodes = list(root.iter())
    order = {e: i for i, e in enumerate(nodes)}
    blocks = primitive_blocks(root)
    if item_xpath:
        selected = explicit_start
        if len(selected) != 1:
            raise ValueError('Item XPath must select exactly one element')
        start = order[selected[0]]
        if not end_xpath:
            raise ValueError('An explicit Item start XPath also requires an end XPath')
        ends = explicit_end
        if len(ends) != 1:
            raise ValueError('End XPath must select exactly one element')
        end, headings = order[ends[0]], []
    else:
        start, end, headings = find_item(blocks, order, item)
    if end <= start:
        raise ValueError('Item end must follow Item start')
    section_blocks = [(order[b], text(b), b) for b in blocks if start <= order[b] < end]
    title_headings = [(p, title) for p, s, b in section_blocks
                      if (title := explicit_table_heading(s, b))]
    title_positions = [p for p, _ in title_headings]
    section_text = ' '.join(s for _, s, _ in section_blocks)
    default_scale, default_evidence = detect_unit(section_text[:2500])

    # Footer candidates are outside tables. Only use numbers near an actual page break.
    breaks = sorted(order[e] for e in nodes if re.search(r'(?:page-break-before|break-before)\s*:\s*(?:always|page)', e.get('style', ''), re.I))
    footers = []
    for b in blocks:
        s = text(b)
        m = re.fullmatch(r'(\d{1,4})(?:\s*\|\s*\d{4}\s+10-K)?', s, re.I)
        if not m:
            continue
        pos = order[b]
        following = bisect.bisect_right(breaks, pos)
        near_break = following < len(breaks) and breaks[following] - pos < 35
        footer_style = any('bottom:' in e.get('style', '').replace(' ', '') for e in [b, *list(b.iterancestors())[:3]])
        if near_break or footer_style or '10-K' in s:
            footers.append((pos, m.group(1)))
    footers.sort()
    footer_positions = [p for p, _ in footers]
    all_tables = [e for e in nodes if e.tag == 'table']
    raw_candidates = [e for e in all_tables if start < order[e] < end and not list(e.iter('table'))[1:]]
    page_counts = Counter()
    section_counts, table_contexts = Counter(), {}
    output, skipped = [], []
    block_positions = [p for p, _, _ in section_blocks]
    previous_table_end = start
    for node in raw_candidates:
        pos = order[node]
        locator = source_paths[node]
        idx = bisect.bisect_left(block_positions, pos)
        previous = section_blocks[max(0, idx-8):idx]
        heading_index = bisect.bisect_left(title_positions, pos) - 1
        heading = title_headings[heading_index] if heading_index >= 0 else None
        section_key = heading[0] if heading else start
        intro = preceding_table_sentence(previous, heading, previous_table_end)
        title = choose_table_title(node, previous, heading, previous_table_end)
        previous_table_end = max(order[e] for e in node.iter())
        if not title:
            title = f'Untitled table {len(output)+1}'
        fi = bisect.bisect_right(footer_positions, pos)
        page = ''
        if fi < len(footers):
            next_break = bisect.bisect_right(breaks, pos)
            if next_break == len(breaks) or footers[fi][0] < breaks[next_break]:
                page = footers[fi][1]
        if page_map and locator in page_map:
            page = str(page_map[locator])
        context = ' '.join(s for _, s, _ in previous[-3:]) + ' ' + ' '.join(text(e) for e in list(node.iter('tr'))[:3])
        local_scale, local_evidence = detect_unit(context)
        scale, evidence = local_scale or default_scale, local_evidence or default_evidence
        try:
            columns, rows, cells, warnings, header_context = parse_table(node, title, scale, evidence, source_paths)
        except ValueError as exc:
            try:
                grid, _ = expand_grid(node)
                raw = [[c.raw if c else '' for c in rr] for rr in grid]
            except ValueError:
                raw = [[text(c) for c in tr if c.tag in {'td', 'th'}] for tr in node.iter('tr')]
            # Navigation/layout tables have no financial values and are not annotations.
            amount_count = sum(numeric(v)[0] in {'number', 'dash'} for rr in raw for v in rr)
            if amount_count >= 2:
                section_counts[section_key] += 1
                page_counts[page or 'unknown'] += 1
                unsupported_id = f'{slug(company)}_{year}_{page or "unknown"}_t{page_counts[page or "unknown"]}'
                skipped.append({'table_id': unsupported_id, 'locator': locator, 'title': title, 'reason': str(exc), 'raw_grid': raw})
            continue
        page_counts[page or 'unknown'] += 1
        section_counts[section_key] += 1
        tid = f'{slug(company)}_{year}_{page or "unknown"}_t{page_counts[page or "unknown"]}'
        if not page:
            warnings.append('Printed page unavailable; ID uses unknown, never the HTML/PDF file position.')
        notes = []
        # Only immediately adjacent table footnotes, not MD&A narrative paragraphs.
        for _, s, _ in section_blocks[idx:idx+3]:
            if re.match(r'^(?:Percentages?\b|\(\d+\)|\*|Note:)', s, re.I) and len(s) < 1000:
                notes.append(s)
            else:
                break
        raw_grid, _ = expand_grid(node)
        output.append(Table(tid, title, page, locator, source, year, item, len(output)+1,
                            columns, rows, cells, notes, warnings, evidence,
                            [[c.raw if c else '' for c in rr] for rr in raw_grid], header_context,
                            source_section=heading[1] if heading else '', introductory_sentence=intro))
        caption = node.find('caption')
        table_contexts[tid] = (section_key, text(caption) if caption is not None else '')
    name_section_tables(output, table_contexts, section_counts)
    return output, {'item': item, 'start_position': start, 'end_position': end,
                    'headings': headings, 'candidate_tables': len(raw_candidates),
                    'unsupported_tables': skipped, 'sha256': hashlib.sha256(data).hexdigest()}


def unique_pairs(previous, current, key, id_key, overrides=None):
    """Exact normalized, unique matches only; never guess by position or numbers."""
    pm, cm = {id_key(x): x for x in previous}, {id_key(x): x for x in current}
    pairs = []
    for old_id, new_id in (overrides or {}).items():
        if old_id not in pm or new_id not in cm:
            raise ValueError(f'Invalid or duplicate mapping {old_id!r} -> {new_id!r}')
        pairs.append((pm.pop(old_id), cm.pop(new_id)))
    pg, cg = defaultdict(list), defaultdict(list)
    for x in pm.values():
        pg[key(x)].append(x)
    for x in cm.values():
        cg[key(x)].append(x)
    for k in pg:
        if len(pg[k]) == 1 and len(cg.get(k, [])) == 1:
            a, b = pg[k][0], cg[k][0]
            pairs.append((a, b))
            pm.pop(id_key(a))
            cm.pop(id_key(b))
    return pairs, list(pm.values()), list(cm.values())


def row_name(row: Row) -> str:
    label = row.label or f'[Unlabelled row {row.row_id}]'
    return f'{row.group} / {label}' if row.group else label


class TableView:
    """Build lookup indexes once instead of rescanning tables for each comparison."""
    def __init__(self, table: Table):
        self.table = table
        self.rows = {r.row_id: r for r in table.rows}
        self.cols = {c.key: c for c in table.columns}
        self.cells = {(c.row_id, c.column_key): c for c in table.cells}

    def evidence(self, row_ids=None, column_keys=None):
        rr = row_ids if row_ids is not None else self.rows
        cc = column_keys if column_keys is not None else self.cols
        entries = []
        for rid in rr:
            for key in cc:
                cell = self.cells[(rid, key)]
                entries.append({
                    'row_id': rid, 'row': self.rows[rid].label, 'row_group': self.rows[rid].group,
                    'column': self.cols[key].header, 'period': self.cols[key].period,
                    'unit': cell.unit, 'raw': cell.raw, 'kind': cell.kind,
                    'number': cell.number, 'locator': cell.locator,
                })
        return entries


def evidence_text(entries: list[dict]) -> str:
    return '; '.join(
        f'{(e["row_group"] + " / ") if e["row_group"] else ""}'
        f'{e["row"] or "[Unlabelled " + e["row_id"] + "]"}'
        f' [{e["column"]}; {e["unit"] or "unit unspecified"}] = '
        f'{e["raw"] if e["kind"] != "blank" else "[blank]"}' for e in entries
    )


def compare_tables(previous: list[Table], current: list[Table], metadata: dict,
                   mappings: dict | None = None, include_unchanged: bool = False,
                   ignore_number_formatting: bool = False):
    mappings = mappings or {}
    pairs, removed_tables, added_tables = unique_pairs(
        previous, current, lambda t: norm(t.title), lambda t: t.table_id,
        mappings.get('table_pairs'))
    pairs.sort(key=lambda pair: pair[0].index)
    records, cell_audit = [], []
    statistics = Counter()
    pair_number = 0

    def emit(old, new, pair_id, level, detail, taxonomy, rationale,
             pe=None, ce=None, previous_row='', current_row='', previous_header='',
             current_header='', previous_period='', current_period='', status='Matched', notes='',
             previous_text=None, current_text=None):
        pe, ce = pe or [], ce or []
        record = {k: '' for k in ANNOTATION_COLUMNS}
        record.update(metadata)
        record.update({
            'Change ID': f'{slug(metadata["Company"])}_{metadata["Previous Fiscal Year"]}_{metadata["Current Fiscal Year"]}_{metadata["Item"]}_C{len(records)+1:05d}',
            'Table Pair ID': pair_id, 'Previous Table ID': old.table_id if old else '',
            'Current Table ID': new.table_id if new else '',
            'Previous Table Title': old.title if old else '', 'Current Table Title': new.title if new else '',
            'Change Level': level, 'Change Detail': detail,
            'Previous Row Label': previous_row, 'Current Row Label': current_row,
            'Previous Column Header': previous_header, 'Current Column Header': current_header,
            'Previous Data Period': previous_period, 'Current Data Period': current_period,
            'Previous Unit': '; '.join(dict.fromkeys(e['unit'] for e in pe if e['unit'])),
            'Current Unit': '; '.join(dict.fromkeys(e['unit'] for e in ce if e['unit'])),
            'Previous Evidence': evidence_text(pe) if previous_text is None else previous_text,
            'Current Evidence': evidence_text(ce) if current_text is None else current_text,
            'Change Taxonomy': taxonomy, 'Match Status': status,
            'Rationale / Evidence': rationale,
            'Previous Source URL': old.source if old else metadata.get('Previous Source URL', ''),
            'Current Source URL': new.source if new else metadata.get('Current Source URL', ''),
            'Review Notes': notes,
        })
        # Keep machine-readable evidence outside the customizable CSV projection.
        record['_previous_cells'], record['_current_cells'] = pe, ce
        records.append(record)
        statistics[level + ': ' + taxonomy] += 1
        return record['Change ID']

    def audit_entries(old, new, pe, ce, result, change_id, status):
        # One audit row per source cell for structural changes, including blanks.
        for side, table, entries in [('previous', old, pe), ('current', new, ce)]:
            for e in entries:
                cell_audit.append({'Side': side, 'Table ID': table.table_id, 'Row ID': e['row_id'],
                                   'Column Header': e['column'], 'Raw Value': e['raw'],
                                   'Result': result, 'Change ID': change_id, 'Match Status': status})

    for old, new in pairs:
        pair_number += 1
        pair_id = f'T{pair_number:03d}'
        pv, cv = TableView(old), TableView(new)
        all_notes = '; '.join(dict.fromkeys(old.warnings + new.warnings))
        context_review = norm(old.header_context) != norm(new.header_context)
        if old.header_context != new.header_context:
            if context_review:
                all_notes += ('; ' if all_notes else '') + 'Period/header context changed; verify duration and reporting basis.'
            emit(old, new, pair_id, 'Column', 'Table header context changed',
                 'Modified' if context_review else 'Reworded',
                 'The stub header describing the table periods or measures changed. '
                 'Verify that shared-period cell comparisons remain meaningful.',
                 previous_text=old.header_context, current_text=new.header_context,
                 status='Needs review' if context_review else 'Matched', notes=all_notes)
        if old.title != new.title:
            emit(old, new, pair_id, 'Table', 'Table title changed', 'Reworded',
                 'The matched table title text changed. Compare the exact previous and current titles.',
                 previous_text=old.title, current_text=new.title, notes=all_notes)
        rp, old_rows, new_rows = unique_pairs(old.rows, new.rows, lambda r: r.key, lambda r: r.row_id,
                                             mappings.get('row_pairs', {}).get(old.table_id))
        cp, old_cols, new_cols = unique_pairs(old.columns, new.columns, lambda c: c.key, lambda c: c.key,
                                             mappings.get('column_pairs', {}).get(old.table_id))
        # A large row-set change can alter even same-named segments' reporting basis.
        row_overlap = len(rp) / max(len(old.rows), len(new.rows), 1)
        basis_review = bool(old_rows and new_rows and row_overlap < 0.6)
        base_status = 'Needs review' if basis_review or all_notes else 'Matched'
        if basis_review:
            all_notes += ('; ' if all_notes else '') + 'Most row categories differ. Verify reporting basis before treating same-named rows as comparable.'
        shared_old_cols, shared_new_cols = [a.key for a, _ in cp], [b.key for _, b in cp]

        # Precedence: table > column > row > cell. Intersections are covered once.
        for side, cols, view in [('previous', old_cols, pv), ('current', new_cols, cv)]:
            groups = defaultdict(list)
            for col in cols:
                groups[col.group].append(col)
            for _, selected in groups.items():
                entries = view.evidence(column_keys=[c.key for c in selected])
                is_new = side == 'current'
                pe, ce = ([], entries) if is_new else (entries, [])
                header = '; '.join(c.header for c in selected)
                periods = '; '.join(dict.fromkeys(c.period for c in selected if c.period))
                verb, tax = ('added', 'New') if is_new else ('removed', 'Removed')
                other_headers = ', '.join(c.header for c in (old.columns if is_new else new.columns))
                cid = emit(old, new, pair_id, 'Column', f'Column group {verb}', tax,
                           f'The {header} column group is present only in the {side} filing. '
                           f'Counterpart table headers: {other_headers}. '
                           f'This record covers {len(entries)} extracted cells, including blank cells.',
                           pe, ce, previous_row='All rows' if pe else '', current_row='All rows' if ce else '',
                           previous_header=header if pe else '', current_header=header if ce else '',
                           previous_period=periods if pe else '', current_period=periods if ce else '',
                           status=base_status, notes=all_notes)
                audit_entries(old, new, pe, ce, tax, cid, base_status)

        for side, rows, view, keys in [('previous', old_rows, pv, shared_old_cols),
                                       ('current', new_rows, cv, shared_new_cols)]:
            for row in rows:
                entries = view.evidence([row.row_id], keys)
                is_new = side == 'current'
                pe, ce = ([], entries) if is_new else (entries, [])
                opposite = old_rows if is_new else new_rows
                status = 'Needs review' if opposite or all_notes else 'Unmatched'
                note = all_notes
                if opposite:
                    note += ('; ' if note else '') + 'Unmatched rows exist on both sides; a rename/reorganization is possible. Supply a verified row mapping if appropriate.'
                labels = '; '.join(row_name(r) for r in (old.rows if is_new else new.rows))
                verb, tax = ('added', 'New') if is_new else ('removed', 'Removed')
                cid = emit(old, new, pair_id, 'Row', f'Unmatched row {verb}', tax,
                           f'No unique label match was found for {row_name(row)!r} in the counterpart table. '
                           f'Counterpart row labels: {labels}. The evidence covers shared columns; '
                           'added/removed columns have precedence at intersections. New/Removed describes label presence, not a business opening/closure.',
                           pe, ce, previous_row=row_name(row) if pe or not is_new else '',
                           current_row=row_name(row) if ce or is_new else '',
                           previous_header='; '.join(pv.cols[k].header for k in keys) if not is_new else '',
                           current_header='; '.join(cv.cols[k].header for k in keys) if is_new else '',
                           status=status, notes=note)
                audit_entries(old, new, pe, ce, tax, cid, status)

        for pr, cr in rp:
            if pr.label != cr.label or pr.group != cr.group:
                label_tax = 'Reworded' if norm(row_name(pr)) == norm(row_name(cr)) else 'Modified'
                emit(old, new, pair_id, 'Row', 'Matched row label changed', label_tax,
                     'The aligned row label changed. A manual mapping establishes correspondence but does not establish unchanged economic meaning.',
                     previous_row=row_name(pr), current_row=row_name(cr),
                     previous_text=row_name(pr), current_text=row_name(cr), status=base_status, notes=all_notes)
            for pc, cc in cp:
                p, c = pv.cells[(pr.row_id, pc.key)], cv.cells[(cr.row_id, cc.key)]
                same_number = p.kind == c.kind == 'number' and Decimal(p.number) == Decimal(c.number)
                same_value = same_number or (p.kind == c.kind and p.kind != 'number' and p.raw == c.raw)
                if p.unit != c.unit:
                    result, detail = 'Modified', 'Unit changed'
                elif not same_value:
                    result, detail = 'Modified', 'Cell value changed'
                elif p.raw != c.raw and not ignore_number_formatting:
                    result, detail = 'Reworded', 'Value display changed'
                else:
                    result, detail = 'Unchanged', 'Cell unchanged'
                pe, ce = pv.evidence([pr.row_id], [pc.key]), cv.evidence([cr.row_id], [cc.key])
                cid = ''
                if result != 'Unchanged' or include_unchanged:
                    cid = emit(old, new, pair_id, 'Cell', detail, result,
                               f'Compared {row_name(pr)!r}, {pc.header!r} against '
                               f'{row_name(cr)!r}, {cc.header!r}: {p.raw or "[blank]"} ({p.unit or "unit unspecified"}) '
                               f'→ {c.raw or "[blank]"} ({c.unit or "unit unspecified"}). {detail}.',
                               pe, ce, row_name(pr), row_name(cr), pc.header, cc.header,
                               pc.period, cc.period, base_status, all_notes)
                statistics['matched_cells'] += 1
                statistics['matched_cells_' + result.lower()] += 1
                audit_entries(old, new, pe, ce, result, cid, base_status)
        for pc, cc in cp:
            if pc.header != cc.header:
                emit(old, new, pair_id, 'Column', 'Matched column header changed', 'Reworded',
                     'The aligned column header text changed; the source headers are retained verbatim.',
                     previous_header=pc.header, current_header=cc.header,
                     previous_period=pc.period, current_period=cc.period,
                     previous_text=pc.header, current_text=cc.header, status=base_status, notes=all_notes)
        if old.notes != new.notes:
            tax = 'New' if not old.notes else 'Removed' if not new.notes else 'Modified'
            emit(old, new, pair_id, 'Footnote', 'Adjacent table footnote changed', tax,
                 'The directly adjacent table footnote text differs. Narrative MD&A paragraphs are excluded.',
                 previous_text=' '.join(old.notes), current_text=' '.join(new.notes),
                 status=base_status, notes=all_notes)

    for side, tables in [('previous', removed_tables), ('current', added_tables)]:
        for table in tables:
            pair_number += 1
            is_new = side == 'current'
            old, new = (None, table) if is_new else (table, None)
            entries = TableView(table).evidence()
            pe, ce = ([], entries) if is_new else (entries, [])
            opposite = removed_tables if is_new else added_tables
            status = 'Needs review' if opposite else 'Unmatched'
            tax = 'New' if is_new else 'Removed'
            other_titles = '; '.join(t.title for t in (previous if is_new else current))
            cid = emit(old, new, f'T{pair_number:03d}', 'Table', 'Unmatched table', tax,
                       f'No unique title match was found for {table.title!r}. Counterpart titles: {other_titles}. '
                       'Check for renamed, split, or continued tables before confirming this classification.',
                       pe, ce, status=status, notes='; '.join(table.warnings))
            audit_entries(old, new, pe, ce, tax, cid, status)
    statistics['table_pairs'] = len(pairs)
    statistics['annotations'] = len(records)
    # Every extracted source cell must be accounted for exactly once in the audit.
    expected = sum(len(t.cells) for t in previous + current)
    keys = [(r['Side'], r['Table ID'], r['Row ID'], r['Column Header']) for r in cell_audit]
    if len(keys) != expected or len(set(keys)) != expected:
        raise AssertionError(f'Coverage failure: {expected} source cells, {len(keys)} audit entries, {len(set(keys))} unique entries')
    statistics['source_cells_accounted_for'] = expected
    return records, cell_audit, dict(statistics)


def write_delimited(path: Path, records: list[dict], columns: list, delimiter=',', header=True):
    # CSV quoting handles commas. TSV sanitization keeps one annotation per physical line.
    specs = [(c, c) if isinstance(c, str) else (c['key'], c.get('label', c['key'])) for c in columns]
    with path.open('w', newline='', encoding='utf-8-sig' if delimiter == ',' else 'utf-8') as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator='\n')
        if header:
            writer.writerow([label for _, label in specs])
        for record in records:
            values = []
            for key, _ in specs:
                value = str(record.get(key, ''))
                if delimiter == '\t':
                    value = value.replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')
                # Avoid spreadsheet formula evaluation of disclosure text. Raw JSON is intact.
                if value.startswith(('=', '+', '@')) or (value.startswith('-') and numeric(value)[0] == 'text'):
                    value = "'" + value
                values.append(value)
            writer.writerow(values)


def raw_cell_rows(tables, side):
    result = []
    for t in tables:
        view = TableView(t)
        for cell in t.cells:
            row, col = view.rows[cell.row_id], view.cols[cell.column_key]
            result.append({'Side': side, 'Filing Fiscal Year': t.year, 'Item': t.item,
                           'Table ID': t.table_id, 'Table Title': t.title, 'Printed Page': t.page,
                           'Row ID': row.row_id, 'Row Label': row.label, 'Row Group': row.group,
                           'Physical Row': row.physical_row, 'Column Key': col.key,
                           'Column Header': col.header, 'Data Period': col.period,
                           'Measure': col.measure, 'Unit': cell.unit, 'Raw Value': cell.raw,
                           'Value Kind': cell.kind, 'Parsed Value': cell.number,
                           'Source Locator': cell.locator, 'Source URL': t.source})
    return result


def export_results(out: Path, previous, current, records, cell_audit, stats, diagnostics, columns=None):
    out.mkdir(parents=True, exist_ok=True)
    columns = columns or ANNOTATION_COLUMNS
    for c in columns:
        key = c if isinstance(c, str) else c.get('key')
        if key not in ANNOTATION_COLUMNS:
            raise ValueError(f'Unknown output column: {key}')
    write_delimited(out/'annotations.csv', records, columns)
    write_delimited(out/'annotations.tsv', records, columns, '\t')
    write_delimited(out/'paste_into_sheets.tsv', records, columns, '\t', header=False)
    raw = raw_cell_rows(previous, 'previous') + raw_cell_rows(current, 'current')
    raw_headers = list(raw[0]) if raw else ['Side', 'Table ID', 'Row Label', 'Column Header', 'Raw Value']
    write_delimited(out/'extracted_cells.csv', raw, raw_headers)
    audit_headers = list(cell_audit[0]) if cell_audit else ['Side', 'Table ID', 'Result', 'Change ID']
    write_delimited(out/'cell_comparison_audit.csv', cell_audit, audit_headers)
    register = [{'Side': side, 'Table ID': t.table_id, 'Table Title': t.title,
                 'Printed Page': t.page, 'Rows': len(t.rows), 'Columns': len(t.columns),
                 'Source Cells': len(t.cells), 'Locator': t.locator,
                 'Warnings': '; '.join(t.warnings)}
                for side, tables in [('previous', previous), ('current', current)] for t in tables]
    write_delimited(out/'table_register.csv', register, list(register[0]) if register else ['Side', 'Table ID'])
    warnings = []
    for side, diag in diagnostics.items():
        for skipped in diag.get('unsupported_tables', []):
            warnings.append({'Side': side, 'Table': skipped['title'], 'Locator': skipped['locator'], 'Issue': skipped['reason']})
    for row in register:
        if row['Warnings']:
            warnings.append({'Side': row['Side'], 'Table': row['Table Title'], 'Locator': row['Locator'], 'Issue': row['Warnings']})
    write_delimited(out/'extraction_warnings.csv', warnings, ['Side', 'Table', 'Locator', 'Issue'])
    audit = {'version': VERSION, 'comparison_mode': 'same_period', 'statistics': stats,
             'diagnostics': diagnostics, 'previous_tables': [asdict(t) for t in previous],
             'current_tables': [asdict(t) for t in current], 'annotations': records,
             'cell_comparison_audit': cell_audit,
             'notes': ['Content Taxonomy and Materiality are deliberately blank for human annotation.',
                       'New/Removed for unmatched labels is a candidate classification; review Match Status.',
                       'Empty values, dashes and zero are distinct. Printed pages never come from document positions.']}
    (out/'audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')


def json_cell_value(cell: Cell):
    """JSON-friendly number, original text, or null; never coerce dash to zero.

    Keep unusually precise decimals / integers as strings when common JSON
    readers (including JavaScript) would round them. raw_value is always retained.
    """
    if cell.kind in {'blank', 'dash'}:
        return None
    if cell.kind != 'number':
        return cell.raw
    number = Decimal(cell.number)
    if number == number.to_integral_value():
        return int(number) if abs(number) <= 2**53 - 1 else cell.number
    candidate = float(number)
    if Decimal(str(candidate)) == number:
        return candidate
    return cell.number


def unique_json_key(base: str, suffix: str, used: set, reserved=()) -> str:
    candidate, index = f'{base} [{suffix}]', 2
    while candidate in used or candidate in reserved:
        candidate = f'{base} [{suffix}-{index}]'
        index += 1
    return candidate


def json_row_paths(rows: list[Row]) -> dict[str, list[str]]:
    """Use actual group/row labels; disambiguate duplicates without overwriting.

    No group means the row label IS the section, not an invented subsection.
    Confirmed footer totals use an explicitly recorded inferred label. Other
    unlabelled rows keep a source-row placeholder; position alone is not enough.
    """
    labels = {r.row_id: r.label or r.inferred_label or f'[Unlabelled row {r.row_id}]' for r in rows}
    counts = Counter((r.group, labels[r.row_id]) for r in rows)
    groups = {r.group for r in rows if r.group}
    reserved = defaultdict(set)
    reserved[''].update(groups)
    for row in rows:
        reserved[row.group].add(labels[row.row_id])
    used = defaultdict(set)
    used[''].update(groups)
    paths = {}
    for row in rows:
        label = labels[row.row_id]
        if counts[(row.group, label)] > 1 or label in used[row.group]:
            label = unique_json_key(label, row.row_id, used[row.group], reserved[row.group])
        used[row.group].add(label)
        paths[row.row_id] = [row.group, label] if row.group else [label]
    return paths


def json_value_leaf(pairs: list[tuple[Column, Cell]]) -> dict:
    """One row/period leaf. Keep every measure, including amount AND percentage."""
    if len(pairs) == 1:
        _, cell = pairs[0]
        return {'value': json_cell_value(cell), 'unit': cell.unit, 'raw_value': cell.raw}
    measures = {norm(col.measure): cell for col, cell in pairs}
    if len(pairs) == 2 and set(measures) == {'amount', 'percentage'}:
        amount, percentage = measures['amount'], measures['percentage']
        return {'value': json_cell_value(amount), 'unit': amount.unit, 'raw_value': amount.raw,
                'percentage': json_cell_value(percentage),
                'raw_percentage': None if percentage.kind == 'blank' else percentage.raw}
    # Other multi-measure layouts retain the exact column labels under value.
    result = {'value': {}, 'unit': {}, 'raw_value': {}}
    for col, cell in pairs:
        key = col.measure or col.header
        if key in result['value']:
            raise ValueError(f'Duplicate JSON measure {key!r}; inspect the source columns.')
        result['value'][key] = json_cell_value(cell)
        result['unit'][key] = cell.unit
        result['raw_value'][key] = cell.raw
    return result


def assign_page_chunk_indices(tables: list[Table]):
    """Count combined headings once per first source page, before --table filtering."""
    seen, counts = set(), Counter()
    for table in sorted(tables, key=lambda t: t.index):
        key = norm(table.title)
        if key in seen:
            continue
        seen.add(key)
        counts[table.page or 'unknown'] += 1
        if table.page_chunk_index is None:
            table.page_chunk_index = counts[table.page or 'unknown']


def build_tables_json(tables: list[Table]) -> tuple[dict, dict, int]:
    """Combine same-heading tables, preserving all cells and source provenance.

    Resolve row paths across the entire heading before writing any values. This
    also handles a row label colliding with another table's group name. Source
    table ordinals (not filing years/pages) disambiguate repeated row labels.
    """
    data, metadata = {}, {}
    assign_page_chunk_indices(tables)
    groups = defaultdict(list)
    for table in tables:
        groups[norm(table.title)].append(table)
    combined_paths = {}
    for key, group in groups.items():
        rows = [replace(row, row_id=f'table_{ordinal}_{row.row_id}')
                for ordinal, table in enumerate(group, start=1) for row in table.rows]
        # Preserve existing row suffixes for single-table headings.
        combined_paths[key] = json_row_paths(rows if len(group) > 1 else group[0].rows)
    source_metadata = defaultdict(list)
    ordinals = Counter()
    accounted = 0
    for table in tables:
        key = norm(table.title)
        group = groups[key]
        title = clean(group[0].title)
        ordinals[key] += 1
        paths = {row.row_id: combined_paths[key][
            f'table_{ordinals[key]}_{row.row_id}' if len(group) > 1 else row.row_id]
            for row in table.rows}
        periods = defaultdict(list)
        for col in table.columns:
            periods[col.period or '[No year/date stated]'].append(col)
        cells = {(c.row_id, c.column_key): c for c in table.cells}
        if len(cells) != len(table.cells):
            raise ValueError(f'{table.table_id}: duplicate source cell identity; no JSON exported.')
        consumed = set()
        period_data = data.setdefault(title, {})
        for period, columns in periods.items():
            sections = period_data.setdefault(period, {})
            for row in table.rows:
                pairs = []
                for col in columns:
                    key = (row.row_id, col.key)
                    if key not in cells or key in consumed:
                        raise ValueError(f'{table.table_id}: missing/duplicated cell {key}; no JSON exported.')
                    pairs.append((col, cells[key]))
                    consumed.add(key)
                parent = sections
                path = paths[row.row_id]
                if len(path) == 2:
                    parent = sections.setdefault(path[0], {})
                if path[-1] in parent:
                    raise ValueError(f'{table.table_id}: JSON path collision {path}; no JSON exported.')
                parent[path[-1]] = json_value_leaf(pairs)
            period_data[period] = sections
        if consumed != set(cells):
            raise ValueError(f'{table.table_id}: some source cells were not exported.')
        source_metadata[title].append({
            'table_id': table.table_id, 'source_title': table.title,
            'source_section': table.source_section,
            'introductory_sentence': table.introductory_sentence,
            'title_basis': table.title_basis,
            'item_table_index': table.index,
            'printed_page': table.page, 'source_url': table.source,
            'source_locator': table.locator, 'header_context': table.header_context,
            'unit_evidence': table.unit_evidence, 'footnotes': table.notes,
            'warnings': table.warnings,
            'rows': [{'row_id': r.row_id, 'section': paths[r.row_id][0],
                      'subsection': paths[r.row_id][1] if len(paths[r.row_id]) == 2 else None,
                      'source_label': r.label, 'source_group': r.group,
                      'inferred_label': r.inferred_label or None,
                      'label_inference': r.label_inference or None,
                      'physical_html_row': r.physical_row} for r in table.rows],
            'columns': [{'year_or_date': c.period, 'measure': c.measure,
                         'source_header': c.header, 'column_key': c.key} for c in table.columns],
            'source_cells': len(table.cells),
        })
        accounted += len(consumed)
    for title, sources in source_metadata.items():
        if len(sources) == 1:
            metadata[title] = sources[0]
        else:
            metadata[title] = {
                'source_title': title,
                'item_table_index': min(s['item_table_index'] for s in sources),
                'item_table_indices': [s['item_table_index'] for s in sources],
                'source_cells': sum(s['source_cells'] for s in sources),
                'source_tables': sources,
            }
        first_table = min(groups[norm(title)], key=lambda t: t.index)
        metadata[title]['page_chunk_index'] = first_table.page_chunk_index
    return data, metadata, accounted


def build_json_result(previous: list[Table], current: list[Table], metadata: dict,
                      diagnostics: dict | None = None) -> dict:
    """Keep filing years outside the table-period keys, so shared years survive."""
    result = {'schema_version': '1.2', 'generator_version': VERSION,
              'company': metadata.get('Company', ''), 'industry': metadata.get('Industry', ''),
              'split': metadata.get('Split', 'Development/Validation'),
              'filing_form': metadata.get('Filing Form', '10-K'), 'item': metadata.get('Item', '')}
    for side, tables in [('previous', previous), ('current', current)]:
        data, provenance, count = build_tables_json(tables)
        diag = (diagnostics or {}).get(side, {})
        filing = {'fiscal_year': metadata.get(side.title() + ' Fiscal Year'),
                  'source_url': metadata.get(side.title() + ' Source URL', ''),
                  'tables': data, 'table_metadata': provenance,
                  'extraction': {'table_count': len(tables), 'heading_count': len(data),
                                 'source_cells_exported': count,
                                 'source_sha256': diag.get('sha256', ''),
                                 'unsupported_tables': diag.get('unsupported_tables', [])}}
        if diag.get('sec_filing_selection'):
            filing['sec_filing_selection'] = diag['sec_filing_selection']
            filing['sec_issuer'] = diag.get('sec_issuer', {})
        result[side] = filing
    return result


def export_json_result(out: Path, previous, current, metadata, diagnostics=None):
    result = build_json_result(previous, current, metadata, diagnostics)
    payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if json.loads(payload) != result:
        raise ValueError('JSON serialization changed the extracted result; no file exported.')
    out.mkdir(parents=True, exist_ok=True)
    (out/'result.json').write_text(payload + '\n', encoding='utf-8')
    return result


def normalize_item(value: str) -> str:
    value = re.sub(r'^item\s*', '', value.strip(), flags=re.I).upper().rstrip('.')
    if not re.fullmatch(r'(?:[1-9]|1[0-6])[A-C]?', value):
        raise argparse.ArgumentTypeError('Choose ONE Item, e.g. 7, 8, 1A, or 7A; no comma-separated lists.')
    return value


def read_json(path: str):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--previous', help='Previous full filing .html/.htm path or direct filing URL')
    parser.add_argument('--current', help='Current full filing .html/.htm path or direct filing URL')
    parser.add_argument('--previous-year', type=int)
    parser.add_argument('--current-year', type=int)
    parser.add_argument('--company', help='Company label in exports; API mode defaults to the SEC issuer name')
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument('--ticker', help='Resolve both filings automatically through the SEC API, e.g. MU')
    identity.add_argument('--cik', help='Use the SEC API with a CIK instead of a ticker, e.g. 723125')
    parser.add_argument('--item', type=normalize_item, default='7')
    parser.add_argument('--filings-dir', default='.')
    parser.add_argument('--table', help='Optional table title or source section heading; ignores leading note numbers, case and whitespace. Otherwise all supported tables in the Item')
    parser.add_argument('--output-dir', help='Default: data/table_output/<company>_<previous>_<current>_item_<item>_tables')
    parser.add_argument('--output-format', choices=['json', 'csv', 'both'], default='json',
                        help='json (default): nested table data; csv: comparison annotations; both: all outputs')
    parser.add_argument('--previous-source-url', default='')
    parser.add_argument('--current-source-url', default='')
    parser.add_argument('--user-agent', default=os.environ.get('SEC_USER_AGENT', ''),
                        help='Your name and contact email; alternatively set SEC_USER_AGENT')
    parser.add_argument('--cache-dir', help='Optional API/HTML disk cache; API mode otherwise keeps source HTML in memory')
    parser.add_argument('--refresh-cache', action='store_true', help='Refresh the optional API-mode disk cache')
    parser.add_argument('--previous-accession', help='Explicit original 10-K accession for ambiguous API results')
    parser.add_argument('--current-accession', help='Explicit original 10-K accession for ambiguous API results')
    parser.add_argument('--mappings', help='CSV comparison only: JSON with table_pairs, row_pairs, and/or column_pairs')
    parser.add_argument('--columns', help='CSV comparison only: JSON array of column names or {key,label} objects')
    parser.add_argument('--include-unchanged', action='store_true')
    parser.add_argument('--ignore-number-formatting', action='store_true')
    parser.add_argument('--strict', action='store_true', help='Stop if a numerical table cannot be parsed')
    for side in ['previous', 'current']:
        parser.add_argument(f'--{side}-item-xpath', default='')
        parser.add_argument(f'--{side}-end-xpath', default='')
        parser.add_argument(f'--{side}-page-map', help='JSON mapping table XPath to printed page label')
    args = parser.parse_args(argv)
    interactive = argv is None and len(sys.argv) == 1
    if interactive:
        args.ticker = input('SEC ticker [MU]: ').strip().upper() or 'MU'
        company_default = 'Micron' if args.ticker == 'MU' else args.ticker
        args.company = input(f'Company label [{company_default}]: ').strip() or company_default
        args.previous_year = int(input('Previous fiscal year: ').strip())
        args.current_year = int(input('Current fiscal year: ').strip())
        args.item = normalize_item(input('One Item [7]: ').strip() or '7')
        if not args.user_agent:
            args.user_agent = input('SEC User-Agent (your name and contact email): ').strip()
        args.table = input('Table title [Enter for all tables in this Item]: ').strip() or None
    try:
        if args.output_format == 'json' and (args.mappings or args.columns or args.include_unchanged or args.ignore_number_formatting):
            raise ValueError('Comparison options require --output-format csv or both. Nested JSON always preserves every extracted value.')
        api_mode = bool(args.ticker or args.cik)
        if api_mode and (args.previous or args.current):
            raise ValueError('Use either --ticker/--cik for API discovery or --previous/--current for explicit inputs, not both.')
        if not api_mode and (args.previous_accession or args.current_accession):
            raise ValueError('Accession overrides require --ticker or --cik.')
        if api_mode and (args.previous_year is None or args.current_year is None):
            raise ValueError('SEC API mode requires --previous-year and --current-year.')
        if api_mode and args.previous_year >= args.current_year:
            raise ValueError('Previous fiscal year must be earlier than current fiscal year.')
        client = None
        issuer, selections = {}, {}
        if api_mode:
            client = SecClient(args.user_agent, Path(args.cache_dir) if args.cache_dir else None, args.refresh_cache)
            print('Looking up original 10-K filings through the SEC submissions API...')
            accession_overrides = {year: accession for year, accession in [
                (args.previous_year, args.previous_accession), (args.current_year, args.current_accession)] if accession}
            issuer, selections = discover_sec_filings(client, [args.previous_year, args.current_year],
                                                       args.ticker or '', args.cik or '', accession_overrides)
            args.company = args.company or issuer['name'] or args.ticker or args.cik
            for side in ['previous', 'current']:
                selected = selections[getattr(args, side+'_year')]
                override = getattr(args, side+'_source_url')
                if override and override != selected['url']:
                    raise ValueError(f'--{side}-source-url differs from the API-selected document. Remove this override.')
                setattr(args, side, selected['url'])
                setattr(args, side+'_source_url', selected['url'])
                print(f'{side.title()} FY{selected["requestedFiscalYear"]}: '
                      f'{selected["accessionNumber"]}; report date {selected.get("reportDate")}; '
                      f'filed {selected.get("filingDate")}.')
        args.company = args.company or 'Micron'
        # Filename years are inferred only if unambiguous. Explicit flags always win.
        for side in ['previous', 'current']:
            source = getattr(args, side)
            year = getattr(args, side+'_year')
            if year is None and source:
                matches = YEAR_RE.findall(Path(urlparse(source).path).name)
                if len(set(matches)) == 1:
                    year = int(matches[0])
                    setattr(args, side+'_year', year)
            if year is None:
                raise ValueError(f'Specify --{side}-year (the fiscal year, not the download year).')
            if not source:
                candidates = [Path(args.filings_dir)/f'{year}{suffix}' for suffix in ['.html', '.htm']]
                existing = [p for p in candidates if p.exists()]
                if len(existing) != 1:
                    raise ValueError(f'Expected one {year}.html or {year}.htm in {args.filings_dir}; use --{side} for another filename.')
                setattr(args, side, str(existing[0]))
        if args.previous_year >= args.current_year:
            raise ValueError('Previous fiscal year must be earlier than current fiscal year.')
        out = Path(args.output_dir) if args.output_dir else Path('data/table_output') / f'{slug(args.company)}_{args.previous_year}_{args.current_year}_item_{args.item.lower()}_tables'
        tables, diagnostics = {}, {}
        for side in ['previous', 'current']:
            source = getattr(args, side)
            data = client.get(source) if client else load_source(source, out/'source_cache', args.user_agent)
            source_url = getattr(args, side+'_source_url') or (source if source.startswith(('https://','http://')) else '')
            tables[side], diagnostics[side] = extract_tables(
                data, args.company, getattr(args, side+'_year'), args.item, source_url,
                getattr(args, side+'_item_xpath'), getattr(args, side+'_end_xpath'),
                read_json(getattr(args, side+'_page_map')))
            diagnostics[side]['input_file_or_url'] = source
            if api_mode:
                selection = selections[getattr(args, side+'_year')]
                diagnostics[side]['sec_filing_selection'] = selection
                diagnostics[side]['sec_issuer'] = issuer
                if selection['declaredFiscalYear'] is None:
                    for table in tables[side]:
                        table.warnings.append('Fiscal-year selection uses reportDate because the document has no DEI fiscal-year tag; verify the issuer year label.')
            if args.strict and diagnostics[side]['unsupported_tables']:
                out.mkdir(parents=True, exist_ok=True)
                (out/'extraction_failure.json').write_text(json.dumps(diagnostics[side], ensure_ascii=False, indent=2), encoding='utf-8')
                raise ValueError(f'{side}: unsupported numerical table(s); see extraction_failure.json. No comparison was exported.')
        for side_tables in tables.values():
            assign_page_chunk_indices(side_tables)
        if args.table:
            for side in tables:
                tables[side] = [t for t in tables[side]
                                if normalize_table_title(t.title) == normalize_table_title(args.table)
                                or normalize_table_title(t.source_section) == normalize_table_title(args.table)]
            if not tables['previous'] and not tables['current']:
                raise ValueError(f'Table title {args.table!r} was not found. Run without --table to inspect the available table titles.')
        meta = {'Company': args.company, 'Industry': '', 'Split': 'Development/Validation', 'Filing Form': '10-K',
                'Previous Fiscal Year': args.previous_year, 'Current Fiscal Year': args.current_year, 'Item': args.item,
                'Previous Source URL': args.previous_source_url or (args.previous if args.previous.startswith('http') else ''),
                'Current Source URL': args.current_source_url or (args.current if args.current.startswith('http') else '')}
        if args.output_format in {'json', 'both'}:
            export_json_result(out, tables['previous'], tables['current'], meta, diagnostics)
            count = sum(len(t.cells) for side in tables.values() for t in side)
            print(f'Nested JSON: result.json; {count} source values preserved across both filings.')
        if args.output_format in {'csv', 'both'}:
            records, audit, stats = compare_tables(tables['previous'], tables['current'], meta,
                                                  read_json(args.mappings), args.include_unchanged, args.ignore_number_formatting)
            export_results(out, tables['previous'], tables['current'], records, audit, stats, diagnostics,
                           read_json(args.columns) if args.columns else None)
            print(f'Annotations: {len(records)}. Matched cells: {stats.get("matched_cells",0)}; '
                  f'unchanged: {stats.get("matched_cells_unchanged",0)}.')
            print('Content Taxonomy and Materiality are blank for annotation; inspect Match Status before accepting labels.')
        if api_mode and args.output_format in {'csv', 'both'}:
            (out/'filing_selection.json').write_text(json.dumps({'issuer': issuer, 'filings': selections},
                                                               ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Tables: {len(tables["previous"])} previous; {len(tables["current"])} current.')
        print(f'Output: {out.resolve()}')
        skipped = sum(len(d['unsupported_tables']) for d in diagnostics.values())
        if skipped:
            target = 'result.json > previous/current > extraction > unsupported_tables' if args.output_format == 'json' else 'extraction_warnings.csv and audit.json'
            print(f'REVIEW: {skipped} numerical table(s) could not be parsed. See {target}.')
        if not tables['previous'] and not tables['current']:
            print('No supported numerical tables were found in this Item. This is not a prose comparator.')
        return 0
    except (ValueError, OSError, etree.LxmlError, json.JSONDecodeError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
