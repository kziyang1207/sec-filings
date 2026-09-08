import unittest

from compare_html_tables import build_json_result, build_tables_json, extract_tables
from export_table_annotations import make_annotations


TABLE = '''<div><table>{caption}
<tr><th></th><th><b>2025</b></th><th><b>2024</b></th></tr>
<tr><td>Cash</td><td>12</td><td>10</td></tr>
<tr><td>Cash flow</td><td>11</td><td>13</td></tr>
</table></div>'''


def extract(body):
    source = ('<html><body><h1>Item 7. Management Discussion</h1>' + body
              + '<h1>Item 8. Financial Statements</h1></body></html>')
    tables, diagnostics = extract_tables(source.encode(), 'NVIDIA', 2025, '7')
    assert not diagnostics['unsupported_tables'], diagnostics
    return tables


class TableTitleTests(unittest.TestCase):
    def test_income_tax_tables_use_their_own_last_sentences(self):
        provision = 'Our income tax (provision) benefit consisted of the following:'
        reconciliation = ('The table below reconciles our tax (provision) benefit based on the U.S. '
                          'federal statutory rate to our effective rate:')
        body = ('<h2>Note 25. Income Taxes</h2><p>Background discussion. ' + provision + '</p>'
                + TABLE.format(caption='') + '<p>' + reconciliation + '</p>'
                + TABLE.format(caption=''))
        current = extract(body)
        previous = extract(body.replace('Note 25. Income Taxes', 'Income Taxes'))
        self.assertEqual([t.title for t in current], [provision, reconciliation])
        document = build_json_result(previous, current, {
            'Company': 'Micron', 'Item': '7', 'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025})
        rows = make_annotations(document)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['Current Section / Subsection'] for row in rows], [provision, reconciliation])
        self.assertTrue(all(row['Previous Disclosure JSON'] and row['Current Disclosure JSON'] for row in rows))
        self.assertNotEqual(rows[0]['Current Table / Chunk ID'], rows[1]['Current Table / Chunk ID'])
        self.assertEqual(make_annotations(document, 'Income Taxes'), rows)
        self.assertEqual(make_annotations(document, reconciliation), [rows[1]])
        for title in (provision, reconciliation):
            metadata = document['current']['table_metadata'][title]
            self.assertEqual(metadata['source_section'], 'Note 25. Income Taxes')
            self.assertEqual(metadata['introductory_sentence'], title)
            self.assertEqual(metadata['title_basis'], 'preceding_sentence')

    def test_single_income_tax_table_keeps_section_heading(self):
        body = ('<h2>Note 25. Income Taxes</h2>'
                '<p>Our income tax (provision) benefit consisted of the following:</p>'
                + TABLE.format(caption=''))
        table, = extract(body)
        self.assertEqual(table.title, 'Note 25. Income Taxes')
        self.assertEqual(table.title_basis, 'section_heading')

    def test_repeated_intro_sentences_never_merge_physical_tables(self):
        body = ('<h2>Income Taxes</h2>'
                + ('<p>The amounts were as follows:</p>' + TABLE.format(caption='')) * 2)
        tables = extract(body)
        self.assertEqual([t.title for t in tables],
                         ['The amounts were as follows: [Table 1]', 'The amounts were as follows: [Table 2]'])
        data, _, count = build_tables_json(tables)
        self.assertEqual(len(data), 2)
        self.assertEqual(count, 8)

    def test_layout_table_does_not_turn_single_table_section_into_multi_table_section(self):
        body = ('<h2>Income Taxes</h2><table><tr><td>Navigation</td><td>Contents</td></tr></table>'
                '<p>Amounts are presented below:</p>' + TABLE.format(caption=''))
        table, = extract(body)
        self.assertEqual(table.title, 'Income Taxes')

    def test_income_taxes_inline_heading_sharing_wrapper_with_table(self):
        interest = ('<p><b><i>Interest Income (Expense), Net:</i></b> '
                    'Interest income (expense) deteriorated for 2024 as compared to 2023 '
                    'primarily due to increases in interest expense.</p>')
        taxes = '''<div><span style="font-style:italic;font-weight:700">Income Taxes:</span>
        Our income tax (provision) benefit consisted of the following:
        <table><tr><th>For the year ended</th><th>2025</th><th>2024</th><th>2023</th></tr>
        <tr><td>Income (loss) before taxes</td><td>9,654</td><td>1,240</td><td>(5,658)</td></tr>
        <tr><td>Income tax (provision) benefit</td><td>(1,124)</td><td>(451)</td><td>(177)</td></tr>
        <tr><td>Effective tax rate</td><td>11.6%</td><td>36.4%</td><td>(3.1)%</td></tr></table>
        The change in our effective tax rate was primarily due to profitability.</div>'''
        tables = extract(interest + taxes)
        table, = tables
        self.assertEqual(table.title, 'Income Taxes')
        self.assertEqual(table.locator, '/html/body/div/table')
        data, _, count = build_tables_json(tables)
        self.assertEqual(list(data), ['Income Taxes'])
        self.assertEqual(count, 9)
        self.assertEqual(data['Income Taxes']['2025']['Income (loss) before taxes']['value'], 9654)
        self.assertEqual(data['Income Taxes']['2024']['Effective tax rate']['value'], 36.4)
        document = build_json_result(tables, tables, {
            'Item': '7', 'Previous Fiscal Year': 2024, 'Current Fiscal Year': 2025})
        record, = make_annotations(document)
        self.assertEqual(record['Current Section / Subsection'], 'Income Taxes')

    def test_inline_colon_headings_with_nested_styles_and_external_separator(self):
        for label in ['<b><i>Income Taxes:</i></b>', '<strong>Income Taxes</strong>:',
                      '<span style="font-weight:700">Income Taxes</span>:']:
            with self.subTest(label=label):
                body = ('<h2>Results of Operations</h2><p>' + label
                        + ' Our income tax benefit consisted of the following:</p>'
                        + TABLE.format(caption=''))
                self.assertEqual(extract(body)[0].title, 'Income Taxes')

    def test_table_following_prose_cannot_become_its_heading(self):
        body = ('<div>Actual introduction for the first table:' + TABLE.format(caption='')
                + '<b>Later Topic:</b> This follows the table.</div>')
        self.assertEqual(extract(body)[0].title, 'Actual introduction for the first table:')

    def test_two_inline_headings_in_one_wrapper_are_kept_in_source_order(self):
        inner_table = TABLE.format(caption='').removeprefix('<div>').removesuffix('</div>')
        body = ('<div><b>Income Taxes:</b> Our tax figures follow:' + inner_table
                + '<b>Cash Flows:</b> Our cash flows follow:' + inner_table + '</div>')
        tables = extract(body)
        self.assertEqual([t.title for t in tables], ['Income Taxes', 'Cash Flows'])
        self.assertEqual([t.locator for t in tables], ['/html/body/div/table[1]', '/html/body/div/table[2]'])

    def test_existing_headings_win_over_introductory_sentences(self):
        for title in ['Results of Operations', 'Operating Expenses',
                      'Liquidity and Capital Resources']:
            with self.subTest(title=title):
                body = (f'<h2>{title}</h2><p>Our business changed this year. '
                        'The following table summarizes the results:</p>' + TABLE.format(caption=''))
                table, = extract(body)
                self.assertEqual(table.title, title)

    def test_heading_persists_beyond_eight_blocks(self):
        body = ('<div><span style="font-weight:700">Operating Expenses</span></div>'
                + '<p>Additional explanatory information.</p>' * 12
                + TABLE.format(caption=''))
        self.assertEqual(extract(body)[0].title, 'Operating Expenses')

    def test_multi_table_sections_use_distinct_introductory_sentences(self):
        body = ('<h2>Liquidity and Capital Resources</h2>'
                '<p>Cash balances were as follows:</p>' + TABLE.format(caption='')
                + '<p>Our cash flows for the year were as follows:</p>' + TABLE.format(caption=''))
        tables = extract(body)
        titles = ['Cash balances were as follows:', 'Our cash flows for the year were as follows:']
        self.assertEqual([t.title for t in tables], titles)
        data, metadata, count = build_tables_json(tables)
        self.assertEqual(list(data), titles)
        self.assertEqual([metadata[t]['source_section'] for t in titles], ['Liquidity and Capital Resources'] * 2)
        self.assertEqual(count, 8)

    def test_formatted_direct_customers_label_introduces_specific_table(self):
        intro = ('Direct Customers – Sales to direct customers which represented 10% or more '
                 'of total revenue, all of which were primarily attributable to the Compute & '
                 'Networking segment, are presented in the following table:')
        body = ('<h2>Operating Income by Reportable Segments</h2>' + TABLE.format(caption='')
                + '<p><i>Direct Customers</i>' + intro[len('Direct Customers'):] + '</p>'
                + TABLE.format(caption=''))
        first, second = extract(body)
        self.assertEqual(first.title, 'Operating Income by Reportable Segments [Table 1]')
        self.assertEqual(second.title, intro)

    def test_caption_overrides_heading_and_inline_intro(self):
        body = ('<h2>Results</h2><p><i>Customers</i> – Customer sales are shown below:</p>'
                + TABLE.format(caption='<caption>Customer Concentration</caption>'))
        self.assertEqual(extract(body)[0].title, 'Customer Concentration')

    def test_no_heading_uses_last_sentence_and_ignores_table_wrapper(self):
        body = ('<p>Our customers are varied. U.S. Customers account for 10.5% of sales:</p>'
                '<div>43</div><p>* Less than 10% of revenue.</p>'
                '<div>(In millions)</div>' + TABLE.format(caption=''))
        self.assertEqual(extract(body)[0].title, 'U.S. Customers account for 10.5% of sales:')

    def test_no_heading_or_intro_keeps_placeholder(self):
        self.assertEqual(extract(TABLE.format(caption=''))[0].title, 'Untitled table 1')

    def test_previous_table_prose_is_not_reused(self):
        body = ('<p>Balances are shown below:</p>' + TABLE.format(caption='')
                + '<p>* Less than 10% of revenue.</p>' + TABLE.format(caption=''))
        first, second = extract(body)
        self.assertEqual(first.title, 'Balances are shown below:')
        self.assertEqual(second.title, 'Untitled table 2')


if __name__ == '__main__':
    unittest.main()
