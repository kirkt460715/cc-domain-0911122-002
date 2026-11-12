import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo

from hangar_ingest.types import PipelineError, fail, instant, integer, object_fields, text
from hangar_ingest.decoders.sensors import normalize
from hangar_ingest.decoders.workbook import column_letter

TEXT_KINDS = {'s', 'inlineStr', 'str', 'd'}
MEASUREMENT_FIELDS = {'event_id', 'alias', 'local_time', 'value'}
CONFIG_FIELDS = {'alias', 'device', 'unit'}
SERIAL_MAX = 2958465


def parse_rules(rules):
    object_fields(rules, {'measurement_sheet', 'config_sheet', 'timezone', 'columns', 'config_columns'}, {'header_rows'})
    measurement_sheet = text(rules['measurement_sheet'], 'measurement_sheet', 256)
    config_sheet = text(rules['config_sheet'], 'config_sheet', 256)
    timezone = text(rules['timezone'], 'timezone', 128)
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError) as exc:
        raise PipelineError('timezone_unknown', 'Timezone must be a known IANA name', timezone=timezone) from exc
    header_rows = integer(rules.get('header_rows', 1), 'header_rows', 0, 100)
    columns = rules['columns']
    object_fields(columns, MEASUREMENT_FIELDS, {'fold'})
    selected = {field: column_letter(value, field) for field, value in columns.items()}
    if len(set(selected.values())) != len(selected):
        fail('columns_duplicate', 'Measurement columns must be distinct')
    config_columns = rules['config_columns']
    object_fields(config_columns, CONFIG_FIELDS)
    joined = {field: column_letter(value, field) for field, value in config_columns.items()}
    if len(set(joined.values())) != len(joined):
        fail('columns_duplicate', 'Configuration columns must be distinct')
    return {
        'measurement_sheet': measurement_sheet,
        'config_sheet': config_sheet,
        'timezone': timezone,
        'header_rows': header_rows,
        'columns': selected,
        'config_columns': joined,
    }


