import csv
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from compare_html_tables import (Cell, Column, Row, Table, assign_page_chunk_indices,
                                 build_json_result, build_tables_json)
from export_table_annotations import export_annotations, main, make_annotations


TITLE = 'Liquidity and Capital Resources'


def source_table(year, index, labels, values, *, title=TITLE, period=None, groups=None):
    period = period or f'Jan 26, {year}'
    groups = groups or [''] * len(labels)
    rows = [Row(f'r{i}', label, label, group, i)
            for i, (label, group) in enumerate(zip(labels, groups), start=1)]
    return Table(
        table_id=f'nvidia_{year}_43_t{index}', title=title, page='43',
        locator=f'/html/body/table[{index}]', source='fixture.html', year=year,
        item='7', index=index,
        columns=[Column('c1', period, period, 'Amount', 1, '')], rows=rows,
        cells=[Cell(row.row_id, 'c1', str(value), 'number', str(value),
                    'USD millions', f'table[{index}]/row[{i}]')
               for i, (row, value) in enumerate(zip(rows, values), start=1)],
    )


class TableJsonTests(unittest.TestCase):
    def test_page_counter_resets_and_survives_table_filtering(self):
        tables = [source_table(2025, i, ['Value'], [i], title=title)
                  for i, title in enumerate(['Cash', 'Cash', 'Debt', 'Leases'], start=1)]
        tables[-1].page = '44'
        assign_page_chunk_indices(tables)
        meta = {'Company': 'NVIDIA', 'Item': '8',
                'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025}
        full = build_json_result([], tables, meta)
        rows = make_annotations(full)
        self.assertEqual([r['Current Table / Chunk ID'] for r in rows],
                         ['table_nvidia_2025_43_01', 'table_nvidia_2025_43_02',
                          'table_nvidia_2025_44_01'])
        filtered = build_json_result([], [tables[2]], meta)
        row, = make_annotations(filtered)
        self.assertEqual(row['Current Table / Chunk ID'], 'table_nvidia_2025_43_02')
        self.assertEqual(full['current']['table_metadata']['Debt']['page_chunk_index'], 2)

    def test_html_to_annotation_with_two_tables_under_one_heading(self):
        filing = '''<html><body>
        <h1>Item 7. Management's Discussion and Analysis</h1>
        <h2>Liquidity and Capital Resources</h2>
        <table>
          <tr><th></th><th>Jan 26, 2025</th><th>Jan 28, 2024</th></tr>
          <tr><td colspan="3">(In millions)</td></tr>
          <tr><td>Cash and cash equivalents</td><td>8,589</td><td>7,280</td></tr>
          <tr><td>Marketable securities</td><td>34,621</td><td>18,704</td></tr>
          <tr><td>Cash, cash equivalents, and marketable securities</td><td>43,210</td><td>25,984</td></tr>
        </table>
        <table>
          <tr><th></th><th colspan="2">Year Ended</th></tr>
          <tr><th></th><th>Jan 26, 2025</th><th>Jan 28, 2024</th></tr>
          <tr><td colspan="3">(In millions)</td></tr>
          <tr><td>Net cash provided by operating activities</td><td>64,089</td><td>28,090</td></tr>
          <tr><td>Net cash used in investing activities</td><td>(20,421)</td><td>(10,566)</td></tr>
          <tr><td>Net cash used in financing activities</td><td>(42,359)</td><td>(13,633)</td></tr>
        </table>
        <h1>Item 8. Financial Statements</h1>
        </body></html>'''
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current, previous, output = root / '2025.html', root / '2024.html', root / 'out'
            current.write_text(filing, encoding='utf-8')
            previous.write_text(filing.replace('2024', '2023').replace('2025', '2024'), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--previous', str(previous), '--current', str(current),
                             '--previous-year', '2024', '--current-year', '2025',
                             '--company', 'NVIDIA', '--item', '7', '--table', TITLE,
                             '--output-dir', str(output)])
            self.assertEqual(code, 0)
            document = json.loads((output / 'result.json').read_text())
            for side in ['previous', 'current']:
                self.assertEqual(list(document[side]['tables']), [TITLE+' [Table 1]', TITLE+' [Table 2]'])
                self.assertEqual(document[side]['extraction']['source_cells_exported'], 12)
                self.assertEqual(len(document[side]['table_metadata']), 2)
            date = document['current']['tables'][TITLE+' [Table 1]']['Jan 26, 2025']
            flows = document['current']['tables'][TITLE+' [Table 2]']['Jan 26, 2025']
            self.assertEqual(len(date), 3)
            self.assertEqual(len(flows), 3)
            self.assertEqual(date['Cash and cash equivalents']['value'], 8589)
            self.assertEqual(flows['Net cash used in investing activities']['value'], -20421)
            with (output / 'table_annotations.tsv').open() as stream:
                records = list(csv.DictReader(stream, delimiter='\t'))
            self.assertEqual(len(records), 2)
            for record in records:
                self.assertTrue(record['Previous Disclosure JSON'])
                self.assertTrue(record['Current Disclosure JSON'])

    def test_combines_same_heading_by_date_and_preserves_all_sources(self):
        cash = source_table(2025, 7, ['Cash and cash equivalents', 'Marketable securities'],
                            [8589, 34621])
        flows = source_table(2025, 8, ['Net cash provided by operating activities'],
                             [64089], title='  Liquidity  and CAPITAL Resources ')
        flows.columns[0].header = 'Year Ended Jan 26, 2025'
        older = source_table(2025, 9, ['Cash and cash equivalents'], [7280],
                             period='Jan 28, 2024')
        # Different row labels are merged directly under each shared date.
        data, metadata, count = build_tables_json([cash, flows])
        self.assertEqual(list(data), [TITLE])
        self.assertEqual(len(data[TITLE]['Jan 26, 2025']), 3)
        self.assertEqual(data[TITLE]['Jan 26, 2025']['Cash and cash equivalents']['value'], 8589)
        self.assertEqual(data[TITLE]['Jan 26, 2025']['Net cash provided by operating activities']['value'], 64089)
        self.assertEqual(count, 3)
        self.assertEqual(metadata[TITLE]['source_cells'], count)
        self.assertEqual(metadata[TITLE]['item_table_indices'], [7, 8])
        sources = metadata[TITLE]['source_tables']
        self.assertEqual([s['table_id'] for s in sources], [cash.table_id, flows.table_id])
        self.assertEqual(sources[1]['columns'][0]['source_header'], 'Year Ended Jan 26, 2025')
        self.assertEqual(sources[1]['source_locator'], flows.locator)
        data, _, count = build_tables_json([cash, flows, older])
        self.assertEqual(set(data[TITLE]), {'Jan 26, 2025', 'Jan 28, 2024'})
        self.assertEqual(count, 4)

    def test_overlapping_labels_preserve_even_equal_values_and_reserve_real_names(self):
        tables = [source_table(2025, 1, ['Total'], [10]),
                  source_table(2025, 2, ['Total', 'Total [table_1_r1]'], [10, 20])]
        data, metadata, count = build_tables_json(tables)
        sections = data[TITLE]['Jan 26, 2025']
        self.assertEqual(len(sections), 3)
        self.assertEqual(sorted(leaf['value'] for leaf in sections.values()), [10, 10, 20])
        self.assertEqual(sections['Total [table_1_r1]']['value'], 20)
        self.assertEqual(count, 3)
        for table, source in zip(tables, metadata[TITLE]['source_tables']):
            for cell, row in zip(table.cells, source['rows']):
                self.assertEqual(sections[row['section']]['raw_value'], cell.raw)

    def test_group_and_row_label_collisions_do_not_overwrite(self):
        tables = [source_table(2025, 1, ['Cash', 'Assets'], [1, 2], groups=['Assets', '']),
                  source_table(2025, 2, ['Cash', 'Debt'], [3, 4], groups=['Assets', 'Assets'])]
        data, metadata, count = build_tables_json(tables)
        sections = data[TITLE]['Jan 26, 2025']
        self.assertEqual(len(sections['Assets']), 3)
        self.assertEqual(count, 4)
        for table, source in zip(tables, metadata[TITLE]['source_tables']):
            for cell, row in zip(table.cells, source['rows']):
                leaf = sections[row['section']]
                if row['subsection'] is not None:
                    leaf = leaf[row['subsection']]
                self.assertEqual(leaf['raw_value'], cell.raw)

    def test_single_table_and_distinct_headings_remain_separate(self):
        cash = source_table(2025, 7, ['Cash'], [8589])
        other = source_table(2025, 8, ['Revenue'], [100], title='Results')
        data, metadata, count = build_tables_json([cash, other])
        self.assertEqual(list(data), [TITLE, 'Results'])
        self.assertEqual(data[TITLE], {'Jan 26, 2025': {
            'Cash': {'value': 8589, 'raw_value': '8589', 'unit': 'USD millions'}}})
        self.assertEqual(metadata[TITLE]['table_id'], cash.table_id)
        self.assertNotIn('source_tables', metadata[TITLE])
        self.assertEqual(count, 2)

    def test_combined_heading_exports_one_paired_annotation(self):
        previous = [source_table(2024, 6, ['Cash'], [7280]),
                    source_table(2024, 7, ['Operating cash flow'], [28090])]
        current = [source_table(2025, 7, ['Cash'], [8589]),
                   source_table(2025, 8, ['Operating cash flow'], [64089])]
        meta = {'Company': 'NVIDIA', 'Item': '7',
                'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025}
        document = build_json_result(previous, current, meta)
        self.assertEqual(document['current']['extraction']['table_count'], 2)
        self.assertEqual(document['current']['extraction']['heading_count'], 1)
        records = make_annotations(document)
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual(row['Previous Table / Chunk ID'], 'table_nvidia_2024_43_01')
        self.assertEqual(row['Current Table / Chunk ID'], 'table_nvidia_2025_43_01')
        self.assertEqual(make_annotations(document, TITLE), records)
        for side in ['Previous', 'Current']:
            disclosure = json.loads(row[f'{side} Disclosure JSON'])
            self.assertEqual(list(disclosure), [TITLE])
            self.assertEqual(len(next(iter(disclosure[TITLE].values()))), 2)
        with tempfile.TemporaryDirectory() as folder:
            export_annotations(records, folder)
            with (Path(folder) / 'table_annotations.tsv').open() as stream:
                exported = list(csv.DictReader(stream, delimiter='\t'))
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]['Current Disclosure JSON'], row['Current Disclosure JSON'])

    def test_heading_can_be_one_table_in_one_year_and_multiple_in_the_other(self):
        previous = [source_table(2024, 6, ['Cash', 'Operating cash flow'], [7280, 28090])]
        current = [source_table(2025, 7, ['Cash'], [8589]),
                   source_table(2025, 8, ['Operating cash flow'], [64089])]
        document = build_json_result(previous, current, {
            'Item': '7', 'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025})
        records = make_annotations(document)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['Previous Disclosure JSON'])
        self.assertTrue(records[0]['Current Disclosure JSON'])


if __name__ == '__main__':
    unittest.main()
