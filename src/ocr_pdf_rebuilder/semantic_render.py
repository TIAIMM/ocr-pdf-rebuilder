"""Render semantic runs without reinterpreting their literal text as markup."""
import re
from functools import lru_cache
import fitz
from .document_ir import line_text

@lru_cache(maxsize=32768)
def unit_width(text, font):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    return stringWidth(text,font,1.0)

def compiled_groups(block, helpers):
    semantic=block['semantic']
    cached=block.get('_semantic_groups')
    if cached and cached[0] is semantic: return cached[1]
    groups=[];current=None
    for line in semantic['lines']:
        if current is None: current={**line,'inlines':list(line['inlines'])}
        else: current['inlines'] += [{'text':' ','styles':[]},*line['inlines']]
        current['break_after']=line['break_after']
        if line['break_after']!='soft' or line.get('bbox'):
            groups.append(current);current=None
    if current: groups.append(current)
    for group in groups:
        role=group['role'];style=role if role.startswith('heading') else 'normal'
        tokens=[]
        for run in group['inlines']:
            styles=run['styles']
            segstyle='strongitalic' if 'strong' in styles and 'italic' in styles else ('strong' if 'strong' in styles else 'italic' if 'italic' in styles else 'code' if 'code' in styles else 'math' if 'math' in styles else 'normal')
            for seg in helpers.split_reportlab_font_runs([{'text':run['text'],'style':segstyle,'styles':styles,'semantic_literal':True}]):
                font,fake=helpers.reportlab_font_for_segment(seg,style)
                scale=0.70 if 'sup' in styles or 'sub' in styles else 1.0
                rise=0.32 if 'sup' in styles else -0.20 if 'sub' in styles else 0.0
                for piece in re.findall(r'\s+|\S+',seg['text']): tokens.append((piece,font,fake,scale,rise,styles))
        group['tokens']=tokens
    block['_semantic_groups']=(semantic,groups)
    return groups

def semantic_layout(block, rect, fontsize, line_height, helpers):
    semantic=block['semantic']
    lines=semantic['lines']
    if not lines: return []
    # Ordinary provider soft wraps form one paragraph; hard/paragraph breaks remain.
    groups=compiled_groups(block,helpers)
    y=rect.y0;rows=[]
    for group in groups:
        role=group['role']; style=role if role.startswith('heading') else 'normal'
        size=fontsize*helpers.line_font_scale(style)
        pitch=size*line_height
        own=group.get('bbox')
        x0=own[0] if own else rect.x0+group['indent']
        x1=own[2] if own else rect.x1
        if own: y=max(y,own[1])
        x=x0
        first=True
        for text,font,fake,scale,rise_scale,styles in group['tokens']:
            small=size*scale;rise=size*rise_scale
            width=unit_width(text,font)*small
            if x+width>x1+0.01 and x>x0+0.01:
                if own: return None
                y+=pitch; x=x0+group['hanging_indent']; first=False
                if text.isspace(): continue
            if x+width>x1+0.01:
                # Break only oversized tokens; all characters retain their styles.
                pieces=list(text)
            else: pieces=[text]
            for piece in pieces:
                w=unit_width(piece,font)*small
                if x+w>x1+0.01 and x>x0+0.01:
                    if own: return None
                    y+=pitch;x=x0+group['hanging_indent'];first=False
                if x+w>x1+0.01 or y+size>rect.y1+0.01: return None
                rows.append((x,y,piece,font,small,fake,rise,styles,size))
                x+=w
        y+=pitch
        if group['break_after']=='paragraph': y+=pitch*0.12
    return rows

def draw_semantic(c, page_height, rect, block, fontsize, line_height, helpers, dry_run=False):
    rows=semantic_layout(block,rect,fontsize,line_height,helpers)
    if rows is None: return False
    if dry_run: return True
    # Alignment remains a category policy; move each physical row as a unit.
    centered=block['category'] in {'Title','Section-header','Caption'} and block.get('layout_role') != 'speaker'
    widths={}
    if centered:
        for x,y,text,font,size,*_ in rows:
            widths[y]=max(widths.get(y,rect.x0),x+helpers.reportlab_text_width(text,font,size))
    for x,y,text,font,size,fake,rise,styles,base_size in rows:
        if centered: x+=max(0,(rect.x1-widths[y])/2)
        baseline=page_height-y-base_size+rise
        width=helpers.reportlab_text_width(text,font,size)
        if 'mark' in styles:
            c.saveState();c.setFillColorRGB(1,1,0.65);c.rect(x,baseline-size*0.15,width,size,fill=1,stroke=0);c.restoreState()
        helpers.reportlab_draw_text(c,x,baseline,text,font,size,fake)
        if 'strike' in styles or 'underline' in styles:
            offset=size*0.3 if 'strike' in styles else -size*0.12
            c.line(x,baseline+offset,x+width,baseline+offset)
    return True

def placements(semantic):
    """All accepted text is emitted once, including lines without coordinates."""
    bbox=semantic['bbox']
    if semantic['formula']:
        return [(fitz.Rect(bbox),semantic['formula']['search'])]
    if semantic['table']:
        table=semantic['table']; count=max((r['column_count'] for r in table),default=1)
        out=[]
        for r in table:
            for c in r['cells']:
                left=bbox[0]+(bbox[2]-bbox[0])*c['col']/count
                right=left+(bbox[2]-bbox[0])*c.get('colspan',1)/count
                top=bbox[1]+(bbox[3]-bbox[1])*r['row']/len(table)
                bottom=top+(bbox[3]-bbox[1])*c.get('rowspan',1)/len(table)
                ls=c['lines']
                for i,l in enumerate(ls):
                    out.append((fitz.Rect(left,top+(bottom-top)*i/len(ls),right,top+(bottom-top)*(i+1)/len(ls)),line_text(l)))
        return out
    lines=semantic['lines'];out=[]
    for i,l in enumerate(lines):
        if l.get('positioned_spans'):
            out.extend((fitz.Rect(s['bbox']),s['text']) for s in l['positioned_spans'] if s['text'].strip())
            continue
        if l.get('bbox'): rect=fitz.Rect(l['bbox'])
        else:
            height=(bbox[3]-bbox[1])/max(1,len(lines))
            rect=fitz.Rect(bbox[0]+l['indent'],bbox[1]+i*height,bbox[2],bbox[1]+(i+1)*height)
        if line_text(l).strip(): out.append((rect,line_text(l)))
    return out
