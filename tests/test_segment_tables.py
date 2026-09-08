import json
import unittest
from itertools import groupby
from pathlib import Path

from lxml import etree, html

from compare_html_tables import (Table, build_json_result, build_tables_json, expand_grid,
                                 extract_tables, numeric, parse_table)
from export_table_annotations import make_annotations


FIXTURE = json.loads((Path(__file__).parent/'fixtures/micron_note27_grids.json').read_text())
TITLE = 'Note 27. Segment and Other Information'


def table_from_grid(grid):
    """Reconstruct equivalent colspans within each three-column accounting lane.

    The fixture is the real saved grid, not the original SEC markup. Do not join
    identical values across business-unit boundaries (e.g. adjacent dashes).
    """
    table = html.Element('table')
    for row in grid:
        tr = etree.SubElement(table, 'tr')
        for start in range(0, len(row), 3):
            for value, repeats in groupby(row[start:start+3]):
                cell = etree.SubElement(tr, 'td', colspan=str(len(list(repeats))))
                cell.text = value
    return table


def parsed_table(fixture, index):
    node = table_from_grid(fixture['raw_grid'])
    columns, rows, cells, warnings, context = parse_table(node, TITLE, 'millions', '(In millions)')
    table = Table(fixture['table_id'], TITLE, '95' if index < 4 else '96', fixture['locator'],
                  FIXTURE['source_url'], 2025, '8', index, columns, rows, cells,
                  warnings=warnings, header_context=context)
    return node, table


class SegmentTableTests(unittest.TestCase):
    def test_actual_segment_grids_preserve_all_units_years_and_values(self):
        for index, fixture in enumerate(FIXTURE['tables'][:3], start=1):
            with self.subTest(source=fixture['table_id']):
                node, table = parsed_table(fixture, index)
                expanded, _ = expand_grid(node)
                self.assertEqual([[c.raw for c in row] for row in expanded], fixture['raw_grid'])
                self.assertEqual(len(table.cells), 56)
                self.assertEqual([c.measure for c in table.columns],
                                 ['CMBU', 'CDBU', 'MCBU', 'AEBU', 'All Other', 'Unallocated', 'Total'])
                year = str(2026-index)
                self.assertEqual({c.period for c in table.columns}, {year})
                for cell in table.cells:
                    row = next(r for r in table.rows if r.row_id == cell.row_id)
                    column = next(c for c in table.columns if c.key == cell.column_key)
                    offset = 3 + 3 * table.columns.index(column)
                    lane = fixture['raw_grid'][row.physical_row-1][offset:offset+3]
                    expected = next(value for value in lane if numeric(value)[0] in {'number', 'dash'})
                    self.assertEqual((cell.kind, cell.number), numeric(expected))
                    self.assertEqual(cell.unit, 'USD millions')
                data, _, _ = build_tables_json([table])
                self.assertEqual(data[TITLE][year]['Revenue']['value']['Total'],
                                 {1: 37378, 2: 25111, 3: 15540}[index])

    def test_unallocated_grid_handles_amounts_spanning_spacer_columns(self):
        fixture = FIXTURE['tables'][3]
        _, table = parsed_table(fixture, 4)
        self.assertEqual(len(table.columns), 3)
        self.assertEqual([c.period for c in table.columns], ['2025', '2024', '2023'])
        self.assertEqual(len(table.cells), 51)
        for cell in table.cells:
            row = next(r for r in table.rows if r.row_id == cell.row_id)
            column = next(c for c in table.columns if c.key == cell.column_key)
            offset = 3 + 3 * table.columns.index(column)
            lane = fixture['raw_grid'][row.physical_row-1][offset:offset+3]
            expected = next(value for value in lane if numeric(value)[0] in {'number', 'dash'})
            self.assertEqual((cell.kind, cell.number), numeric(expected))
        data, _, count = build_tables_json([table])
        self.assertEqual(count, 51)
        selling = data[TITLE]['2025']['Selling, general, and administrative:']
        self.assertEqual(selling['Stock-based compensation']['value'], 219)

    def test_whole_note_exports_all_five_tables_separately(self):
        root = html.fromstring('<html><body><h1>Item 8. Financial Statements</h1>'
                               '<h2>' + TITLE + '</h2><p>All amounts in millions.</p></body></html>')
        body = root.find('body')
        for fixture in FIXTURE['tables']:
            body.append(table_from_grid(fixture['raw_grid']))
        description = etree.SubElement(body, 'p')
        description.text = 'Depreciation and amortization expense included in operating income (loss) was as follows:'
        depreciation = html.fromstring('''<table><tr><th>For the year ended</th><th>2025</th><th>2024</th><th>2023</th></tr>
        <tr><td>CMBU</td><td>$2,260</td><td>$1,112</td><td>$909</td></tr>
        <tr><td>CDBU</td><td>1,530</td><td>1,434</td><td>1,020</td></tr>
        <tr><td>MCBU</td><td>3,177</td><td>3,762</td><td>4,319</td></tr>
        <tr><td>AEBU</td><td>1,375</td><td>1,447</td><td>1,486</td></tr>
        <tr><td>All Other</td><td>5</td><td>7</td><td>3</td></tr>
        <tr><td>Unallocated</td><td>5</td><td>18</td><td>19</td></tr>
        <tr><td></td><td>8,352</td><td>7,780</td><td>7,756</td></tr></table>''')
        body.append(depreciation)
        etree.SubElement(body, 'h1').text = 'Item 9. Other Matters'
        tables, diagnostics = extract_tables(etree.tostring(root), 'Micron', 2025, '8')
        self.assertEqual(diagnostics['unsupported_tables'], [])
        self.assertEqual(len(tables), 5)
        document = build_json_result([], tables, {'Company': 'Micron', 'Item': '8',
                                     'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025})
        self.assertEqual(len(document['current']['tables']), 5)
        self.assertEqual({period for table in document['current']['tables'].values() for period in table},
                         {'2025', '2024', '2023'})
        self.assertEqual(document['current']['extraction']['source_cells_exported'], 240)
        rows = make_annotations(document)
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row['Current Disclosure JSON'] for row in rows))
        self.assertEqual(rows[-1]['Current Section / Subsection'], description.text)
        self.assertEqual(len(make_annotations(document, TITLE)), 5)

    def test_shared_year_amount_and_percentage_columns_stay_separate(self):
        node = html.fromstring('''<table><tr><th></th><th colspan="4">2025</th></tr>
            <tr><td>Revenue</td><td>$</td><td>500</td><td>10</td><td>%</td></tr>
            <tr><td>Income</td><td>$</td><td>50</td><td>1</td><td>%</td></tr></table>''')
        columns, rows, cells, _, _ = parse_table(node, 'Results', 'millions', '')
        self.assertEqual([c.measure for c in columns], ['Amount', 'Percentage'])
        self.assertEqual(len(cells), 4)


if __name__ == '__main__':
    unittest.main()
