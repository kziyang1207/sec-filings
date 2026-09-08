import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from export_table_annotations import main, read_result


TABLE = '''<table><tr><th></th><th>2025</th><th>2024</th></tr>
<tr><td>Amount</td><td>20</td><td>10</td></tr></table>'''


class MultiItemExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previous, self.current = self.root/'2024.html', self.root/'2025.html'
        self.output = self.root/'out'
        self.filing = (
            '<html><body><h1>Item 7. Management Discussion</h1>'
            '<h2>Shared Heading</h2>' + TABLE +
            '<h1>Item 7A. Market Risk</h1><h2>Excluded</h2>' + TABLE +
            '<h1>Item 8. Financial Statements</h1><h2>Shared Heading</h2>' + TABLE +
            '<h2>Note 8. Equipment</h2>' + TABLE +
            '<h1>Item 9. Other Matters</h1><div style="bottom:0">41</div></body></html>')
        self.previous.write_text(self.filing.replace('Note 8. Equipment', 'Equipment'))
        self.current.write_text(self.filing)
        self.args = ['--previous', str(self.previous), '--current', str(self.current),
                     '--previous-year', '2024', '--current-year', '2025',
                     '--company', 'Example', '--output-dir', str(self.output)]

    def run_export(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(self.args + list(extra))

    def rows(self, path=None):
        with ((path or self.output)/'table_annotations.tsv').open() as stream:
            return list(csv.DictReader(stream, delimiter='\t'))

    def test_default_exports_all_tables_from_items_7_and_8_only(self):
        self.assertEqual(self.run_export(), 0)
        result = read_result(self.output/'result.json')
        self.assertEqual(list(result['items']), ['7', '8'])
        self.assertEqual(list(result['items']['7']['current']['tables']), ['Shared Heading'])
        self.assertEqual(list(result['items']['8']['current']['tables']), ['Shared Heading', 'Note 8. Equipment'])
        rows = self.rows()
        self.assertEqual([row['Item'] for row in rows], ['7', '8', '8'])
        self.assertEqual([row['Current Table / Chunk ID'] for row in rows],
                         ['table_example_2025_41_01', 'table_example_2025_41_02', 'table_example_2025_41_03'])
        self.assertTrue(all(row['Previous Disclosure JSON'] and row['Current Disclosure JSON'] for row in rows))
        self.assertIn('Items 7, 8', (self.output/'paste_into_sheets.html').read_text())

    def test_item_override_retains_single_item_json_format(self):
        for item in ('7', '8'):
            with self.subTest(item=item):
                self.assertEqual(self.run_export(['--item', item]), 0)
                result = read_result(self.output/'result.json')
                self.assertNotIn('items', result)
                self.assertEqual(result['item'], item)
                self.assertEqual({row['Item'] for row in self.rows()}, {item})

    def test_table_override_searches_both_items_and_preserves_page_index(self):
        self.assertEqual(self.run_export(['--table', 'Equipment']), 0)
        row, = self.rows()
        self.assertEqual(row['Item'], '8')
        self.assertEqual(row['Current Table / Chunk ID'], 'table_example_2025_41_03')
        result = read_result(self.output/'result.json')
        self.assertEqual(result['items']['7']['current']['tables'], {})
        self.assertEqual(list(result['items']['8']['current']['tables']), ['Note 8. Equipment'])

    def test_selected_table_may_exist_in_only_one_year(self):
        self.previous.write_text(self.filing.replace('<h2>Note 8. Equipment</h2>' + TABLE, ''))
        self.assertEqual(self.run_export(['--table', 'Equipment']), 0)
        row, = self.rows()
        self.assertEqual(row['Previous Disclosure JSON'], '')
        self.assertTrue(row['Current Disclosure JSON'])

    def test_bundle_can_be_reexported_or_filtered_without_fetching(self):
        self.assertEqual(self.run_export(), 0)
        source = self.output/'result.json'
        original = source.read_bytes()
        for item in (None, '8'):
            output = self.root/('copy' + str(item))
            args = ['--input', str(source), '--output-dir', str(output)]
            if item:
                args += ['--item', item]
            with self.subTest(item=item), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
                expected = [row for row in self.rows() if item is None or row['Item'] == item]
                self.assertEqual(self.rows(output), expected)
                self.assertEqual(source.read_bytes(), original)

    def test_failed_second_item_keeps_previous_successful_output(self):
        self.assertEqual(self.run_export(), 0)
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        self.current.write_text(self.filing.replace('Item 8. Financial Statements', 'Other Section'))
        self.assertEqual(self.run_export(), 2)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_no_matching_table_is_an_error(self):
        self.assertEqual(self.run_export(['--table', 'Does not exist']), 2)
        self.assertFalse(self.output.exists())

    def test_empty_item_does_not_prevent_other_item_export(self):
        without_item7_tables = self.filing.replace('<h2>Shared Heading</h2>' + TABLE, '', 1)
        self.current.write_text(without_item7_tables)
        self.previous.write_text(without_item7_tables)
        self.assertEqual(self.run_export(), 0)
        self.assertEqual({row['Item'] for row in self.rows()}, {'8'})

    def test_malformed_combined_json_is_rejected(self):
        for items in ({}, [], {'7': {'item': '8'}}):
            with self.subTest(items=items):
                path = self.root/'bad.json'
                path.write_text(json.dumps({'items': items}))
                with self.assertRaises(ValueError):
                    read_result(path)


if __name__ == '__main__':
    unittest.main()
