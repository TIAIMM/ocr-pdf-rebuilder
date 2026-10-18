import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ocr_pdf_rebuilder.document_ir import parse_markup, plain_text, line_text, lines_html, page_markdown, attach_page
from ocr_pdf_rebuilder import paddle_pipeline as p
from ocr_pdf_rebuilder.semantic_render import placements, semantic_layout
import fitz
from unittest.mock import patch

def test_nested_inline_and_literal_symbols():
    lines,issues=parse_markup('**bold *italic*** H<sub>2</sub>O 4<sup>th</sup> W_G_W \\*literal\\* <word> &amp; `**code**`',inline=True)
    assert plain_text(lines)=='bold italic H2O 4th W_G_W *literal* <word> & **code**'
    italic=next(r for r in lines[0]['inlines'] if r['text']=='italic')
    assert italic['styles']==['strong','italic']
    assert next(r for r in lines[0]['inlines'] if r['text']=='2')['styles']==['sub']
    assert 'unknown_tag:word' in issues
    lines,issues=parse_markup('<a> added<br/>next',inline=True)
    assert plain_text(lines)=='<a> added\nnext'
    assert 'unknown_tag:a' in issues

def test_bare_tex_scripts_and_complex_inline_math_keep_style():
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,210,70],'__bbox_units':'pdf',
        'text':'5^{th} August $H^{em2}$ S_{F}'},0,300,400,None)
    runs=[r for l in block['semantic']['lines'] for r in l['inlines']]
    assert '^{' not in plain_text(block['semantic']['lines'])
    assert next(r for r in runs if r['text']=='th')['styles']==['sup']
    assert next(r for r in runs if r['text']=='em2')['styles']==['math','sup']
    assert next(r for r in runs if r['text']=='F')['styles']==['sub']
    assert next(r for r in runs if r['text']=='H')['formula']['source']=='H^{em2}'


def test_escaped_provider_scripts_keep_structure_and_literal_symbols():
    for source in [r'A\_{F} B\^{XYZ}', r'<p>A\_{F} B\^{XYZ}</p>',
                   r'**A\_{F} B\^{XYZ}**']:
        block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,290,90],
            '__bbox_units':'pdf','text':source},0,300,400,None)
        runs=[r for l in block['semantic']['lines'] for r in l['inlines']]
        assert plain_text(block['semantic']['lines'])=='AF BXYZ'
        assert 'sub' in next(r for r in runs if r['text']=='F')['styles']
        assert 'sup' in next(r for r in runs if r['text']=='XYZ')['styles']
    literal=p.shared.cell_to_block({'category':'Text','bbox':[10,10,290,90],
        '__bbox_units':'pdf','text':r'W_G_W \*literal\* `A\_{F}`'},0,300,400,None)
    assert plain_text(literal['semantic']['lines'])==r'W_G_W *literal* A\_{F}'


def test_real_index_name_is_correct_in_all_outputs(tmp_path):
    raw=r'东布罗夫斯基（Da\_{b}rowski [Dombrowski], Jaroslaw 1836—1871）'
    source=tmp_path/'source.pdf'
    with fitz.open() as doc:
        doc.new_page(width=300,height=400);doc.save(source)
    cell={'category':'Text','bbox':[10,20,290,110],'__bbox_units':'pdf','text':raw}
    results={0:{'cells':[cell]}};block=p.shared.cell_to_block(cell,0,300,400,None)
    blocks=p.shared.prepare_blocks([block],300,400)
    page=attach_page(results[0],blocks,0,300,400,p.shared,'paddle')
    assert 'Dąbrowski [Dombrowski]' in page_markdown(page)
    assert 'Da_{b}' not in page_markdown(page)
    output=tmp_path/'text.pdf';search=tmp_path/'search.pdf'
    p.shared.render_blocks_to_pdf_reportlab([(300,400,blocks)],output)
    p.shared.build_searchable_pdf(source,results,search)
    for path in [output,search]:
        with fitz.open(path) as doc:
            assert 'Dąbrowski' in doc[0].get_text()
            assert not p.shared.LATEX_RESIDUE_RE.search(doc[0].get_text())

