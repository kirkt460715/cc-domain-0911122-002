import base64
import binascii
import datetime as dt
import hashlib
import json

from hangar_ingest.types import PipelineError, canonical, digest, fail, identity, integer
from hangar_ingest.decoders import measurements, workbook
from hangar_ingest.quality import rules as quality_rules
from hangar_ingest.quality.normalize import duplicate_decision, observation
from hangar_ingest.runs import batches, journal

FORMAT = 'xlsx'
MAX_TTL = 86400


def config_version(db, source):
    aliases = db.all('SELECT * FROM aliases WHERE source=? ORDER BY external_id,valid_from', (source,))
    device_ids = sorted({row['device_id'] for row in aliases})
    devices = [db.one('SELECT * FROM devices WHERE device_id=?', (device_id,)) for device_id in device_ids]
    rules = [db.one('SELECT * FROM quality_rules WHERE device_id=?', (device_id,)) for device_id in device_ids]
    calibrations = [
        row
        for device_id in device_ids
        for row in db.all('SELECT * FROM calibrations WHERE device_id=? ORDER BY valid_from,calibration_id', (device_id,))
    ]
    return digest({'aliases': aliases, 'devices': devices, 'quality_rules': rules, 'calibrations': calibrations})


def _classify(db, source, rows):
    planned = {}
    classified = []
    for row in rows:
        outcome = {'row': row['row'], 'evidence': row['evidence'], 'record': row['record'], 'error': row['error']}
        record = row['record']
        if row['error'] is not None:
            classified.append({**outcome, 'outcome': 'quarantine'})
            continue
        event_id = record['event_id']
        if event_id in planned:
            prior = planned[event_id]
            if prior['raw_hash'] == digest(record):
                classified.append({**outcome, 'outcome': 'duplicate', 'observation_id': prior['observation_id']})
                continue
            rule = quality_rules.get(db, prior['device_id'])
            if rule['duplicate_policy'] == 'keep_first':
                classified.append({**outcome, 'outcome': 'ignored_conflict', 'observation_id': prior['observation_id']})
                continue
            classified.append({**outcome, 'outcome': 'quarantine', 'error': {
                'code': 'event_conflict',
                'message': 'Source event ID has different content',
                'context': {'event_id': event_id},
            }})
            continue
        try:
            duplicate = duplicate_decision(db, source, record)
            if duplicate is not None:
                classified.append({**outcome, 'outcome': duplicate['status'], 'observation_id': duplicate['observation_id']})
                continue
            normalized = observation(db, source, record)
        except PipelineError as exc:
            classified.append({**outcome, 'outcome': 'quarantine', 'error': exc.as_dict()})
            continue
        planned[event_id] = {
            'raw_hash': digest(record),
            'device_id': normalized['device_id'],
            'observation_id': normalized['observation_id'],
        }
        classified.append({**outcome, 'outcome': 'accept', 'observation_id': normalized['observation_id']})
    return classified


def preview(db, source, data, rules, ttl_seconds=3600):
    identity(source, 'source')
    if not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024:
        fail('input_size', 'Workbook input must be bounded bytes')
    integer(ttl_seconds, 'ttl_seconds', 1, MAX_TTL)
    book = workbook.read(data)
    parsed = measurements.parse_rules(rules)
    rows = _classify(db, source, measurements.convert(book, parsed))
    checksum = hashlib.sha256(data).hexdigest()
    version = config_version(db, source)
    rules_digest = digest(parsed)
    preview_id = digest({'source': source, 'workbook': checksum, 'rules': rules_digest, 'config': version})
    summary = {'total': len(rows)}
    for row in rows:
        summary[row['outcome']] = summary.get(row['outcome'], 0) + 1
    now = db.clock()
    point = dt.datetime.fromisoformat(now.replace('Z', '+00:00')) + dt.timedelta(seconds=ttl_seconds)
    expires = point.isoformat(timespec='microseconds').replace('+00:00', 'Z')
    result = {
        'preview_id': preview_id,
        'source': source,
        'workbook': {'sha256': checksum, 'date_system': '1904' if book.date1904 else '1900', 'sheets': book.sheets},
        'rules': parsed,
        'rules_digest': rules_digest,
        'config_version': version,
        'summary': summary,
        'rows': rows,
        'created_at': now,
        'expires_at': expires,
    }
    with db.transaction():
        db.execute(
            "INSERT OR REPLACE INTO workbook_previews(preview_id,source,workbook_hash,rules,rules_digest,config_version,result,state,confirmed_batch,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,'open',NULL,?,?)",
            (preview_id, source, checksum, canonical(parsed), rules_digest, version, canonical(result), now, expires),
        )
        journal.record(db, 'workbook.previewed', {
            'preview_id': preview_id, 'source': source, 'sha256': checksum,
            'rules_digest': rules_digest, 'config_version': version, 'summary': summary,
        })
    return result


