import copy
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import fitz
import pytest
from ocr_pdf_rebuilder.manual_corrections import apply_manual_corrections, source_binding, manifest_revision, correction_path
from ocr_pdf_rebuilder.document_ir import attach_page, page_markdown
from ocr_pdf_rebuilder import paddle_pipeline as p


def correction(tmp_path):
    source=tmp_path/'source.pdf'
    with fitz.open() as doc:
        doc.new_page(width=300,height=400);doc.new_page(width=300,height=400);doc.save(source)
    cells=[{'category':'Text','bbox':[10,50,290,300],'__bbox_units':'pdf','text':'Verified edition reference.',
            '__source_id':'manual-body','__manual_correction_evidence':{'text_origin':'edition-reference','positioning_precision':'block'}},
           {'category':'Caption','bbox':[10,10,290,40],'__bbox_units':'pdf','text':'Reference, not a diplomatic transcription.',
            '__source_id':'manual-note','__manual_editorial_note':True}]
    manifest=tmp_path/'manual.json'
    manifest.write_text(json.dumps({'schema':1,'revision':'review-1','source':source_binding(source),
        'pages':[{'page_number':1,'cells':cells,'evidence':{'source_pages':[2],'text_origin':'edition-reference'}}]}))
    return source,manifest


def test_exact_source_page_override_preserves_unrelated_and_cache(tmp_path):
    source,manifest=correction(tmp_path)
    raw={0:{'cells':[{'text':'hallucinated','__source_id':'bad'}],'image_fallback_path':'old.png',
            'ocr_text_suppressed':True,'md_nohf_text':'hallucinated'},1:{'cells':[{'text':'genuine'}]}}
    original=copy.deepcopy(raw);accepted=apply_manual_corrections(source,copy.deepcopy(raw),manifest)
    assert raw==original and accepted[1]==original[1]
    assert 'image_fallback_path' not in accepted[0] and not accepted[0]['ocr_text_suppressed']
    assert accepted[0]['md_nohf_text']=='' and accepted[0]['structural_source_page_preserved']
    assert accepted[0]['manual_correction']['rejected_source_ids']==['bad']
    assert accepted[0]['manual_correction']['human_verified'] is False
    source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(RuntimeError,match='do not match'):apply_manual_corrections(source,raw,manifest)


def test_template_is_review_alert_not_automatic_deletion(tmp_path):
    pangram='The quick brown fox jumps over the lazy dog.'
    raw={0:{'cells':[{'text':pangram}]},1:{'cells':[{'text':'Seite der Handschrift'},{'text':pangram}]}}
    accepted=apply_manual_corrections(tmp_path/'missing.pdf',raw,tmp_path/'missing.json')
    assert 'ocr_quality_alerts' not in accepted[0]
    assert accepted[1]['ocr_quality_alerts'][0]['requires_review']
    assert accepted[1]['cells'][1]['text']==pangram


def test_common_structure_and_search_excludes_editorial_note(tmp_path):
    source,manifest=correction(tmp_path);pages=[]
    for provider in ['paddle','mineru']:
        results=apply_manual_corrections(source,{0:{}},manifest)
        blocks=p.shared.blocks_from_page_result(results[0],300,400)
        pages.append(attach_page(results[0],blocks,0,300,400,p.shared,provider))
        output=tmp_path/(provider+'.pdf');p.shared.build_searchable_pdf(source,results,output)
        with fitz.open(output) as doc:
            assert doc[0].get_text().strip()=='Verified edition reference.'
        assert 'diplomatic transcription' in page_markdown(pages[-1])
        assert pages[-1]['preserve_source_visual'] and pages[-1]['manual_correction']['text_origin']=='edition-reference'
    assert pages[0]==pages[1]


def test_revision_changes_without_model_cache_change(tmp_path):
    _,manifest=correction(tmp_path);before=manifest_revision(manifest)
    manifest.write_text(manifest.read_text()+'\n')
    assert manifest_revision(manifest)!=before
    assert correction_path(tmp_path/'raw'/'book')==tmp_path/'raw'/'manual-corrections'/'book.json'
    assert not correction_path(tmp_path/'raw'/'book').is_relative_to(tmp_path/'raw'/'book')


def test_long_reference_reflows_with_readable_leading():
    cell={'category':'Text','bbox':[10,50,290,330],'__bbox_units':'pdf',
          'text':'<p>'+('Reference text about the source. '*90)+'</p>',
          '__manual_correction_evidence':{'text_origin':'edition-reference'}}
    blocks=p.shared.prepare_blocks([p.shared.cell_to_block(cell,0,300,400,None)],300,400)
    assert blocks[0]['manual_reference_layout'] and blocks[0]['line_height']>=1.2
    assert not p.shared.reportlab_page_fit_failures(400,blocks)
