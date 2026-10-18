import io
import sys
from pathlib import Path
from contextlib import ExitStack, nullcontext
from unittest.mock import patch

import fitz
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ocr_pdf_rebuilder import paddle_pipeline as p


TABLE = '<table><tr><th colspan="2">ALL</th></tr><tr><td rowspan="2">MERGED</td><td>R1</td></tr><tr><td>R2</td></tr></table>'

def test_table_inline_styles_reach_pdf(tmp_path):
    source=tmp_path/'styled.pdf'
    with fitz.open() as doc:
        doc.new_page(width=300,height=400);doc.save(source)
    result={'cells':[{'category':'Table','bbox':[20,40,280,130],'__bbox_units':'pdf',
                     'text':'<table><tr><td>H<sub>2</sub>O <strong>BOLD</strong></td></tr></table>'}]}
    output=tmp_path/'text.pdf'
    with patch.object(p,'write_paddle_qc_report'):
        p.build_outputs(source,{0:result},tmp_path/'work',tmp_path/'raw',output,tmp_path/'images.pdf',tmp_path/'book.md')
    with fitz.open(output) as doc:
        spans=[span for block in doc[0].get_text('dict')['blocks'] for line in block.get('lines',[]) for span in line['spans']]
        small=next(s for s in spans if s['text']=='2')
        normal=next(s for s in spans if 'H' in s['text'])
        assert small['size']<normal['size'] and small['origin'][1]>normal['origin'][1]
        assert any('Bold' in s['font'] and 'BOLD' in s['text'] for s in spans)


def test_html_table_preserves_merged_geometry_and_markdown():
    rows = p.shared.extract_html_table_rows(TABLE)
    assert [row["cells"] for row in rows] == [["ALL", ""], ["MERGED", "R1"], ["", "R2"]]
    assert rows[0]["cell_specs"][0]["colspan"] == 2
    assert rows[1]["cell_specs"][0]["rowspan"] == 2
    assert not p.shared.is_toc_table(rows)
    markdown = p.shared.table_block_to_markdown({"text": TABLE, "source_text": TABLE, "table_rows": rows})
    assert 'colspan="2"' in markdown and 'rowspan="2"' in markdown
    assert markdown.count("MERGED") == 1
    toc = p.shared.extract_html_table_rows('<table><tr><td>Title</td><td>Text</td><td>Apparat</td></tr><tr><td>Book</td><td>12</td><td>34</td></tr></table>')
    assert not p.shared.is_toc_table(toc)
    escaped = p.shared.extract_html_table_rows(r'<table><tr><td>Frankreich\nAdresse\nan alle\nund Europa</td><td>$\nu$</td></tr></table>')
    assert escaped[0]['cells'][0] == 'Frankreich\nAdresse\nan alle\nund Europa'
    assert p.shared.normalize_text(escaped[0]['cells'][1]) == 'ν'
    escaped_md = p.shared.table_block_to_markdown({'table_rows': escaped})
    assert 'Frankreich<br>Adresse' in escaped_md
    assert escaped_md.count('\n') == 1


def test_search_cleanup_preserves_editorial_angle_brackets(tmp_path):
    original = '<em[ancipatory]» against <organized against the peop[le]» people >'
    source, output = tmp_path / 'source.pdf', tmp_path / 'search.pdf'
    buffer = io.BytesIO()
    Image.new('RGB', (300, 400), 'white').save(buffer, format='PNG')
    with fitz.open() as doc:
        doc.new_page(width=300, height=400).insert_image(fitz.Rect(0, 0, 300, 400), stream=buffer.getvalue())
        doc.save(source)
    p.shared.build_searchable_pdf(source, {0: {'cells': [
        {'category': 'Text', 'bbox': [20, 20, 280, 50], '__bbox_units': 'pdf', 'text': original}]}}, output)
    with fitz.open(output) as doc:
        assert original in doc[0].get_text()


def test_table_outputs_keep_columns_and_source_page_and_clean_search(tmp_path):
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 90, 275, 230), outline="black", width=3)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    source = tmp_path / "source.pdf"
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=400)
        page.insert_image(page.rect, stream=buffer.getvalue())
        doc.save(source)
    result = {"cells": [
        {"category": "Table", "bbox": [25, 90, 275, 230], "text": TABLE, "__bbox_units": "pdf"},
        {"category": "Text", "bbox": [25, 250, 275, 290],
         "text": '4<sup>th</sup> H<sub>2</sub>O **bold** &lt;em&gt;word&lt;/em&gt;', "__bbox_units": "pdf"}],
        "image_size": None, "md_nohf_text": "WRONG FLATTENED"}
    text, images, md, search = [tmp_path / name for name in ["text.pdf", "images.pdf", "book.md", "search.pdf"]]
    with patch.object(p, "write_paddle_qc_report"):
        p.build_outputs(source, {0: result}, tmp_path / "work", tmp_path / "raw", text, images, md, search)
    assert result["structural_source_page_preserved"]
    assert 'rowspan="2"' in md.read_text(encoding="utf-8")
    assert "WRONG FLATTENED" not in md.read_text(encoding="utf-8")
    with fitz.open(text) as doc:
        words = {w[4]: w for w in doc[0].get_text("words")}
        assert words["MERGED"][0] < words["R1"][0]
        assert words["R1"][0] == words["R2"][0]
        assert words["R1"][1] < words["R2"][1]
        assert doc[0].get_text().count("MERGED") == 1
    with fitz.open(source) as src, fitz.open(images) as doc:
        assert src[0].get_pixmap().samples == doc[0].get_pixmap().samples
    with fitz.open(search) as doc:
        extracted = doc[0].get_text()
        assert "MERGED" in extracted and "R2" in extracted
        assert "4th" in extracted and "H2O" in extracted and "word" in extracted
        assert "<sup>" not in extracted and "<em>" not in extracted and "**" not in extracted


