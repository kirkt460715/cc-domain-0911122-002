import tempfile,unittest
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as E
from lab.workbooks import build,NS
class WorkbookSource(unittest.TestCase):
    def test_date_systems_and_cell_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            for flag in [False,True]:
                with ZipFile(build(Path(d)/'source.xlsx',flag)) as z:
                    self.assertEqual(E.fromstring(z.read('xl/workbook.xml')).find('{'+NS+'}workbookPr').get('date1904'),str(int(flag)))
                    cells={x.get('r'):x for x in E.fromstring(z.read('xl/worksheets/sheet1.xml')).iter('{'+NS+'}c')}
                    self.assertIsNone(cells['D5'].find('{'+NS+'}v'))
                    self.assertEqual(cells['A2'].find('.//{'+NS+'}t').text,'0001')
                    self.assertEqual(cells['E3'].find('{'+NS+'}v').text,'1')