def test_semantic_search_does_not_reinterpret_literal_symbols(tmp_path):
    source=tmp_path/'source.pdf';output=tmp_path/'search.pdf'
    with fitz.open() as doc:
        doc.new_page(width=300,height=400);doc.save(source)
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,290,50],'__bbox_units':'pdf',
        'text':r'W_G_W \*literal\* <word>'},0,300,400,None)
    result={};attach_page(result,[block],0,300,400,p.shared,'paddle')
    p.shared.build_searchable_pdf(source,{0:result},output)
    with fitz.open(output) as doc:
        assert doc[0].get_text().strip()=='W_G_W *literal* <word>'

def test_breaks_and_explicit_indent():
    lines,_=parse_markup('<p style="margin-left:20pt;text-indent:-8pt">first<br>second</p><p>third</p>')
    assert [line_text(l) for l in lines]==['first','second','third']
    assert [l['break_after'] for l in lines]==['hard','paragraph','paragraph']
    assert lines[0]['indent']==12 and lines[0]['hanging_indent']==8
    assert lines[1]['indent']==20
    soft,_=parse_markup('ordinary\ncontinuation')
    assert plain_text(soft)=='ordinary continuation'
    code,_=parse_markup('```\n  first\n    second\n```')
    assert code[0]['break_after']=='hard' and line_text(code[1])=='    second'

def test_markdown_html_applies_indent_to_paragraph_and_keeps_code():
    lines,_=parse_markup('<p style="margin-left:20pt;text-indent:-8pt">first<br>second</p>')
    serialized=lines_html(lines)
    assert serialized=='<p style="margin-left:20pt;text-indent:-8pt">first<br>second</p>'
    restored,_=parse_markup(serialized)
    assert restored[0]['indent']==12 and restored[0]['hanging_indent']==8
    code,_=parse_markup('```\n  first\n    second\n```')
    assert lines_html(code)=='<pre><code>  first</code>\n<code>    second</code></pre>'

def test_common_provider_structure_and_merged_cells():
    raw='<table><tr><td colspan="2">A</td></tr><tr><td rowspan="2">B</td><td>X<br>Y</td></tr><tr><td>Z</td></tr></table>'
    pages=[]
    for provider in ['paddle','mineru']:
        cell={'category':'Table','bbox':[10,10,210,150],'__bbox_units':'pdf','text':raw}
        block=p.shared.cell_to_block(cell,0,300,400,None)
        pages.append(attach_page({'cells':[cell]},[block],0,300,400,p.shared,provider))
    assert pages[0]==pages[1]
    table=pages[0]['blocks'][0]['table']
    assert table[0]['cells'][0]['colspan']==2 and table[1]['cells'][0]['rowspan']==2
    assert table[2]['cells'][0]['col']==1
    assert [t for _,t in placements(pages[0]['blocks'][0])]==['A','B','X','Y','Z']
    assert page_markdown(pages[0]).count('>B<')==1

def test_markdown_table_keeps_empty_cells_and_escaped_pipe():
    cell={'category':'Table','bbox':[10,10,210,150],'__bbox_units':'pdf',
          'text':'| A | B | C |\n|---|---|---|\n| x\\|y | | H<sub>2</sub>O |'}
    block=p.shared.cell_to_block(cell,0,300,400,None)
    table=block['semantic']['table']
    assert [c['text'] for c in table[1]['cells']]==['x|y','','H2O']
    assert [c['col'] for c in table[1]['cells']]==[0,1,2]
    assert table[0]['cells'][0]['header']
    assert next(r for l in table[1]['cells'][2]['lines'] for r in l['inlines'] if r['text']=='2')['styles']==['sub']

def test_formula_markdown_has_one_delimiter_pair():
    cell={'category':'Formula','bbox':[10,10,210,70],'__bbox_units':'pdf','text':r'$$x^{2}+y$$'}
    block=p.shared.cell_to_block(cell,0,300,400,None)
    page=attach_page({},[block],0,300,400,p.shared,'paddle')
    assert page_markdown(page)=='$$\nx^{2}+y\n$$'

def test_list_indent_is_evidence_but_cell_spaces_are_not():
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,210,150],'__bbox_units':'pdf',
        'text':'- outer\n  - inner'},0,300,400,None)
    assert block['semantic']['lines'][1]['indent']==14
    cell={'category':'Table','bbox':[10,10,210,150],'__bbox_units':'pdf',
          'text':'<table><tr><td>    ordinary</td></tr></table>'}
    table=p.shared.cell_to_block(cell,0,300,400,None)['semantic']['table']
    assert table[0]['cells'][0]['lines'][0]['role']=='body'

