"""Source-bound semantic structure shared by both providers and all outputs."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import TypedDict

from markdown_it import MarkdownIt

SCHEMA_VERSION = 1
LAYOUT_VERSION = 4
COORDINATES = 'pdf-points-top-left'

class Block(TypedDict):
    id: str | None
    order: int
    role: str
    bbox: list[float]
    raw_text: str
    accepted_text: str
    lines: list[dict]
    table: list[dict]
    issues: list[str]
    formula: dict | None

class Page(TypedDict):
    index: int
    width: float
    height: float
    text_policy: str
    preserve_source_visual: bool
    blocks: list[Block]

class Document(TypedDict):
    schema: str
    schema_version: int
    layout_version: int
    coordinates: str
    provider: str
    source: str
    pages: list[Page]
    references: list[dict]

@dataclass
class Inline:
    text: str
    styles: list[str] = field(default_factory=list)
    reference: str | None = None
    bbox: list[float] | None = None

@dataclass
class Line:
    inlines: list[Inline] = field(default_factory=list)
    break_after: str = 'soft'
    role: str = 'body'
    indent: float = 0.0
    hanging_indent: float = 0.0
    bbox: list[float] | None = None
    evidence: str = 'markup'
    positioned_spans: list[dict] = field(default_factory=list)
    anchor: str | None = None

class MarkupParser(HTMLParser):
    """HTML events and CommonMark events feed the same line/run builder."""
    STYLES = {'b':'strong', 'strong':'strong', 'i':'italic', 'em':'italic',
              'sup':'sup', 'sub':'sub', 'del':'strike', 's':'strike',
              'u':'underline', 'mark':'mark', 'code':'code'}
    BLOCKS = {'p':'body', 'div':'body', 'blockquote':'quote', 'li':'list',
              'pre':'code', 'h1':'heading1', 'h2':'heading2', 'h3':'heading3',
              'h4':'heading4', 'h5':'heading5', 'h6':'heading6'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = [Line()]
        self.stack = []
        self.issues = []
        self.code_block = False
        self.role = 'body'
        self.reference = None

    def new_line(self, kind):
        indent=self.lines[-1].indent+self.lines[-1].hanging_indent if kind=='hard' else (18.0 if self.role=='quote' else 0.0)
        self.lines[-1].break_after = kind
        self.lines.append(Line(role=self.role, indent=indent))

    def add(self, text, extra=()):
        if not text: return
        styles = list(dict.fromkeys([s for _, s in self.stack if s] + list(extra)))
        parts = text.split('\n')
        for n, part in enumerate(parts):
            if n: self.new_line('hard' if self.code_block else 'soft')
            self.lines[-1].role = self.role
            self.lines[-1].inlines.append(Inline(part, styles.copy(), self.reference))

    def handle_data(self, data): self.add(data)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag=='br': self.new_line('hard'); return
        if tag=='a' and not any(k in attrs for k in ('href','id','name')):
            self.add(self.get_starttag_text());self.issues.append('unknown_tag:a');return
        if tag in self.BLOCKS:
            if self.lines[-1].inlines: self.new_line('paragraph')
            self.role = self.BLOCKS[tag]
            self.lines[-1].role = self.role
            self.lines[-1].anchor=attrs.get('id')
            if attrs.get('data-role')=='footnote': self.role='footnote';self.lines[-1].role='footnote'
            if tag=='pre': self.code_block=True
            if tag=='blockquote' and not re.search(r'margin-left\s*:',attrs.get('style','')): self.lines[-1].indent=18.0
            # Only numeric point units are accepted; arbitrary web CSS is not layout evidence.
            for prop, value in re.findall(r'(margin-left|text-indent)\s*:\s*(-?[\d.]+)pt', attrs.get('style','')):
                if prop=='margin-left': self.lines[-1].indent += float(value)
                else:
                    self.lines[-1].indent += float(value)
                    self.lines[-1].hanging_indent = -float(value)
        if tag=='a': self.reference = attrs.get('href')
        if tag in self.STYLES or tag in self.BLOCKS or tag in {'a','span','small','font'}:
            self.stack.append((tag, self.STYLES.get(tag)))
        else:
            # Preserve editorial <word> notation rather than interpreting it as HTML.
            self.add(self.get_starttag_text())
            self.issues.append('unknown_tag:'+tag)

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag!='br' and any(t==tag for t,_ in self.stack): self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((n for n in range(len(self.stack)-1,-1,-1) if self.stack[n][0]==tag), None)
        if index is None:
            self.add('</'+tag+'>'); self.issues.append('unmatched_tag:'+tag); return
        del self.stack[index:]
        if tag=='a': self.reference=None
        if tag in self.BLOCKS:
            self.new_line('paragraph'); self.role='body'; self.code_block=False

    def result(self):
        while self.lines and not self.lines[-1].inlines: self.lines.pop()
        if self.stack: self.issues.extend('unclosed_tag:'+t for t,_ in self.stack)
        return [asdict(line) for line in self.lines], self.issues

def _semantic_inline_rule(state, silent):
    """Explicit math, script and note delimiters; CommonMark owns emphasis."""
    tail = state.src[state.pos:]
    match = re.match(r'\[\^([^\]\n]+)\]', tail)
    kind, value, length = None, None, 0
    if match: kind,value,length='note',match[1],len(match[0])
    elif (script := re.match(r'([_^])\{([^{}\n]+)\}',tail)):
        kind,value,length=('sup' if script[1]=='^' else 'sub'),script[2],len(script[0])
    else:
        for opener, closer, style in [('\\(', '\\)', 'math'), ('\\[','\\]','math'),
                                       ('$$','$$','math'),('$','$','math'),('^','^','sup'),('~','~','sub')]:
            if not tail.startswith(opener) or tail.startswith('~~'): continue
            end=tail.find(closer,len(opener))
            if end<=len(opener): continue
            body=tail[len(opener):end]
            if '\n' in body: continue
            if opener=='$' and not re.search(r'[=+*/_^{}\\]|\d|\b[A-Za-z]\b',body): continue
            kind,value,length=style,body,end+len(closer); break
    if kind is None: return False
    if not silent:
        token=state.push('semantic_inline','',0); token.content=value; token.meta={'kind':kind}
    state.pos+=length
    return True

_MD = MarkdownIt('commonmark', {'html':True}).enable(['strikethrough','table'])
_MD.inline.ruler.before('escape','semantic_inline',_semantic_inline_rule)

def parse_markup(text: str, *, inline=False):
    parser=MarkupParser()
    if not inline:
        text=re.sub(r'(?m)^\[\^([^\]\n]+)\]:[ \t]*(.+)$',lambda m:'<p id="footnote-'+escape(m[1],quote=True)+'" data-role="footnote">'+escape(m[1])+'. '+m[2]+'</p>',text)
    tokens=_MD.parseInline(text) if inline else _MD.parse(text)
    def visit(tokens):
        list_depth=0;lists=[]
        for t in tokens:
            if t.type=='inline': visit(t.children or [])
            elif t.type in {'html_inline','html_block'}: parser.feed(t.content)
            elif t.type=='text':
                if re.search(r'</?(?:strong|em|sup|sub|br)\b[^>]*>',t.content): parser.feed(t.content)
                else: parser.add(t.content)
            elif t.type=='code_inline': parser.add(t.content,('code',))
            elif t.type=='semantic_inline':
                if t.meta['kind']=='note':
                    old=parser.reference; parser.reference='#footnote-'+t.content
                    parser.add(t.content,('sup',)); parser.reference=old
                else: parser.add(t.content,(t.meta['kind'],))
            elif t.type in {'softbreak','hardbreak'}: parser.new_line('soft' if t.type=='softbreak' else 'hard')
            elif t.type in {'fence','code_block'}:
                parser.role='code'; parser.code_block=True; parser.add(t.content.rstrip('\n'),('code',))
                parser.new_line('paragraph'); parser.role='body'; parser.code_block=False
            elif t.type in {'bullet_list_open','ordered_list_open'}:
                list_depth+=1;lists.append(int(t.attrGet('start') or 1) if t.type=='ordered_list_open' else None)
            elif t.type in {'bullet_list_close','ordered_list_close'}:
                list_depth-=1;lists.pop();parser.role='body'
            elif t.type=='list_item_open':
                parser.role='list'; parser.lines[-1].role='list'; parser.lines[-1].hanging_indent=14.0
                parser.lines[-1].indent=max(0,list_depth-1)*14.0
                parser.add(str(lists[-1])+'. ' if lists and lists[-1] is not None else '• ')
                if lists and lists[-1] is not None: lists[-1]+=1
            elif t.type=='paragraph_open':
                if parser.lines[-1].inlines and not (list_depth and parser.role=='list'): parser.new_line('paragraph')
                if not list_depth and parser.role not in {'quote','list'}: parser.role='body'
            elif t.type=='paragraph_close': parser.new_line('paragraph')
            elif t.type=='blockquote_open': parser.role='quote'; parser.lines[-1].indent=18.0
            elif t.type=='blockquote_close': parser.role='body'
            elif t.type=='heading_open': parser.role='heading'+t.tag[1:]
            elif t.type=='heading_close': parser.new_line('paragraph'); parser.role='body'
            elif t.type.endswith('_open') and t.tag in {'em','strong','s','a'}:
                parser.stack.append((t.tag,parser.STYLES.get(t.tag)))
                if t.tag=='a': parser.reference=t.attrGet('href')
            elif t.type.endswith('_close') and t.tag in {'em','strong','s','a'}:
                parser.handle_endtag(t.tag)
            elif t.type=='image': parser.add(t.content)
            elif t.type=='hr': parser.new_line('paragraph')
    visit(tokens)
    parser.close()
    return parser.result()

def line_text(line): return ''.join(x['text'] for x in line['inlines'])

def plain_text(lines):
    return ''.join(line_text(l)+ (' ' if l['break_after']=='soft' else '\n') for l in lines).rstrip()

def inline_html(inlines):
    tags={'strong':'strong','italic':'em','sup':'sup','sub':'sub','strike':'del',
          'underline':'u','mark':'mark','code':'code','math':'span'}
    parts=[]
    for run in inlines:
        value=escape(run['text'])
        for style in reversed(run['styles']):
            tag=tags.get(style)
            if tag: value=f'<{tag}>{value}</{tag}>'
        if run.get('reference'): value=f'<a href="{escape(run["reference"],quote=True)}">{value}</a>'
        parts.append(value)
    return ''.join(parts)

def lines_html(lines):
    groups=[];group=[]
    for line in lines:
        group.append(line)
        if line['break_after']=='paragraph': groups.append(group);group=[]
    if group: groups.append(group)
    result=[]
    for group in groups:
        first=group[0]
        tag='blockquote' if first['role']=='quote' else 'p'
        if first['role'].startswith('heading'): tag='h'+first['role'][-1]
        if first['role']=='code': tag='pre'
        style=''
        if first['indent'] or first['hanging_indent']:
            style=f' style="margin-left:{first["indent"]+first["hanging_indent"]:g}pt;text-indent:{-first["hanging_indent"]:g}pt"'
        anchor=' id="'+escape(first['anchor'],quote=True)+'"' if first.get('anchor') else ''
        parts=[]
        geometric=any(l.get('bbox') for l in group)
        for n,line in enumerate(group):
            content=inline_html(line['inlines'])
            if geometric:
                content=f'<span style="display:block;margin-left:{line["indent"]:g}pt">{content}</span>'
            parts.append(content)
            if n<len(group)-1 and not geometric:
                parts.append(('\n' if tag=='pre' else '<br>') if line['break_after']=='hard' else ' ')
        result.append(f'<{tag}{"" if geometric else style}{anchor}>'+''.join(parts)+f'</{tag}>')
    return '\n'.join(result)

def ensure_structure(block, helpers):
    signature=(block.get('text'),block.get('source_text'),block.get('left'),block.get('top'),block.get('width'),block.get('height'))
    if block.get('_semantic_signature')!=signature:
        block['semantic']=structure_block(block,helpers)
        block['_semantic_signature']=signature
    return block['semantic']

def normalize_runs(lines, helpers):
    for line in lines:
        normalized=[]
        for run in line['inlines']:
            if 'math' in run['styles']:
                source=run['text'];run['text']=helpers.linearize_latex_formula(source)
                run['formula']={'source':source,'display':run['text'],'search':run['text']}
                # The formula converter leaves unrepresentable Unicode scripts
                # (for example H^{em2}) as H^em2. Retain their actual script style.
                position=0
                for match in re.finditer(r'([_^])(?:\{([^{}]+)\}|([A-Za-z0-9]+))',run['text']):
                    if match.start()>position: normalized.append({**run,'text':run['text'][position:match.start()]})
                    normalized.append({**run,'text':match[2] or match[3],
                                       'styles':[*run['styles'],'sup' if match[1]=='^' else 'sub']})
                    position=match.end()
                if position:
                    if position<len(run['text']): normalized.append({**run,'text':run['text'][position:]})
                    continue
            elif 'code' not in run['styles']:
                # CommonMark can unescape a provider's \_{...} into a text
                # token. Unrepresentable Unicode scripts must still become
                # semantic runs, just like their unescaped TeX counterparts.
                # Split before plain-text normalization: HTML text tokens may
                # still carry the backslash, which that normalizer discards.
                text=helpers.replace_ocr_latex_hallucinations(helpers.normalize_ocr_symbol_confusables(run['text']))
                position=0
                for match in re.finditer(r'\\?([_^])\{([^{}\n]+)\}',text):
                    if match.start()>position:
                        normalized.append({**run,'text':helpers.normalize_safe_latex_markup(text[position:match.start()])})
                    normalized.append({**run,'text':helpers.normalize_safe_latex_markup(match[2]),
                        'styles':list(dict.fromkeys([*run['styles'],'sup' if match[1]=='^' else 'sub']))})
                    position=match.end()
                if position:
                    if position<len(text):
                        normalized.append({**run,'text':helpers.normalize_safe_latex_markup(text[position:])})
                    continue
                run['text']=helpers.normalize_safe_latex_markup(text)
            normalized.append(run)
        line['inlines']=normalized

def transfer_styles(source, geometric):
    chars=[(ch,r['styles'],r.get('reference')) for l in source for r in l['inlines'] for ch in r['text'] if not ch.isspace()]
    if ''.join(c[0] for c in chars)!=key_text(plain_text(geometric)): return
    position=0
    for line in geometric:
        runs=[]
        for ch in line_text(line):
            style,ref=(chars[position][1:] if position<len(chars) else ([],None))
            if not ch.isspace(): position+=1
            if runs and runs[-1]['styles']==style and runs[-1].get('reference')==ref: runs[-1]['text']+=ch
            else: runs.append({'text':ch,'styles':style.copy(),'reference':ref,'bbox':None})
        line['inlines']=runs

def remove_unproven_indent(text):
    if re.search(r'<pre\b|^\s*```',text,re.M): return text
    return re.sub(r'(?m)^[ \t]+([^\n]*)',lambda m:m[0] if re.match(r'(?:[-+*]|\d+[.)])\s|>',m[1]) else m[1],text)

def structure_block(block, helpers):
    """Use final accepted text, retaining markup when it has the same content."""
    raw=str(block.get('source_text') or block.get('text') or '')
    accepted=str(block.get('text') or '')
    source = raw if helpers.normalize_markdown_text(raw)==helpers.normalize_markdown_text(accepted) else accepted
    # Indentation in raw OCR alone is not evidence of a code block.
    if block.get('category')!='Code': source=remove_unproven_indent(source)
    lines,issues=([],[]) if block.get('category') in {'Table','Picture','ImageFallback'} else parse_markup(source)
    normalize_runs(lines,helpers)
    category=block.get('category','Text')
    table=[]
    if category=='Table':
        rows=helpers.extract_table_rows(raw) or helpers.table_rows_for_block({**block,'semantic':{}})
        for rowno,row in enumerate(rows):
            specs=row.get('cell_specs') or [dict(col=i,text=v,colspan=1,rowspan=1) for i,v in enumerate(row.get('cells',[]))]
            cells=[]
            for cell in specs:
                cell_lines, cell_issues=parse_markup(remove_unproven_indent(str(cell.get('raw_html',cell['text']))))
                normalize_runs(cell_lines,helpers)
                for l in cell_lines:
                    if l['break_after']=='soft': l['break_after']='hard'
                cells.append({**cell,'lines':cell_lines,'raw_text':cell['text'],'text':plain_text(cell_lines)})
                issues+=cell_issues
            table.append({'row':rowno,'cells':cells,'column_count':len(row.get('cells',[]))})
    x,y=float(block.get('left',0)),float(block.get('top',0))
    bbox=[x,y,x+float(block.get('width',0)),y+float(block.get('height',0))]
    geometric=[]
    for item in block.get('line_info') or []:
        b=item.get('bbox')
        if not b or len(b)!=4: continue
        sx,sy=block.get('bbox_scale_x',1),block.get('bbox_scale_y',1)
        b=[b[0]*sx,b[1]*sy,b[2]*sx,b[3]*sy]
        if not (bbox[0]-2<=b[0]<b[2]<=bbox[2]+2 and bbox[1]-2<=b[1]<b[3]<=bbox[3]+2): continue
        gl,gi=parse_markup(str(item.get('text') or ''),inline=True)
        if len(gl)!=1: continue
        normalize_runs(gl,helpers)
        gl[0].update(bbox=b,evidence='provider-line',indent=max(0,b[0]-x),break_after='hard')
        spans=[]
        for span in item.get('spans') or []:
            sb=span.get('bbox')
            if sb and len(sb)==4:
                sl,_=parse_markup(str(span.get('text') or ''),inline=True)
                normalize_runs(sl,helpers)
                spans.append({'text':plain_text(sl),'bbox':[sb[0]*sx,sb[1]*sy,sb[2]*sx,sb[3]*sy]})
        if spans and key_text(''.join(s['text'] for s in spans))==key_text(line_text(gl[0])):
            gl[0]['positioned_spans']=spans
        geometric+=gl
    # Accept line geometry only if the complete accepted block is represented.
    key=lambda t: re.sub(r'\s+','',helpers.normalize_markdown_text(t))
    if geometric and key(plain_text(geometric))==key(plain_text(lines)):
        transfer_styles(lines,geometric)
        # Keep semantic runs from the source when line text can be matched exactly.
        if len(lines)==len(geometric):
            for l,g in zip(lines,geometric):
                if key(line_text(l))==key(line_text(g)): g['inlines']=l['inlines'];g['role']=l['role']
        lines=geometric
    elif category not in {'Table','Formula','Picture','ImageFallback'}:
        issues.append('line_geometry_unavailable_or_incomplete')
    if category in {'Title','Section-header'}:
        for l in lines: l['role']='heading1' if category=='Title' else 'heading2'
    if category=='List-item':
        for l in lines: l['role']='list';l['hanging_indent']=14.0
    return {'id':block.get('source_id'), 'order':block.get('reading_order',block.get('order',0)),
            'manual_correction_evidence':block.get('manual_correction_evidence'),
            'manual_editorial_note':bool(block.get('manual_editorial_note')),
            'role':category,'bbox':bbox,'raw_text':raw,'accepted_text':accepted,'lines':lines,
            'table':table,'issues':list(dict.fromkeys(issues)),
            'formula':{'source':raw,'display':accepted,'search':re.sub(r'\(([^()\s]+)\)/\(([^()\s]+)\)',r'\1/\2',helpers.linearize_latex_formula(raw))} if category=='Formula' else None}

def key_text(text): return re.sub(r'\s+','',text)

def attach_page(result, blocks, page_index, width, height, helpers, provider):
    suppressed=bool(result.get('ocr_text_suppressed') or (any(b['category']=='ImageFallback' for b in blocks) and not result.get('visual_fallback_text_preserved')))
    for n,block in enumerate(blocks):
        if not block.get('source_id'): block['source_id']=f'p{page_index+1:04d}-legacy-{int(block.get("order",n))+1:04d}'
        semantic=ensure_structure(block,helpers)
        semantic.update(id=block['source_id'],order=block.get('reading_order',block.get('order',n)))
    for block in blocks:
        block['semantic']['layout']={k:v for k,v in block.items() if not k.startswith('_') and k not in {'semantic','source_cell'}}
    page={'index':page_index,'width':width,'height':height,'text_policy':'source_only' if suppressed else 'accepted',
          'preserve_source_visual':bool(result.get('structural_source_page_preserved') or result.get('image_fallback_path')),
          'blocks':[b['semantic'] for b in blocks],
          'layout_exclusions':result.get('layout_exclusions', []),
          'speaker_column_policy':result.get('speaker_column_policy'),
          'manual_correction':result.get('manual_correction')}
    result['document_page']=page
    # Search uses accepted blocks, not the unfiltered provider cells.
    result['accepted_blocks']=blocks
    return page

def write_document(path, pages, provider, source):
    doc={'schema':'ocr-document','schema_version':SCHEMA_VERSION,'layout_version':LAYOUT_VERSION,
         'coordinates':COORDINATES,'provider':provider,'source':str(source),'pages':pages}
    anchors={l['anchor']:b['id'] for p in pages for b in p['blocks'] for l in b['lines'] if l.get('anchor')}
    doc['references']=[{'source':b['id'],'target':anchors.get(r['reference'][1:]),'reference':r['reference'],
                        'resolved':r['reference'][1:] in anchors}
                       for p in pages for b in p['blocks'] for l in b['lines'] for r in l['inlines']
                       if r.get('reference','') and r['reference'].startswith('#')]
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(doc,ensure_ascii=False),encoding='utf-8');temporary.replace(path)

def page_markdown(page):
    if page['text_policy']=='source_only': return f'[PDF 第 {page["index"]+1} 页：保留原图，本页 OCR 文字未采用。]'
    output=[]
    for block in page['blocks']:
        if block['role'] in {'Page-header','Page-footer','Picture','ImageFallback'}: continue
        if block['table']:
            rows=[]
            for row in block['table']:
                parts=[]
                for c in row['cells']:
                    tag='th' if c.get('header') else 'td'
                    parts.append(f'<{tag} colspan="{c.get("colspan",1)}" rowspan="{c.get("rowspan",1)}">{lines_html(c["lines"])}</{tag}>')
                rows.append('<tr>'+''.join(parts)+'</tr>')
            output.append('<table>\n'+'\n'.join(rows)+'\n</table>')
        elif block['formula']:
            expression=block['formula']['source'].strip()
            for opener,closer in [('$$','$$'),('\\[','\\]'),('\\(','\\)'),('$','$')]:
                if expression.startswith(opener) and expression.endswith(closer):
                    expression=expression[len(opener):-len(closer)].strip();break
            output.append('$$\n'+expression+'\n$$')
        else: output.append(lines_html(block['lines']))
    return '\n\n'.join(output)

def legacy_segments(text):
    lines,_=parse_markup(text,inline=True)
    result=[]
    for l in lines:
        for run in l['inlines']:
            styles=run['styles']
            style='strongitalic' if 'strong' in styles and 'italic' in styles else next(iter(styles),'normal')
            result.append({'text':run['text'],'style':style,'styles':styles,'semantic_literal':True})
    return result

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.rows=[];self.cell=None;self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='table': self.depth+=1;return
        if self.depth!=1: return
        if tag=='tr': self.rows.append([])
        elif tag in {'td','th'}:
            if not self.rows: self.rows.append([])
            self.cell={'attrs':dict(attrs),'header':tag=='th','parts':[]};self.rows[-1].append(self.cell)
        elif self.cell is not None: self.cell['parts'].append(self.get_starttag_text())
    def handle_startendtag(self,tag,attrs): self.handle_starttag(tag,attrs)
    def handle_endtag(self,tag):
        if tag=='table': self.depth-=1
        elif tag in {'td','th'}: self.cell=None
        elif tag=='tr': self.cell=None
        elif self.cell is not None: self.cell['parts'].append('</'+tag+'>')
    def handle_data(self,data):
        if self.cell is not None: self.cell['parts'].append(data)
    def handle_entityref(self,name): self.handle_data('&'+name+';')
    def handle_charref(self,name): self.handle_data('&#'+name+';')

def parse_table_rows(text, normalize, latex_commands):
    parser=TableParser();parser.feed(text);parser.close()
    rows=[];occupied=set()
    for rn,rawrow in enumerate(parser.rows):
        cells={};specs=[];col=0
        for c in rawrow:
            while (rn,col) in occupied: col+=1
            raw=''.join(c['parts'])
            raw=re.sub(r'\\(n[A-Za-z]*)',lambda m:m[0] if m[1] in latex_commands else '\n'+m[1][1:],raw)
            lines,_=parse_markup(raw)
            for l in lines:
                if l['break_after']=='soft': l['break_after']='hard'
            value=normalize(plain_text(lines))
            def span(name):
                try: return max(1,int(c['attrs'].get(name,1)))
                except ValueError: return 1
            rs,cs=span('rowspan'),span('colspan')
            specs.append({'col':col,'rowspan':rs,'colspan':cs,'text':value,'raw_html':raw,'header':c['header']})
            cells[col]=value
            occupied.update((r,k) for r in range(rn,rn+rs) for k in range(col,col+cs));col+=cs
        rows.append({'cells':cells,'cell_specs':specs,'row_index':rn,'source':'html'})
    if not rows: return []
    count=max((k+1 for _,k in occupied),default=0)
    while len(rows)<max((r+1 for r,_ in occupied),default=0):
        rows.append({'cells':{},'cell_specs':[],'row_index':len(rows),'source':'html'})
    for row in rows:
        row['cells']=[row['cells'].get(k,'') for k in range(count)]
        row.update(nonempty_cells=[c for c in row['cells'] if c],col_count=count,
                   first_col=next((i for i,c in enumerate(row['cells']) if c),0))
    return rows

def parse_markdown_table_rows(text, normalize):
    tokens=_MD.parse(text);rows=[];cells=None;cell=None
    for token in tokens:
        if token.type=='tr_open': cells=[]
        elif token.type in {'th_open','td_open'} and cells is not None:
            cell={'col':len(cells),'rowspan':1,'colspan':1,'header':token.type=='th_open','raw_html':'','text':''}
            cells.append(cell)
        elif token.type=='inline' and cell is not None:
            cell['raw_html']=token.content
            lines,_=parse_markup(token.content,inline=True);cell['text']=normalize(plain_text(lines))
        elif token.type in {'th_close','td_close'}: cell=None
        elif token.type=='tr_close' and cells is not None:
            values=[c['text'] for c in cells]
            rows.append({'cells':values,'cell_specs':cells,'row_index':len(rows),'source':'markdown',
                         'col_count':len(values),'nonempty_cells':[v for v in values if v],
                         'first_col':next((i for i,v in enumerate(values) if v),0)})
            cells=None
    return rows
