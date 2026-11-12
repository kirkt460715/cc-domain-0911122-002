import tempfile
from pathlib import Path
from zipfile import ZipFile
from .workbooks import build
from hangar_ingest.runs.database import Database
from hangar_ingest.runs import inspection, intake, processor
from hangar_ingest.quality import devices, rules

RULES = {
    'measurement_sheet': '测量',
    'config_sheet': '仪表',
    'timezone': 'America/New_York',
    'columns': {'event_id': 'A', 'alias': 'B', 'local_time': 'C', 'value': 'D', 'fold': 'E'},
    'config_columns': {'alias': 'A', 'device': 'B', 'unit': 'C'},
}


def show(preview):
    for row in preview['rows']:
        values = row['evidence']['values']
        detail = row['error']['code'] if row['error'] else row['record']['observed_at']
        print(f"  行{row['row']} 事件 {values.get('event_id')} 原值 {values.get('local_time')} -> {row['outcome']} {detail}")


with tempfile.TemporaryDirectory() as d:
    paths = {flag: build(Path(d) / f'measurements-{1904 if flag else 1900}.xlsx', flag) for flag in [False, True]}
    for flag, path in paths.items():
        with ZipFile(path) as z:
            print('日期体系', 1904 if flag else 1900, '文件成员', z.namelist())
    db = Database(':memory:')
    devices.register(db, 'dev1', 'plane1', 'engine1', 'temperature', 'C')
    devices.register(db, 'dev2', 'plane1', 'engine2', 'temperature', 'C')
    devices.add_calibration(db, 'cal1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
    rules.configure(db, 'dev1', minimum='-100', maximum='200')
    for source in ['feed1', 'feed2']:
        devices.add_alias(db, source, 'dev1', 'dev1', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
        devices.add_alias(db, source, 'dev2', 'dev2', '2026-01-01T00:00:00Z', '2031-01-01T00:00:00Z')
    batches = {}
    for flag, source in [(False, 'feed1'), (True, 'feed2')]:
        data = paths[flag].read_bytes()
        preview = intake.preview(db, source, data, RULES)
        print('日期体系', 1904 if flag else 1900, '预览摘要', preview['summary'])
        show(preview)
        staged = intake.confirm(db, preview['preview_id'], f"xlsx-{1904 if flag else 1900}", data)
        batch = processor.drain(db, staged['batch_id'], chunk=4)['batch']
        batches[flag] = batch
        print('  批次', batch['batch_id'], '状态', batch['state'], '接收', batch['accepted'], '隔离', batch['rejected'])
    data = paths[True].read_bytes()
    preview = intake.preview(db, 'feed1', data, RULES)
    staged = intake.confirm(db, preview['preview_id'], 'xlsx-1904-feed1', data)
    processor.drain(db, staged['batch_id'])
    states = db.all('SELECT row_number,state FROM raw_rows WHERE batch_id=? ORDER BY row_number', (staged['batch_id'],))
    print('跨工作簿重复事件行状态', {row['row_number']: row['state'] for row in states})
    checked = inspection.check(db)
    print('完整性检查', checked['ok'])
    assert checked['ok'], checked['errors']
    assert batches[False]['accepted'] == 3 and batches[False]['rejected'] == 6
    assert batches[True]['accepted'] == 3 and batches[True]['rejected'] == 6
    assert {row['row_number']: row['state'] for row in states}[8] == 'quarantined'
    assert db.scalar('SELECT COUNT(*) FROM observations') == 6