def _stage(db, batch_id, source, data, rows):
    checksum = hashlib.sha256(data).hexdigest()
    with db.transaction():
        existing = db.one('SELECT * FROM batches WHERE batch_id=?', (batch_id,))
        if existing:
            if (existing['source'], existing['content_hash'], existing['format']) == (source, checksum, FORMAT):
                return {**existing, 'duplicate': True}
            fail('batch_conflict', 'Batch identifier already has different content')
        duplicate = db.one('SELECT * FROM batches WHERE source=? AND content_hash=? AND format=?', (source, checksum, FORMAT))
        if duplicate:
            return {**duplicate, 'duplicate': True}
        now = db.clock()
        db.execute(
            "INSERT INTO batches(batch_id,source,content_hash,format,state,revision,cursor,total,accepted,rejected,created_at,updated_at) "
            "VALUES(?,?,?,?,'staged',1,0,0,0,0,?,?)", (batch_id, source, checksum, FORMAT, now, now),
        )
        count = 0
        for row in rows:
            count += 1
            if row['record'] is not None:
                payload, error = row['record'], None
            else:
                payload = {'sheet': row['evidence']['sheet'], 'row': row['row'], 'raw': row['evidence']['values']}
                error = row['error']
            db.execute(
                'INSERT INTO raw_rows(batch_id,row_number,payload,raw_hash,state,error) VALUES(?,?,?,?,?,?)',
                (batch_id, row['row'], canonical(payload), digest(payload), 'invalid' if error else 'pending', canonical(error) if error else None),
            )
        db.execute('UPDATE batches SET total=? WHERE batch_id=?', (count, batch_id))
        journal.record(db, 'batch.staged', {'batch_id': batch_id, 'source': source, 'records': count, 'sha256': checksum, 'format': FORMAT})
        return batches.get(db, batch_id)


def confirm(db, preview_id, batch_id, data):
    identity(preview_id, 'preview_id')
    identity(batch_id, 'batch_id')
    if not isinstance(data, bytes) or not data or len(data) > 64 * 1024 * 1024:
        fail('input_size', 'Workbook input must be bounded bytes')
    checksum = hashlib.sha256(data).hexdigest()
    with db.transaction():
        row = db.one('SELECT * FROM workbook_previews WHERE preview_id=?', (preview_id,))
        if row is None:
            fail('unknown_preview', 'Preview does not exist', preview_id=preview_id)
        if checksum != row['workbook_hash']:
            fail('workbook_mismatch', 'Confirmed content differs from the previewed workbook')
        if row['state'] == 'open':
            if db.clock() > row['expires_at']:
                fail('preview_expired', 'Preview has expired; run a new preview')
            if config_version(db, row['source']) != row['config_version']:
                fail('preview_stale', 'Referenced configuration changed since the preview; run a new preview')
        elif row['state'] != 'confirmed':
            fail('preview_state', 'Preview is not confirmable', state=row['state'])
        result = json.loads(row['result'])
        staged = _stage(db, batch_id, row['source'], data, result['rows'])
        db.execute("UPDATE workbook_previews SET state='confirmed',confirmed_batch=? WHERE preview_id=?", (staged['batch_id'], preview_id))
        journal.record(db, 'workbook.confirmed', {
            'preview_id': preview_id, 'batch_id': staged['batch_id'], 'sha256': checksum,
            'rules_digest': row['rules_digest'], 'config_version': row['config_version'],
        })
        return {**staged, 'preview_id': preview_id}


def get_preview(db, preview_id):
    identity(preview_id, 'preview_id')
    row = db.one('SELECT * FROM workbook_previews WHERE preview_id=?', (preview_id,))
    if row is None:
        fail('unknown_preview', 'Preview does not exist', preview_id=preview_id)
    return {**json.loads(row['result']), 'state': row['state'], 'confirmed_batch': row['confirmed_batch']}


def _decode(content_base64):
    if not isinstance(content_base64, str) or not content_base64 or len(content_base64) > 96 * 1024 * 1024:
        fail('content_base64', 'Workbook content must be bounded base64 text')
    try:
        return base64.b64decode(content_base64.encode(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PipelineError('content_base64', 'Workbook content must be valid base64') from exc


def preview_base64(db, source, content_base64, rules, ttl_seconds=3600):
    return preview(db, source, _decode(content_base64), rules, ttl_seconds)


def confirm_base64(db, preview_id, batch_id, content_base64):
    return confirm(db, preview_id, batch_id, _decode(content_base64))