def test_mineru_structural_fallback_keeps_text_output(tmp_path):
    source = tmp_path / 'source.pdf'
    buffer = io.BytesIO()
    Image.new('RGB', (300, 400), 'white').save(buffer, format='PNG')
    with fitz.open() as doc:
        doc.new_page(width=300, height=400).insert_image(fitz.Rect(0, 0, 300, 400), stream=buffer.getvalue())
        doc.save(source)
    results = {0: {'cells': [{'category': 'Table', 'bbox': [25, 90, 275, 230],
                              '__bbox_units': 'pdf', 'text': TABLE}]}}
    shared = p.shared
    with ExitStack() as stack:
        for name in ['OUTPUT_DIR', 'TMP_DIR', 'MINERU_OUTPUT_DIR', 'LOG_DIR']:
            path = tmp_path / name.lower(); path.mkdir()
            stack.enter_context(patch.object(shared, name, path))
        for name, value in {'SKIP_EXISTING': False, 'DELETE_INTERMEDIATE_ON_SUCCESS': False,
                            'CLEAN_TMP_ON_SUCCESS': False}.items():
            stack.enter_context(patch.object(shared, name, value))
        stack.enter_context(patch.object(shared, 'create_mineru_api_session', return_value=nullcontext(None)))
        stack.enter_context(patch.object(shared, 'build_mineru_parser_runs', return_value=[]))
        stack.enter_context(patch.object(shared, 'collect_parser_run_results', return_value=results))
        for name in ['retry_bad_pages', 'repair_forward_page_content_leaks']:
            stack.enter_context(patch.object(shared, name, side_effect=lambda path, pages, *a, **kw: pages))
        for name in ['prepare_resumable_job_workspace', 'suppress_blank_page_outputs',
                     'write_completed_output_state', 'write_mineru_qc_report']:
            stack.enter_context(patch.object(shared, name))
        output = shared.process_pdf(source, 1, 1)
    with fitz.open(output['output_text_pdf']) as doc:
        assert 'MERGED' in doc[0].get_text()
    with fitz.open(source) as src, fitz.open(output['output_image_pdf']) as doc:
        assert src[0].get_pixmap().samples == doc[0].get_pixmap().samples


def test_structural_qc_can_be_read_back_with_multiple_suspect_pages(tmp_path):
    suspects = {n: ['table_structure'] for n in range(1, 13)}
    with patch.object(p, 'LOG_DIR', tmp_path), patch.object(p.shared, 'collect_qc_suspect_pages', return_value=suspects):
        p.write_paddle_qc_report(tmp_path/'book.pdf',tmp_path/'raw',{},
                                [(300,400,[])]*12,tmp_path/'text.pdf',None,[],{'page_count':12})
    qc = p.shared.read_checkpoint(tmp_path/'book_qc.json')
    assert qc is not None
    assert qc['suspect_pages']['12'] == ['table_structure']


def test_cell_newlines_survive_text_and_search_rendering(tmp_path):
    source=tmp_path/'source.pdf'
    buffer=io.BytesIO();Image.new('RGB',(300,400),'white').save(buffer,format='PNG')
    with fitz.open() as doc:
        doc.new_page(width=300,height=400).insert_image(fitz.Rect(0,0,300,400),stream=buffer.getvalue());doc.save(source)
    results={0:{'cells':[{'category':'Table','bbox':[25,90,275,230],'__bbox_units':'pdf',
                         'text':r'<table><tr><td>Alpha\nBeta</td></tr></table>'}]}}
    with patch.object(p,'write_paddle_qc_report'):
        p.build_outputs(source,results,tmp_path/'work',tmp_path/'raw',tmp_path/'text.pdf',tmp_path/'images.pdf',tmp_path/'book.md',tmp_path/'search.pdf')
    for name in ['text.pdf','search.pdf']:
        with fitz.open(tmp_path/name) as doc:
            words={w[4]:w for w in doc[0].get_text('words')}
            assert words['Alpha'][1] < words['Beta'][1]
