import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from export_table_annotations import main, make_annotations, table_chunk_ids
from table_titles import normalize_table_title


TITLE = 'Property, Plant, and Equipment'


def document(previous_title=TITLE, current_title='Note 8. ' + TITLE):
    return {
        'company': 'Example', 'item': '8',
        'previous': {'fiscal_year': 2024, 'tables': {previous_title: {'2024': {'Land': {'value': 10}}}},
                     'table_metadata': {previous_title: {'item_table_index': 7, 'printed_page': '62'}}},
        'current': {'fiscal_year': 2025, 'tables': {current_title: {'2025': {'Land': {'value': 20}}}},
                    'table_metadata': {current_title: {'item_table_index': 9, 'printed_page': '65'}}},
    }


class ExportTableAnnotationsTests(unittest.TestCase):
    def test_legacy_json_counts_per_page_in_source_order(self):
        filing = {'fiscal_year': 2025, 'tables': {'Last': {}, 'First': {}, 'Second': {}},
                  'table_metadata': {
                      'Last': {'item_table_index': 9, 'printed_page': '65'},
                      'First': {'item_table_index': 3, 'printed_page': '64'},
                      'Second': {'item_table_index': 7, 'printed_page': '64'}}}
        self.assertEqual(table_chunk_ids(filing, 'Micron'), {
            'First': 'table_micron_2025_64_01', 'Second': 'table_micron_2025_64_02',
            'Last': 'table_micron_2025_65_01'})

    def test_invalid_or_duplicate_page_counters_are_rejected(self):
        doc = document()
        title = next(iter(doc['current']['tables']))
        for invalid in (0, -1, '1', True):
            doc['current']['table_metadata'][title]['page_chunk_index'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'page_chunk_index'):
                table_chunk_ids(doc['current'], 'Example')
        doc['current']['table_metadata'][title]['page_chunk_index'] = 1
        doc['current']['tables']['Other'] = {'2025': {'Value': 1}}
        doc['current']['table_metadata']['Other'] = {
            'item_table_index': 10, 'printed_page': '65', 'page_chunk_index': 1}
        with self.assertRaisesRegex(ValueError, 'Duplicate page chunk ID'):
            table_chunk_ids(doc['current'], 'Example')

    def test_combined_heading_uses_page_of_first_item_position(self):
        doc = document()
        title = next(iter(doc['current']['tables']))
        doc['current']['table_metadata'][title] = {
            'item_table_index': 9,
            'source_tables': [
                {'item_table_index': 10, 'printed_page': '66'},
                {'item_table_index': 9, 'printed_page': '65'},
            ],
        }
        row, = make_annotations(doc)
        self.assertEqual(row['Current Table / Chunk ID'], 'table_example_2025_65_01')

    def test_company_page_labels_and_missing_metadata(self):
        doc = document()
        title = next(iter(doc['current']['tables']))
        doc['company'] = 'Micron Technology, Inc.'
        doc['current']['table_metadata'][title]['printed_page'] = 'F-12'
        row, = make_annotations(doc)
        self.assertEqual(row['Current Table / Chunk ID'], 'table_micron_technology_inc_2025_f-12_01')
        for page in ['', None]:
            with self.subTest(page=page):
                doc['current']['table_metadata'][title]['printed_page'] = page
                self.assertEqual(table_chunk_ids(doc['current'], 'NVIDIA')[title],
                                 'table_nvidia_2025_unknown_01')
        doc['current'].pop('table_metadata')
        self.assertEqual(table_chunk_ids(doc['current'], '')[title], 'table_company_2025_unknown_01')

    def test_duplicate_item_positions_are_rejected_even_on_different_pages(self):
        doc = document()
        doc['current']['tables']['Other'] = {'2025': {'Value': 1}}
        doc['current']['table_metadata']['Other'] = {'item_table_index': 9, 'printed_page': '70'}
        with self.assertRaisesRegex(ValueError, 'Duplicate table position 9'):
            make_annotations(doc)

    def test_note_prefix_variants_and_renumbering_match(self):
        for prefix in ['Note 8. ', 'NOTE 12: ', 'Note 8 – ', 'Note 9 — ',
                       'Note 8 - ', 'Note 8 ', 'Note 8.', 'Note 8) ']:
            with self.subTest(prefix=prefix):
                self.assertEqual(normalize_table_title(prefix + TITLE), TITLE.casefold())
                row, = make_annotations(document('Note 7. ' + TITLE, prefix + TITLE))
                self.assertTrue(row['Previous Disclosure JSON'])
                self.assertTrue(row['Current Disclosure JSON'])

    def test_paired_row_preserves_original_titles_values_and_chunk_ids(self):
        doc = document()
        row, = make_annotations(doc)
        self.assertEqual(row['Previous Section / Subsection'], TITLE)
        self.assertEqual(row['Current Section / Subsection'], 'Note 8. ' + TITLE)
        self.assertEqual(json.loads(row['Previous Disclosure JSON']), doc['previous']['tables'])
        self.assertEqual(json.loads(row['Current Disclosure JSON']), doc['current']['tables'])
        self.assertEqual(row['Previous Table / Chunk ID'], 'table_example_2024_62_01')
        self.assertEqual(row['Current Table / Chunk ID'], 'table_example_2025_65_01')

        self.assertEqual(make_annotations(doc, TITLE), [row])
        self.assertEqual(make_annotations(doc, 'NOTE 8. ' + TITLE.upper()), [row])

    def test_unrelated_titles_numbers_and_bare_notes_are_not_conflated(self):
        for title in ['Notes Payable', 'Note 8', 'Note 9', 'Note 8.', 'Debt due in 2025',
                      '8. Property, Plant, and Equipment', 'See Note 8. Debt']:
            with self.subTest(title=title):
                self.assertEqual(normalize_table_title(title), title.casefold())
        rows = make_annotations(document('Notes Payable', 'Note 8. Debt'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['Previous Disclosure JSON'], '')
        self.assertEqual(rows[1]['Current Disclosure JSON'], '')

    def test_added_and_removed_tables_keep_current_order(self):
        doc = document()
        doc['current']['tables']['New table'] = {'2025': {'Land': {'value': 30}}}
        doc['previous']['tables']['Removed table'] = {'2024': {'Land': {'value': 40}}}
        paired, added, removed = make_annotations(doc)
        self.assertEqual(paired['Current Section / Subsection'], 'Note 8. ' + TITLE)
        self.assertEqual(added['Current Section / Subsection'], 'New table')
        self.assertEqual(added['Previous Disclosure JSON'], '')
        self.assertEqual(removed['Previous Section / Subsection'], 'Removed table')
        self.assertEqual(removed['Current Disclosure JSON'], '')

    def test_ambiguous_normalized_titles_are_rejected(self):
        doc = document()
        doc['current']['tables']['Note 9. ' + TITLE] = {'2025': {'Land': {'value': 99}}}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            make_annotations(doc)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            make_annotations(doc, TITLE)

    def test_existing_json_can_be_reexported_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'result.json', Path(folder) / 'out'
            payload = json.dumps(document())
            source.write_text(payload)
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--input', str(source), '--output-dir', str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(source.read_text(), payload)
            with (output / 'table_annotations.tsv').open() as stream:
                rows = list(csv.DictReader(stream, delimiter='\t'))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['Current Section / Subsection'], 'Note 8. ' + TITLE)

    def test_html_table_filter_keeps_both_note_title_variants(self):
        template = '''<html><body><h1>Item 8. Financial Statements</h1>
        <h2>{title}</h2><table>
        <tr><th></th><th>2025</th><th>2024</th></tr>
        <tr><td>Land</td><td>20</td><td>10</td></tr></table>
        <h2>Unrelated heading</h2><table>
        <tr><th></th><th>2025</th><th>2024</th></tr>
        <tr><td>Other</td><td>99</td><td>88</td></tr></table>
        <h1>Item 9. Other Matters</h1></body></html>'''
        with tempfile.TemporaryDirectory() as folder:
            previous, current, output = [Path(folder) / name for name in ('old.html', 'new.html', 'out')]
            previous.write_text(template.format(title=TITLE))
            current.write_text(template.format(title='Note 8. ' + TITLE))
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(['--previous', str(previous), '--current', str(current),
                             '--previous-year', '2024', '--current-year', '2025',
                             '--company', 'Example', '--item', '8', '--table', TITLE,
                             '--output-dir', str(output)])
            self.assertEqual(code, 0)
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(list(result['previous']['tables']), [TITLE])
            self.assertEqual(list(result['current']['tables']), ['Note 8. ' + TITLE])
            row, = make_annotations(result)
            self.assertTrue(row['Previous Disclosure JSON'])
            self.assertTrue(row['Current Disclosure JSON'])


if __name__ == '__main__':
    unittest.main()