def _decimal(cell, field):
    try:
        return Decimal(str(cell.value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise PipelineError('invalid_number', 'Cell does not contain a decimal', cell=cell.ref, field=field) from exc


def _cached(cell, field):
    if cell.formula is not None:
        if cell.raw is None:
            fail('formula_cache_missing', 'Formula has no cached value and recalculation is not performed', cell=cell.ref, field=field)
        if cell.kind == 'e':
            fail('formula_error', 'Formula cached value is an error', cell=cell.ref, field=field, error=cell.value)
    if cell.kind == 'e':
        fail('cell_error', 'Cell contains an error value', cell=cell.ref, field=field, error=cell.value)


def _required(selected, refs, field):
    cell = selected.get(field)
    if cell is None:
        fail('cell_missing', 'Required cell is missing', field=field, ref=refs[field])
    return cell


def event_id_value(cell):
    _cached(cell, 'event_id')
    if cell.kind in TEXT_KINDS:
        if cell.value is None or cell.value == '':
            fail('cell_missing', 'Required cell is empty', cell=cell.ref, field='event_id')
        return str(cell.value)
    if cell.kind == 'n':
        number = _decimal(cell, 'event_id')
        if number != number.to_integral_value():
            fail('event_id_invalid', 'Numeric event identifier must be integral', cell=cell.ref)
        return format(number.to_integral_value(), 'f')
    fail('event_id_invalid', 'Unsupported event identifier cell type', cell=cell.ref, kind=cell.kind)


def alias_value(cell):
    _cached(cell, 'alias')
    if cell.kind in TEXT_KINDS and cell.value is not None and str(cell.value).strip():
        return str(cell.value).strip()
    if cell.kind == 'n':
        return format(_decimal(cell, 'alias').normalize(), 'f')
    fail('alias_invalid', 'Unsupported alias cell', cell=cell.ref, kind=cell.kind)


def value_value(cell):
    _cached(cell, 'value')
    if cell.kind in TEXT_KINDS or cell.kind == 'n':
        if cell.value is None or not str(cell.value).strip():
            fail('cell_missing', 'Required cell is empty', cell=cell.ref, field='value')
        return str(cell.value).strip()
    fail('value_invalid', 'Unsupported value cell type', cell=cell.ref, kind=cell.kind)


def fold_value(cell):
    if cell is None:
        return None
    _cached(cell, 'fold')
    if cell.value is None or not str(cell.value).strip():
        return None
    value = str(cell.value).strip()
    if value not in {'0', '1'}:
        fail('fold_invalid', 'Fold must be 0 or 1', cell=cell.ref, value=value)
    return int(value)


def parse_local_text(value, cell=None):
    context = {'value': value}
    if cell is not None:
        context['cell'] = cell.ref
    if 'T' not in value and ' ' not in value:
        fail('local_time_invalid', 'Local time must include a time of day', **context)
    try:
        point = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PipelineError('local_time_invalid', 'Local time is not ISO formatted', **context) from exc
    if point.tzinfo is not None:
        fail('local_time_invalid', 'Local time must be a naive wall clock', **context)
    return point


def serial_local(value, date1904, cell=None):
    context = {'serial': str(value), 'date_system': '1904' if date1904 else '1900'}
    if cell is not None:
        context['cell'] = cell.ref
    if date1904:
        if not 0 <= value <= SERIAL_MAX:
            fail('excel_serial_invalid', 'Excel serial is outside the supported range', **context)
        base = dt.datetime(1904, 1, 1)
    else:
        if not 1 <= value <= SERIAL_MAX:
            fail('excel_serial_invalid', 'Excel serial is outside the supported range', **context)
        if 60 <= value < 61:
            fail('excel_serial_60', 'Excel serial 60 is the fictitious 1900-02-29 and cannot be a real date', **context)
        base = dt.datetime(1899, 12, 31) if value < 60 else dt.datetime(1899, 12, 30)
    days = int(value)
    micros = int(((value - days) * 86400000000).quantize(Decimal('1'), rounding=ROUND_HALF_EVEN))
    try:
        return base + dt.timedelta(days=days, microseconds=micros)
    except OverflowError as exc:
        raise PipelineError('excel_serial_invalid', 'Excel serial is outside the supported range', **context) from exc


def local_value(cell, date1904):
    _cached(cell, 'local_time')
    if cell.kind in TEXT_KINDS:
        if cell.value is None or not str(cell.value).strip():
            fail('cell_missing', 'Required cell is empty', cell=cell.ref, field='local_time')
        return parse_local_text(str(cell.value).strip(), cell)
    if cell.kind == 'n':
        return serial_local(_decimal(cell, 'local_time'), date1904, cell)
    fail('local_time_invalid', 'Unsupported local time cell type', cell=cell.ref, kind=cell.kind)


def local_to_utc(naive, zone, fold):
    def exists(option):
        aware = naive.replace(tzinfo=zone, fold=option)
        return aware.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None) == naive
    if not exists(0) and not exists(1):
        fail('local_time_skipped', 'Local time does not exist because of a daylight saving gap', local=naive.isoformat(), timezone=zone.key)
    offsets = {option: naive.replace(tzinfo=zone, fold=option).utcoffset() for option in (0, 1)}
    if offsets[0] != offsets[1]:
        if fold is None:
            fail('fold_required', 'Local time is ambiguous during the daylight saving overlap and needs an explicit fold', local=naive.isoformat(), timezone=zone.key)
        aware = naive.replace(tzinfo=zone, fold=fold)
    else:
        aware = naive.replace(tzinfo=zone, fold=0)
    utc = aware.astimezone(dt.timezone.utc)
    try:
        return instant(utc.isoformat(timespec='microseconds').replace('+00:00', 'Z'))
    except PipelineError as exc:
        exc.context.update({'local': naive.isoformat(), 'timezone': zone.key})
        raise


def _config_text(cell, field, sheet, row_number):
    if cell is None:
        fail('config_invalid', 'Configuration row is missing a field', sheet=sheet, row=row_number, field=field)
    _cached(cell, field)
    if cell.kind in TEXT_KINDS and cell.value is not None and str(cell.value).strip():
        return str(cell.value).strip()
    if cell.kind == 'n':
        return format(_decimal(cell, field).normalize(), 'f')
    fail('config_invalid', 'Configuration cell has an unsupported type', sheet=sheet, row=row_number, field=field, cell=cell.ref)


def config_map(book, rules):
    sheet = rules['config_sheet']
    letters = rules['config_columns']
    mapping = {}
    for row_number, cells in sorted(book.rows(sheet).items()):
        if row_number <= rules['header_rows']:
            continue
        alias_cell = cells.get(letters['alias'])
        if alias_cell is None or alias_cell.value is None or str(alias_cell.value).strip() == '':
            continue
        try:
            alias = alias_value(alias_cell)
        except PipelineError as exc:
            exc.context.update({'sheet': sheet, 'row': row_number})
            raise
        device = _config_text(cells.get(letters['device']), 'device', sheet, row_number)
        unit = _config_text(cells.get(letters['unit']), 'unit', sheet, row_number)
        mapping.setdefault(alias, []).append({'device': device, 'unit': unit, 'ref': alias_cell.ref, 'row': row_number})
    return mapping


def _convert_row(selected, refs, config, zone, date1904):
    alias = alias_value(_required(selected, refs, 'alias'))
    candidates = config.get(alias, [])
    if not candidates:
        fail('config_alias_missing', 'Alias has no configuration row', alias=alias)
    if len(candidates) > 1:
        fail('config_alias_conflict', 'Alias has multiple configuration rows', alias=alias, candidates=len(candidates), refs=[entry['ref'] for entry in candidates])
    fold = fold_value(selected.get('fold'))
    observed_at = local_to_utc(local_value(_required(selected, refs, 'local_time'), date1904), zone, fold)
    return normalize({
        'event_id': event_id_value(_required(selected, refs, 'event_id')),
        'device': candidates[0]['device'],
        'observed_at': observed_at,
        'value': value_value(_required(selected, refs, 'value')),
        'unit': candidates[0]['unit'],
    })


def _evidence(sheet, row_number, selected):
    cells = {}
    values = {}
    for field, cell in selected.items():
        if cell is None:
            values[field] = None
            continue
        cells[field] = {'ref': cell.ref, 'kind': cell.kind, 'raw': cell.raw, 'value': cell.value}
        if cell.formula is not None:
            cells[field]['formula'] = cell.formula
        values[field] = cell.value
    return {'sheet': sheet, 'row': row_number, 'cells': cells, 'values': values}


def convert(book, rules):
    rules = parse_rules(rules)
    config = config_map(book, rules)
    zone = ZoneInfo(rules['timezone'])
    sheet = rules['measurement_sheet']
    grouped = book.rows(sheet)
    results = []
    for row_number in sorted(grouped):
        if row_number <= rules['header_rows']:
            continue
        cells = grouped[row_number]
        selected = {field: cells.get(letter) for field, letter in rules['columns'].items()}
        present = [cell for cell in selected.values() if cell is not None]
        if not present or all(cell.formula is None and (cell.value is None or str(cell.value).strip() == '') for cell in present):
            continue
        evidence = _evidence(sheet, row_number, selected)
        refs = {field: f'{letter}{row_number}' for field, letter in rules['columns'].items()}
        try:
            record = _convert_row(selected, refs, config, zone, book.date1904)
        except PipelineError as exc:
            error = exc.as_dict()
            error['context'] = {**error.get('context', {}), 'sheet': sheet, 'row': row_number}
            results.append({'row': row_number, 'record': None, 'error': error, 'evidence': evidence})
        else:
            results.append({'row': row_number, 'record': record, 'error': None, 'evidence': evidence})
    return results
