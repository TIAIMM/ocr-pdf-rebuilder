import sys
from pathlib import Path
from unittest.mock import patch
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ocr_pdf_rebuilder.output_publication import staged_products

def test_build_failure_preserves_previous_generation(tmp_path):
    paths=[tmp_path/'a.pdf',tmp_path/'b.md']
    for p in paths: p.write_bytes(b'previous')
    with pytest.raises(RuntimeError):
        with staged_products(paths) as staged:
            staged[0].write_bytes(b'new')
            raise RuntimeError('render failed')
    assert [p.read_bytes() for p in paths]==[b'previous']*2

def test_partial_publication_restores_all_previous_files(tmp_path):
    import os
    paths=[tmp_path/'a.pdf',tmp_path/'b.md']
    for p in paths: p.write_bytes(b'previous')
    replace=os.replace
    def fail_second(source,target):
        if Path(source).name=='b.md': raise OSError('file locked')
        return replace(source,target)
    with pytest.raises(OSError), patch('ocr_pdf_rebuilder.output_publication.os.replace',side_effect=fail_second):
        with staged_products(paths) as staged:
            for p in staged: p.write_bytes(b'new')
    assert [p.read_bytes() for p in paths]==[b'previous']*2
