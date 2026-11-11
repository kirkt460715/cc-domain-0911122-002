import tempfile
from pathlib import Path
from zipfile import ZipFile
from .workbooks import build
with tempfile.TemporaryDirectory() as d:
    for flag in [False,True]:
        with ZipFile(build(Path(d)/'source.xlsx',flag)) as z:print('日期体系',1904 if flag else 1900,'文件成员',z.namelist())