def test_incomplete_line_geometry_does_not_drop_text():
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,210,70],'__bbox_units':'pdf',
        'text':'first<br>second','__line_info':[{'bbox':[10,10,100,30],'text':'first'}]},0,300,400,None)
    assert [t for _,t in placements(block['semantic'])]==['first','second']
    assert 'line_geometry_unavailable_or_incomplete' in block['semantic']['issues']

def test_source_only_policy_is_shared():
    result={'ocr_text_suppressed':True}
    page=attach_page(result,[],370,300,400,p.shared,'paddle')
    assert page['text_policy']=='source_only'
    assert '未采用' in page_markdown(page)

def test_note_reference_and_geometry_indent():
    lines,_=parse_markup('text[^n] [link](#target)',inline=True)
    assert next(r for r in lines[0]['inlines'] if r['text']=='n')['reference']=='#footnote-n'
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,210,80],'__bbox_units':'pdf','text':'first\nsecond',
       '__line_info':[{'bbox':[30,10,180,30],'text':'first'},{'bbox':[10,40,180,60],'text':'second'}]},0,300,400,None)
    assert block['semantic']['lines'][0]['indent']==20
    assert [box.x0 for box,_ in placements(block['semantic'])]==[30,10]

def test_list_numbers_and_footnote_definitions_survive():
    lines,_=parse_markup('3. first\n4. second\n\n[^n]: actual note')
    assert line_text(lines[0])=='3. first' and line_text(lines[1])=='4. second'
    assert lines[-1]['anchor']=='footnote-n'
    assert lines[-1]['role']=='footnote'

def test_hanging_indent_and_scripts_are_actual_layout():
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,110,150],'__bbox_units':'pdf',
        'text':'<p style="margin-left:14pt;text-indent:-14pt">longword first second third fourth H<sub>2</sub>O</p>'},0,200,200,None)
    p.shared.reportlab_register_fonts()
    rows=semantic_layout(block,fitz.Rect(10,10,110,150),10,1.2,p.shared)
    assert rows is not None
    assert any(r[0]==24 for r in rows if r[1]>10)
    assert next(r for r in rows if r[2]=='2')[4]==7

def test_emphasis_survives_provider_wraps():
    block=p.shared.cell_to_block({'category':'Text','bbox':[10,10,210,80],'__bbox_units':'pdf','text':'**first second**',
        '__line_info':[{'bbox':[10,10,210,30],'text':'first'},{'bbox':[10,40,210,60],'text':'second'}]},0,300,400,None)
    assert all('strong' in r['styles'] for l in block['semantic']['lines'] for r in l['inlines'])

def test_render_version_change_reuses_recognition_cache(tmp_path):
    work=tmp_path/'work';raw=tmp_path/'raw';work.mkdir();raw.mkdir()
    (raw/'recognized.json').write_text('retained')
    old={'worker':{'sha256':'same worker'},'models':['same model'],
         'config':{'output_version':'old layout','backend':'vllm-server'}}
    current={**old,'config':{**old['config'],'output_version':'new layout'}}
    source={'path':'source.pdf','size':10,'sha256':'same source'}
    p.shared.write_checkpoint(work/'job_state.json',{'schema':p.CHECKPOINT_SCHEMA,'source':source,'runtime_identity':old})
    with patch.object(p,'paddle_runtime_identity',return_value=current),patch.object(p.shared,'source_file_signature',return_value=source),patch.object(p.shared,'current_package_source_identity_hash',return_value='new renderer'):
        assert p.prepare_workspace(tmp_path/'source.pdf',work,raw)
    assert (raw/'recognized.json').read_text()=='retained'
    identity={'source':source,'parser_config_hash':'configured model','runtime_identity_hash':'same model','implementation_identity_hash':'new renderer'}
    cached={**identity,'implementation_identity_hash':'old renderer','status':'complete'}
    assert p.shared.checkpoint_matches(cached,identity,status='complete')
    assert not p.shared.checkpoint_matches(cached,{**identity,'parser_config_hash':'other model'},status='complete')
