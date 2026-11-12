import base64
import datetime as dt
import io
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from lab.workbooks import build, cell
from hangar_ingest.types import PipelineError
from hangar_ingest.decoders import measurements, workbook
from hangar_ingest.quality import devices, review, rules
from hangar_ingest.runs import batches, inspection, intake, processor
from hangar_ingest.runs.database import Database
from hangar_ingest.api import Application
from hangar_ingest.cli import main
from tests.support import Clock

RULES = {
    'measurement_sheet': '测量',
    'config_sheet': '仪表',
    'timezone': 'America/New_York',
    'columns': {'event_id': 'A', 'alias': 'B', 'local_time': 'C', 'value': 'D', 'fold': 'E'},
    'config_columns': {'alias': 'A', 'device': 'B', 'unit': 'C'},
}


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path1900 = build(self.root / 'w1900.xlsx', False)
        self.path1904 = build(self.root / 'w1904.xlsx', True)

    def tearDown(self):
        self.directory.cleanup()

    def data(self, date1904):
        return (self.path1904 if date1904 else self.path1900).read_bytes()

    def test_workbook_structure_and_cell_types(self):
        book = workbook.read(self.data(False))
        self.assertFalse(book.date1904)
        self.assertEqual(book.sheets, ['测量', '仪表'])
        rows = book.rows('测量')
        self.assertEqual(rows[2]['A'].value, '0001')
        self.assertEqual(rows[2]['A'].kind, 'inlineStr')
        self.assertEqual(rows[2]['B'].value, 'sensor1')
        self.assertEqual(rows[2]['B'].kind, 's')
        self.assertNotIn('E', rows[4])
        self.assertEqual(rows[5]['D'].formula, '10+10')
        self.assertIsNone(rows[5]['D'].raw)
        self.assertEqual(rows[8]['D'].value, '20')
        self.assertEqual(rows[10]['D'].kind, 'e')
        self.assertEqual(rows[10]['D'].value, '#DIV/0!')
        self.assertEqual(book.rows('仪表')[3]['A'].value, 'conflict')
        self.assertTrue(workbook.read(self.data(True)).date1904)
        with self.assertRaises(PipelineError) as failure:
            workbook.read(b'not a zip')
        self.assertEqual(failure.exception.code, 'workbook_format')
        with self.assertRaises(PipelineError) as failure:
            book.rows('missing')
        self.assertEqual(failure.exception.code, 'worksheet_missing')

    def test_serial_date_systems(self):
        self.assertEqual(measurements.serial_local(Decimal('46328.5'), False), dt.datetime(2026, 11, 2, 12, 0))
        self.assertEqual(measurements.serial_local(Decimal('46328.5'), True), dt.datetime(2030, 11, 3, 12, 0))
        self.assertEqual(measurements.serial_local(Decimal('60'), True), dt.datetime(1904, 3, 1))
        self.assertEqual(measurements.serial_local(Decimal('59'), False), dt.datetime(1900, 2, 28))
        self.assertEqual(measurements.serial_local(Decimal('61'), False), dt.datetime(1900, 3, 1))
        for serial in ('60', '60.5'):
            with self.subTest(serial=serial):
                with self.assertRaises(PipelineError) as failure:
                    measurements.serial_local(Decimal(serial), False)
                self.assertEqual(failure.exception.code, 'excel_serial_60')
        with self.assertRaises(PipelineError) as failure:
            measurements.serial_local(Decimal('0'), False)
        self.assertEqual(failure.exception.code, 'excel_serial_invalid')

    def test_local_time_fold_gap_and_plain(self):
        zone = ZoneInfo('America/New_York')
        ambiguous = dt.datetime(2026, 11, 1, 1, 30)
        self.assertEqual(measurements.local_to_utc(ambiguous, zone, 0), '2026-11-01T05:30:00.000000Z')
        self.assertEqual(measurements.local_to_utc(ambiguous, zone, 1), '2026-11-01T06:30:00.000000Z')
        with self.assertRaises(PipelineError) as failure:
            measurements.local_to_utc(ambiguous, zone, None)
        self.assertEqual(failure.exception.code, 'fold_required')
        with self.assertRaises(PipelineError) as failure:
            measurements.local_to_utc(dt.datetime(2026, 3, 8, 2, 30), zone, None)
        self.assertEqual(failure.exception.code, 'local_time_skipped')
        with self.assertRaises(PipelineError) as failure:
            measurements.local_to_utc(dt.datetime(2026, 3, 8, 2, 30), zone, 1)
        self.assertEqual(failure.exception.code, 'local_time_skipped')
        self.assertEqual(measurements.local_to_utc(dt.datetime(2026, 11, 2, 12, 0), zone, None), '2026-11-02T17:00:00.000000Z')

    def test_rules_validation(self):
        parsed = measurements.parse_rules(RULES)
        self.assertEqual(parsed['columns']['event_id'], 'A')
        self.assertEqual(parsed['header_rows'], 1)
        broken = [
            ('timezone_unknown', {**RULES, 'timezone': 'Not/AZone'}),
            ('columns_duplicate', {**RULES, 'columns': {'event_id': 'A', 'alias': 'a', 'local_time': 'C', 'value': 'D'}}),
            ('column_invalid', {**RULES, 'columns': {'event_id': '1', 'alias': 'B', 'local_time': 'C', 'value': 'D'}}),
            ('invalid_fields', {**RULES, 'surprise': True}),
        ]
        for code, candidate in broken:
            with self.subTest(code=code):
                with self.assertRaises(PipelineError) as failure:
                    measurements.parse_rules(candidate)
                self.assertEqual(failure.exception.code, code)
        with self.assertRaises(PipelineError) as failure:
            measurements.convert(workbook.read(self.data(False)), {**RULES, 'config_sheet': 'missing'})
        self.assertEqual(failure.exception.code, 'worksheet_missing')

    def test_convert_rows_1900(self):
        rows = {row['row']: row for row in measurements.convert(workbook.read(self.data(False)), RULES)}
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[2]['record'], {'event_id': '0001', 'device': 'dev1', 'observed_at': '2026-11-01T05:30:00.000000Z', 'value': '20.5', 'unit': 'C'})
        self.assertEqual(rows[3]['record']['observed_at'], '2026-11-01T06:30:00.000000Z')
        self.assertEqual(rows[4]['error']['code'], 'local_time_skipped')
        self.assertEqual(rows[5]['error']['code'], 'formula_cache_missing')
        self.assertEqual(rows[5]['error']['context']['cell'], 'D5')
        self.assertEqual(rows[6]['error']['code'], 'excel_serial_60')
        self.assertEqual(rows[7]['error']['code'], 'config_alias_conflict')
        self.assertEqual(rows[7]['error']['context']['candidates'], 2)
        self.assertEqual(rows[8]['record']['observed_at'], '2026-11-02T17:00:00.000000Z')
        self.assertEqual(rows[8]['record']['value'], '20')
        self.assertEqual(rows[9]['error']['code'], 'fold_required')
        self.assertEqual(rows[10]['error']['code'], 'formula_error')
        self.assertEqual(rows[10]['error']['context']['error'], '#DIV/0!')
        evidence = rows[6]['evidence']
        self.assertEqual(evidence['sheet'], '测量')
        self.assertEqual(evidence['cells']['local_time']['ref'], 'C6')
        self.assertEqual(evidence['cells']['local_time']['raw'], '60')
        self.assertEqual(evidence['values']['event_id'], '0005')

    def test_convert_rows_1904_date_system_difference(self):
        rows = {row['row']: row for row in measurements.convert(workbook.read(self.data(True)), RULES)}
        self.assertEqual(rows[2]['record']['observed_at'], '2026-11-01T05:30:00.000000Z')
        self.assertEqual(rows[6]['error']['code'], 'invalid_time')
        self.assertEqual(rows[6]['error']['context']['local'], '1904-03-01T00:00:00')
        self.assertEqual(rows[8]['record']['observed_at'], '2030-11-03T17:00:00.000000Z')

    def test_convert_extra_row_shapes(self):
        extra = [
            cell('A11', '123', 'n') + cell('B11', '0', 's') + cell('C11', '2026-11-02T13:00:00') + cell('D11', '21.5'),
            cell('A12', '0010') + cell('B12', 'unknown') + cell('C12', '2026-11-02T13:00:00') + cell('D12', '1', 'n'),
            cell('A13', '0011') + cell('B13', '0', 's') + cell('C13', '2026-11-02T13:00:00'),
        ]
        path = build(self.root / 'extra.xlsx', False, extra=extra)
        rows = {row['row']: row for row in measurements.convert(workbook.read(path.read_bytes()), RULES)}
        self.assertEqual(rows[11]['record']['event_id'], '123')
        self.assertEqual(rows[11]['record']['value'], '21.5')
        self.assertEqual(rows[12]['error']['code'], 'config_alias_missing')
        self.assertEqual(rows[13]['error']['code'], 'cell_missing')
        self.assertEqual(rows[13]['error']['context']['ref'], 'D13')


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.clock = Clock()
        self.db = Database(':memory:', clock=self.clock)
        devices.register(self.db, 'dev1', 'plane1', 'engine1', 'temperature', 'C')
        devices.register(self.db, 'dev2', 'plane1', 'engine2', 'temperature', 'C')
        devices.add_alias(self.db, 'feed1', 'dev1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
        devices.add_alias(self.db, 'feed1', 'dev2', 'dev2', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
        devices.add_calibration(self.db, 'cal1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
        rules.configure(self.db, 'dev1', minimum='-100', maximum='200')
        self.data1900 = build(self.root / 'w1900.xlsx', False).read_bytes()
        self.data1904 = build(self.root / 'w1904.xlsx', True).read_bytes()

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def preview(self, data=None, source='feed1'):
        return intake.preview(self.db, source, data if data is not None else self.data1900, RULES)

    def test_preview_outcomes_and_cell_evidence(self):
        preview = self.preview()
        self.assertEqual(preview['workbook']['date_system'], '1900')
        self.assertEqual(preview['summary'], {'total': 9, 'accept': 3, 'quarantine': 6})
        rows = {row['row']: row for row in preview['rows']}
        self.assertEqual(rows[2]['outcome'], 'accept')
        self.assertEqual(rows[2]['record']['observed_at'], '2026-11-01T05:30:00.000000Z')
        self.assertEqual(rows[2]['evidence']['cells']['event_id']['ref'], 'A2')
        self.assertEqual(rows[2]['evidence']['cells']['event_id']['value'], '0001')
        self.assertEqual(rows[3]['record']['observed_at'], '2026-11-01T06:30:00.000000Z')
        for row_number, code in [(4, 'local_time_skipped'), (5, 'formula_cache_missing'), (6, 'excel_serial_60'), (7, 'config_alias_conflict'), (9, 'fold_required'), (10, 'formula_error')]:
            with self.subTest(row=row_number):
                self.assertEqual(rows[row_number]['outcome'], 'quarantine')
                self.assertEqual(rows[row_number]['error']['code'], code)
                self.assertEqual(rows[row_number]['error']['context']['sheet'], '测量')
        self.assertEqual(rows[8]['outcome'], 'accept')
        again = self.preview()
        self.assertEqual(again['preview_id'], preview['preview_id'])

    def test_confirm_drain_quarantine_and_integrity(self):
        preview = self.preview()
        staged = intake.confirm(self.db, preview['preview_id'], 'b1', self.data1900)
        self.assertEqual(staged['format'], 'xlsx')
        self.assertEqual(staged['total'], 9)
        batch = processor.drain(self.db, 'b1', chunk=4)['batch']
        self.assertEqual((batch['accepted'], batch['rejected'], batch['state']), (3, 6, 'partial'))
        observations = self.db.all('SELECT * FROM observations ORDER BY external_event')
        self.assertEqual([row['external_event'] for row in observations], ['0001', '0002', '0007'])
        self.assertEqual(observations[0]['event_time'], '2026-11-01T05:30:00.000000Z')
        self.assertEqual(observations[0]['raw_value'], '20.5')
        self.assertEqual(observations[2]['event_time'], '2026-11-02T17:00:00.000000Z')
        self.assertEqual(observations[2]['raw_value'], '20')
        self.assertEqual(review.reason_counts(self.db), {
            'config_alias_conflict': 1, 'excel_serial_60': 1, 'fold_required': 1,
            'formula_cache_missing': 1, 'formula_error': 1, 'local_time_skipped': 1,
        })
        entries = {entry['row_number']: entry for entry in review.listing(self.db)}
        self.assertEqual(entries[6]['payload']['sheet'], '测量')
        self.assertEqual(entries[6]['payload']['raw']['local_time'], '60')
        self.assertEqual(entries[6]['reasons']['context']['cell'], 'C6')
        self.assertTrue(processor.verify_checkpoint(self.db, 'b1')['ok'])
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_confirm_binds_workbook_expiry_and_config_version(self):
        preview = self.preview()
        with self.assertRaises(PipelineError) as failure:
            intake.confirm(self.db, preview['preview_id'], 'b1', self.data1904)
        self.assertEqual(failure.exception.code, 'workbook_mismatch')
        self.clock.tick(3601)
        with self.assertRaises(PipelineError) as failure:
            intake.confirm(self.db, preview['preview_id'], 'b1', self.data1900)
        self.assertEqual(failure.exception.code, 'preview_expired')
        preview = self.preview()
        devices.add_calibration(self.db, 'cal2', 'dev2', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
        with self.assertRaises(PipelineError) as failure:
            intake.confirm(self.db, preview['preview_id'], 'b1', self.data1900)
        self.assertEqual(failure.exception.code, 'preview_stale')
        self.assertEqual(batches.listing(self.db), [])
        fresh = self.preview()
        self.assertNotEqual(fresh['preview_id'], preview['preview_id'])
        staged = intake.confirm(self.db, fresh['preview_id'], 'b1', self.data1900)
        self.assertEqual(staged['total'], 9)
        fetched = intake.get_preview(self.db, fresh['preview_id'])
        self.assertEqual(fetched['state'], 'confirmed')
        self.assertEqual(fetched['confirmed_batch'], 'b1')

    def test_repeat_delivery_and_cross_workbook_dedup(self):
        first = intake.confirm(self.db, self.preview()['preview_id'], 'b1', self.data1900)
        self.assertNotIn('duplicate', first)
        processor.drain(self.db, 'b1')
        again = intake.confirm(self.db, self.preview()['preview_id'], 'b2', self.data1900)
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['batch_id'], 'b1')
        cross = self.preview(self.data1904)
        outcomes = {row['row']: row['outcome'] for row in cross['rows']}
        self.assertEqual(outcomes[2], 'duplicate')
        self.assertEqual(outcomes[8], 'quarantine')
        staged = intake.confirm(self.db, cross['preview_id'], 'b3', self.data1904)
        processor.drain(self.db, 'b3')
        states = {row['row_number']: row['state'] for row in batches.rows(self.db, 'b3')}
        self.assertEqual(states[2], 'duplicate')
        self.assertEqual(states[3], 'duplicate')
        self.assertEqual(states[8], 'quarantined')
        self.assertEqual(review.reason_counts(self.db).get('event_conflict'), 1)
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM observations'), 3)
        self.assertTrue(inspection.check(self.db)['ok'])

    def test_within_batch_duplicate_prediction(self):
        extra = [
            cell('A11', '0001') + cell('B11', '0', 's') + cell('C11', '2026-11-01T01:30:00') + cell('D11', '20.5', 'n') + cell('E11', '0', 'n'),
            cell('A12', '0001') + cell('B12', '0', 's') + cell('C12', '2026-11-02T14:00:00') + cell('D12', '99', 'n'),
        ]
        data = build(self.root / 'dup.xlsx', False, extra=extra).read_bytes()
        preview = self.preview(data)
        rows = {row['row']: row for row in preview['rows']}
        self.assertEqual(rows[11]['outcome'], 'duplicate')
        self.assertEqual(rows[12]['outcome'], 'quarantine')
        self.assertEqual(rows[12]['error']['code'], 'event_conflict')
        staged = intake.confirm(self.db, preview['preview_id'], 'b1', data)
        processor.drain(self.db, 'b1')
        states = {row['row_number']: row['state'] for row in batches.rows(self.db, 'b1')}
        self.assertEqual(states[11], 'duplicate')
        self.assertEqual(states[12], 'quarantined')
        self.assertEqual(staged['total'], 11)

    def test_api_operations(self):
        app = Application(self.db)
        self.assertIn('workbook.preview', app.commands())
        self.assertIn('workbook.confirm', app.commands())
        content = base64.b64encode(self.data1900).decode()
        preview = app.call('workbook.preview', {'source': 'feed1', 'content_base64': content, 'rules': RULES})
        staged = app.call('workbook.confirm', {'preview_id': preview['preview_id'], 'batch_id': 'api-batch', 'content_base64': content})
        self.assertEqual(staged['format'], 'xlsx')
        fetched = app.call('workbook.preview.get', {'preview_id': preview['preview_id']})
        self.assertEqual(fetched['state'], 'confirmed')
        batch = processor.drain(self.db, 'api-batch')['batch']
        self.assertEqual((batch['accepted'], batch['rejected']), (3, 6))
        with self.assertRaises(PipelineError) as failure:
            app.call('workbook.preview', {'source': 'feed1', 'content_base64': '!!!', 'rules': RULES})
        self.assertEqual(failure.exception.code, 'content_base64')

    def test_cli_preview_and_confirm(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / 'cli.db')
            workbook_path = Path(directory) / 'measurements.xlsx'
            workbook_path.write_bytes(self.data1900)
            rules_path = Path(directory) / 'rules.json'
            rules_path.write_text(json.dumps(RULES))
            db = Database(db_path, clock=Clock())
            devices.register(db, 'dev1', 'plane1', 'engine1', 'temperature', 'C')
            devices.add_alias(db, 'feed1', 'dev1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
            devices.add_calibration(db, 'cal1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
            rules.configure(db, 'dev1', minimum='-100', maximum='200')
            db.close()
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(['--db', db_path, 'preview', str(workbook_path), '--source', 'feed1', '--rules', str(rules_path)], stdout=stdout, stderr=stderr)
            self.assertEqual(code, 0, stderr.getvalue())
            preview = json.loads(stdout.getvalue())['result']
            self.assertEqual(preview['summary']['accept'], 3)
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(['--db', db_path, 'confirm', str(workbook_path), '--preview-id', preview['preview_id'], '--batch', 'cli-batch'], stdout=stdout, stderr=stderr)
            self.assertEqual(code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())['result']
            self.assertEqual((result['batch']['accepted'], result['batch']['rejected']), (3, 6))
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(['--db', db_path, 'check'], stdout=stdout, stderr=stderr)
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())['result']['ok'])
