import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ocr_pdf_rebuilder.layout_policy import apply_layout_policy
from ocr_pdf_rebuilder.document_ir import attach_page
from ocr_pdf_rebuilder import paddle_pipeline as p
from ocr_pdf_rebuilder.semantic_render import placements


def block(text, x, y, w, h=12, category='Text'):
    return dict(text=text, source_text=text, source_id=f'{x}-{y}', left=x, top=y,
                width=w, height=h, category=category, order=y, line_info=[])


def speakers():
    return [block('LORENZO', 79, 110, 40), block('BASTELICA =', 79, 190, 51),
            block('PERRET =', 79, 330, 40, category='Section-header'),
            block('dit que les ouvriers ' * 8, 152, 110, 260, 65),
            block('La question de principe ' * 8, 151, 190, 261, 110)]


def test_remove_counter_sequence_but_keep_body_and_footer_numbers():
    blocks = [block(str(n), 424, 100+n*10, 8) for n in [5, 10, 15, 20]]
    blocks += [block('15', 210, 150, 9), block('20', 79, 645, 10, category='Page-footer'),
               block('5', 79, 660, 7), block('10', 240, 200, 10, category='Table')]
    r = {}; accepted = apply_layout_policy(blocks, 482, 709, r)
    assert [b['text'] for b in accepted] == ['15', '20', '5', '10']
    assert len(r['layout_exclusions']) == 4
    attach_page(r, accepted, 0, 482, 709, p.shared, 'paddle')
    assert not any('424-' == b['id'][:4] for b in r['document_page']['blocks'])


def test_a_single_margin_number_is_not_enough_evidence():
    b = block('5', 424, 200, 8)
    assert apply_layout_policy([b], 482, 709, {}) == [b]


def test_speaker_fragment_removed_only_with_independent_matching_label():
    blocks = speakers() + [block('RET = Les résolutions ' + 'texte ' * 22, 97, 330, 315, 35)]
    r = {}; out = apply_layout_policy(blocks, 482, 709, r)
    body = out[-1]
    assert body['text'].startswith('Les résolutions')
    assert body['source_text'].startswith('RET =')
    assert body['left'] >= 151
    assert body['pre_repair_bbox'][0] == 97
    assert r['structural_source_page_preserved']
    attach_page(r, out, 0, 482, 709, p.shared, 'paddle')
    rect, text = placements(body['semantic'])[0]
    assert rect.x0 >= 151 and not text.startswith('RET =')
    before = body['text']; apply_layout_policy(out, 482, 709, r)
    assert body['text'] == before
    unmatched = block('NET = Les résolutions ' + 'texte '*22, 97, 330, 315, 35)
    apply_layout_policy(speakers()+[unmatched], 482, 709, {})
    assert unmatched['text'].startswith('NET =')


def test_split_merged_speaker_on_established_column_page():
    merged = block('3] OUTINE appuie la proposition ' + 'texte '*30, 85, 265, 327, 55)
    r = {}; out = apply_layout_policy(speakers()+[merged], 482, 709, r)
    label = next(b for b in out if b.get('speaker_prefix_split') is None and b['text'] == '3] OUTINE')
    assert label['left'] == 79 and label['left']+label['width'] < merged['left']
    assert merged['text'].startswith('appuie') and merged['source_text'].startswith('3] OUTINE')
    attach_page(r, out, 0, 482, 709, p.shared, 'mineru')
    assert len([b for b in r['document_page']['blocks'] if b['accepted_text'] == '3] OUTINE']) == 1


def test_no_column_reconstruction_without_repeated_labels():
    body = block('RET = Les résolutions ' + 'texte '*22, 97, 330, 315, 35)
    r = {}; apply_layout_policy([speakers()[2], body], 482, 709, r)
    assert body['left'] == 97 and body['text'].startswith('RET =')
    assert not r.get('structural_source_page_preserved')


def test_short_response_with_small_left_bbox_error_is_separated():
    body = block("appuie cette proposition. Elle est adoptée à l'unanimité.",78,330,303)
    apply_layout_policy(speakers()+[body],482,709,{})
    assert body['left'] >= 151 and body['speaker_column_repaired']


def test_matching_partial_speaker_with_dash_is_not_repeated_in_body():
    blocks=speakers()
    blocks[2]['text']=blocks[2]['source_text']='VERRYCKEN'
    body=block('YCKEN — Les délégués ' + 'texte '*22,151,330,261,50)
    apply_layout_policy(blocks+[body],482,709,{})
    assert body['text'].startswith('Les délégués')
    assert body['source_text'].startswith('YCKEN —')


def test_source_visual_variant_uses_accepted_search_layer(tmp_path):
    import fitz
    source, search, images = [tmp_path/n for n in ['source.pdf','search.pdf','images.pdf']]
    with fitz.open() as d:
        page=d.new_page(width=300,height=400)
        page.insert_text((240,100),'15',render_mode=3)
        page.draw_rect(fitz.Rect(40,60,210,190),fill=(.9,.9,.9))
        d.save(source)
    b=p.shared.cell_to_block({'category':'Text','bbox':[40,70,210,120],
        '__bbox_units':'pdf','text':'accepted body'},0,300,400,None)
    r={'structural_source_page_preserved':True}
    attach_page(r,[b],0,300,400,p.shared,'paddle')
    p.shared.build_searchable_pdf(source,{0:r},search)
    with fitz.open() as d:
        d.new_page(width=300,height=400);d.save(images)
    p.shared.restore_structural_source_pages(search,images,{0:r})
    with fitz.open(source) as a,fitz.open(images) as b:
        assert a[0].get_pixmap().samples == b[0].get_pixmap().samples
        assert b[0].get_text().strip() == 'accepted body'
