from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED
from xml.sax.saxutils import escape
NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
def cell(ref,value,kind='inlineStr',formula=None):
    if formula is not None:
        t=f' t="{kind}"' if kind in ('e','str','b') else ''
        return f'<c r="{ref}"{t}><f>{formula}</f>'+('' if value is None else f'<v>{escape(str(value))}</v>')+'</c>'
    if kind=='inlineStr':return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    return f'<c r="{ref}" t="{kind}"><v>{escape(str(value))}</v></c>'
def sheet(rows):return f'<worksheet xmlns="{NS}"><sheetData>'+''.join(f'<row r="{i}">'+r+'</row>' for i,r in enumerate(rows,1))+'</sheetData></worksheet>'
def build(path,date1904=False,extra=(),conf_extra=()):
    headers=['event_id','alias','local_time','value','fold']
    first=''.join(cell(chr(65+i)+'1',v) for i,v in enumerate(headers))
    rows=[first,
        cell('A2','0001')+cell('B2','0','s')+cell('C2','2026-11-01T01:30:00')+cell('D2','20.5','n')+cell('E2','0','n'),
        cell('A3','0002')+cell('B3','0','s')+cell('C3','2026-11-01T01:30:00')+cell('D3','21.5','n')+cell('E3','1','n'),
        cell('A4','0003')+cell('B4','0','s')+cell('C4','2026-03-08T02:30:00')+cell('D4','22','n'),
        cell('A5','0004')+cell('B5','0','s')+cell('C5','2026-11-02T12:00:00')+cell('D5',None,formula='10+10'),
        cell('A6','0005')+cell('B6','0','s')+cell('C6','60','n')+cell('D6','20','n'),
        cell('A7','0006')+cell('B7','1','s')+cell('C7','2026-11-02T12:00:00')+cell('D7','25','n'),
        cell('A8','0007')+cell('B8','0','s')+cell('C8','46328.5','n')+cell('D8','20',formula='10+10'),
        cell('A9','0008')+cell('B9','0','s')+cell('C9','2026-11-01T01:30:00')+cell('D9','23','n'),
        cell('A10','0009')+cell('B10','0','s')+cell('C10','2026-11-02T12:00:00')+cell('D10','#DIV/0!','e',formula='1/0')]+list(extra)
    conf=[cell('A1','alias')+cell('B1','device')+cell('C1','unit'),cell('A2','sensor1')+cell('B2','dev1')+cell('C2','C'),cell('A3','conflict')+cell('B3','dev1')+cell('C3','C'),cell('A4','conflict')+cell('B4','dev2')+cell('C4','C')]+list(conf_extra)
    types='<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'
    package_rel='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="'+REL+'/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook=f'<workbook xmlns="{NS}" xmlns:r="{REL}"><workbookPr date1904="{1 if date1904 else 0}"/><sheets><sheet name="测量" sheetId="1" r:id="rId1"/><sheet name="仪表" sheetId="2" r:id="rId2"/></sheets></workbook>'
    rels='<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="{REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in [1,2])+f'<Relationship Id="rId3" Type="{REL}/sharedStrings" Target="sharedStrings.xml"/></Relationships>'
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for name,data in {'[Content_Types].xml':types,'_rels/.rels':package_rel,'xl/workbook.xml':workbook,'xl/_rels/workbook.xml.rels':rels,'xl/worksheets/sheet1.xml':sheet(rows),'xl/worksheets/sheet2.xml':sheet(conf),'xl/sharedStrings.xml':f'<sst xmlns="{NS}" count="9" uniqueCount="2"><si><t>sensor1</t></si><si><t>conflict</t></si></sst>'}.items():z.writestr(name,data)
    return Path(path)
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('directory');a=p.parse_args();d=Path(a.directory);d.mkdir(parents=True,exist_ok=True)
    for flag in [False,True]:print(build(d/f'measurements-{1904 if flag else 1900}.xlsx',flag))
