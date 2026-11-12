import io
import posixpath
import re
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

from hangar_ingest.types import PipelineError, fail

MAX_SIZE = 64 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_UNCOMPRESSED = 256 * 1024 * 1024
CELL_REF = re.compile(r'([A-Za-z]{1,4})([0-9]{1,7})')
COLUMN_LETTER = re.compile(r'[A-Za-z]{1,3}')


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def column_index(letters):
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def column_letters(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_letter(value, field):
    if not isinstance(value, str) or not COLUMN_LETTER.fullmatch(value):
        fail('column_invalid', 'Column reference must be spreadsheet letters', field=field)
    return value.upper()


class Cell:
    __slots__ = ('ref', 'row', 'column', 'kind', 'value', 'raw', 'formula')

    def __init__(self, ref, row, column, kind, value, raw, formula):
        self.ref = ref
        self.row = row
        self.column = column
        self.kind = kind
        self.value = value
        self.raw = raw
        self.formula = formula


class Workbook:
    def __init__(self, date1904, sheets, cells):
        self.date1904 = date1904
        self.sheets = sheets
        self._cells = cells
        self._rows = {}

    def cells(self, name):
        if name not in self._cells:
            fail('worksheet_missing', 'Workbook does not contain the worksheet', sheet=name)
        return self._cells[name]

    def rows(self, name):
        if name not in self._rows:
            grouped = {}
            for cell in self.cells(name).values():
                grouped.setdefault(cell.row, {})[cell.column] = cell
            self._rows[name] = grouped
        return self._rows[name]


def _parse_xml(data, part):
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise PipelineError('workbook_xml', 'Workbook part is not well-formed XML', part=part) from exc


def _rels(data):
    root = _parse_xml(data, 'relationships')
    result = {}
    for element in root:
        if _local(element.tag) != 'Relationship':
            continue
        attrs = {_local(key): value for key, value in element.attrib.items()}
        if 'Id' in attrs and 'Target' in attrs:
            result[attrs['Id']] = attrs
    return result


def _resolve(base, target):
    if target.startswith('/'):
        return target.lstrip('/')
    return posixpath.normpath(posixpath.join(base, target))


def _shared_strings(root):
    values = []
    for element in root:
        if _local(element.tag) != 'si':
            continue
        values.append(''.join(node.text or '' for node in element.iter() if _local(node.tag) == 't'))
    return values


def _sheet_cells(root, shared, part):
    cells = {}
    row_counter = 0
    for row_element in root.iter():
        if _local(row_element.tag) != 'row':
            continue
        row_counter += 1
        row_ref = row_element.get('r')
        row_number = int(row_ref) if row_ref and row_ref.isdigit() else row_counter
        column_counter = 0
        for cell_element in row_element:
            if _local(cell_element.tag) != 'c':
                continue
            column_counter += 1
            ref = cell_element.get('r')
            if ref:
                match = CELL_REF.fullmatch(ref)
                if not match:
                    fail('workbook_cell', 'Cell reference is malformed', ref=ref, part=part)
                letters, cell_row = match.group(1).upper(), int(match.group(2))
            else:
                letters, cell_row = column_letters(column_counter), row_number
                ref = f'{letters}{cell_row}'
            kind = cell_element.get('t', 'n')
            formula = None
            raw = None
            inline = None
            for child in cell_element:
                tag = _local(child.tag)
                if tag == 'f':
                    formula = child.text if child.text is not None else ''
                elif tag == 'v':
                    raw = child.text
                elif tag == 'is':
                    inline = ''.join(node.text or '' for node in child.iter() if _local(node.tag) == 't')
            if kind == 's':
                try:
                    value = shared[int(raw)]
                except (TypeError, ValueError, IndexError) as exc:
                    raise PipelineError('workbook_shared_strings', 'Shared string reference is invalid', ref=ref, part=part) from exc
            elif kind == 'inlineStr':
                value = inline
            else:
                value = raw
            cells[ref] = Cell(ref, cell_row, letters, kind, value, raw, formula)
    return cells


def read(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_SIZE:
        fail('input_size', 'Workbook input must be bounded bytes')
    try:
        archive = ZipFile(io.BytesIO(data))
    except BadZipFile as exc:
        raise PipelineError('workbook_format', 'Input is not an xlsx workbook') from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED:
            fail('workbook_size', 'Workbook package exceeds supported size')
        names = set(archive.namelist())

        def part(name):
            if name not in names:
                fail('workbook_parts', 'Workbook package is missing a required part', part=name)
            return archive.read(name)

        package = _rels(part('_rels/.rels'))
        office = next((attrs['Target'] for attrs in package.values() if attrs.get('Type', '').endswith('/officeDocument')), None)
        if office is None:
            fail('workbook_parts', 'Workbook package has no office document')
        workbook_part = _resolve('', office)
        root = _parse_xml(part(workbook_part), workbook_part)
        date1904 = False
        sheet_entries = []
        for element in root.iter():
            tag = _local(element.tag)
            if tag == 'workbookPr':
                date1904 = element.get('date1904', '0') in {'1', 'true'}
            elif tag == 'sheet':
                attrs = {_local(key): value for key, value in element.attrib.items()}
                sheet_entries.append((attrs.get('name'), attrs.get('id')))
        base = posixpath.dirname(workbook_part)
        rels_part = _resolve(base, '_rels/' + posixpath.basename(workbook_part) + '.rels')
        relationships = _rels(part(rels_part))
        shared = None
        for attrs in relationships.values():
            if attrs.get('Type', '').endswith('/sharedStrings'):
                shared_part = _resolve(base, attrs['Target'])
                shared = _shared_strings(_parse_xml(part(shared_part), shared_part))
        sheets = []
        cells = {}
        for name, rel_id in sheet_entries:
            if not name or rel_id not in relationships:
                fail('workbook_parts', 'Worksheet relationship is missing', sheet=name)
            target = relationships[rel_id]
            if not target.get('Type', '').endswith('/worksheet'):
                fail('workbook_parts', 'Worksheet relationship type is unsupported', sheet=name)
            sheet_part = _resolve(base, target['Target'])
            sheets.append(name)
            cells[name] = _sheet_cells(_parse_xml(part(sheet_part), sheet_part), shared, sheet_part)
        return Workbook(date1904, sheets, cells)
