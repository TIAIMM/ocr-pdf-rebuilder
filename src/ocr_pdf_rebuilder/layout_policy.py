"""Evidence-based exclusions and separation of speaker and body columns."""
import re
from statistics import median


def _text(block):
    return str(block.get('text', '')).strip()


def _name(text):
    letters = ''.join(c for c in text if c.isalpha())
    return 3 <= len(letters) <= 28 and letters.isupper() and len(text) <= 40


def apply_layout_policy(blocks, width, height, result):
    """Do not use marginal counters as anchors for reflowed body text.

    A speaker column needs several independent labels and body-column boxes.
    No source PDF text is consulted to reconstruct either labels or counters.
    """
    excluded = result.setdefault('layout_exclusions', [])
    numbers = [b for b in blocks if b.get('category') not in
               {'Table', 'Page-header', 'Page-footer', 'ImageFallback'}
               and re.fullmatch(r'\d{1,3}', _text(b))
               and 5 <= int(_text(b)) <= 150 and int(_text(b)) % 5 == 0
               and b['width'] < width * .045
               and height * .08 < b['top'] < height * .92
               and (b['left'] < width * .18 or b['left'] > width * .80)]
    removed = set()
    for seed in numbers:
        column = sorted([b for b in numbers if abs(b['left'] - seed['left']) < width * .025],
                        key=lambda b: b['top'])
        values = [int(_text(b)) for b in column]
        if len(column) < 3 or sum(y - x == 5 for x, y in zip(values, values[1:])) < 2:
            continue
        for b in column:
            if id(b) not in removed:
                excluded.append({'source_id': b.get('source_id'), 'text': _text(b),
                                 'bbox': [b['left'], b['top'], b['left'] + b['width'], b['top'] + b['height']],
                                 'reason': 'marginal_counter_not_a_reflow_anchor'})
                removed.add(id(b))
    blocks = [b for b in blocks if id(b) not in removed]
    labels = [b for b in blocks if b.get('category') in {'Text', 'Section-header'}
              and b['left'] < width * .28 and b['width'] < width * .15
              and b['height'] < height * .035 and _name(_text(b))]
    if len(labels) < 3 or max(b['left'] for b in labels) - min(b['left'] for b in labels) > width * .035:
        return blocks
    bodies = [b for b in blocks if b.get('category') == 'Text' and len(_text(b)) > 80
              and width * .29 <= b['left'] <= width * .50
              and b['left'] + b['width'] > width * .72]
    if len(bodies) < 2:
        return blocks
    anchor = median(b['left'] for b in bodies)
    label_left = median(b['left'] for b in labels)
    if anchor < max(b['left'] + b['width'] for b in labels) + width * .025:
        return blocks
    result['speaker_column_policy'] = {'body_left': anchor, 'label_left': label_left,
                                      'evidence': 'independent_label_and_body_boxes'}
    result['structural_source_page_preserved'] = True
    for b in blocks:
        b['structural_source_page_preserved'] = True
    for label in labels:
        label['layout_role'] = 'speaker'
        for body in blocks:
            if body is label or body.get('category') != 'Text' or len(_text(body)) < 25:
                continue
            if abs(body['top'] - label['top']) > max(label['height'], height * .008):
                continue
            if body['left'] + body['width'] < width * .72 or body['left'] < label_left - width * .02:
                continue
            text = _text(body)
            prefix = re.match(r'^([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ ]{1,28}\s*[=—–-])\s+', text)
            label_key = ''.join(c for c in _text(label) if c.isalpha())
            prefix_key = ''.join(c for c in prefix[1] if c.isalpha()) if prefix else ''
            if len(prefix_key) >= 3 and label_key.endswith(prefix_key):
                body['speaker_prefix_removed'] = prefix[1]
                body['text'] = text[prefix.end():]
                body['line_info'] = []
            if body['left'] < anchor - width * .015:
                right = body['left'] + body['width']
                body['pre_repair_bbox'] = [body['left'], body['top'], right, body['top'] + body['height']]
                body['left'], body['width'] = anchor, right - anchor
                body['line_info'] = []
                body['speaker_column_repaired'] = True
    # A merged all-capital speaker prefix is split only on an established column page.
    for body in list(blocks):
        if body.get('category') != 'Text' or len(_text(body)) < 80 or body['left'] >= anchor - width * .06:
            continue
        if body['left'] < label_left - width * .02:
            continue
        match = re.match(r'^((?:[|\[\]0-9]+\s+)?[A-ZÀ-ÖØ-Þ]{3,}(?:\s+[A-ZÀ-ÖØ-Þ]{2,})?(?:\s*=)?)\s+(?=[a-zà-öø-ÿ])', _text(body))
        if not match:
            continue
        prefix = match[1]
        label = {k: v for k, v in body.items() if k not in {'semantic', '_semantic_groups', 'source_cell'}}
        label.update(text=prefix, source_text=prefix, source_id=str(body.get('source_id', body.get('order', ''))) + '-speaker',
                     left=label_left, width=anchor - label_left - width * .025,
                     height=median(b['height'] for b in labels), category='Text', line_info=[], layout_role='speaker')
        right = body['left'] + body['width']
        body['pre_repair_bbox'] = [body['left'], body['top'], right, body['top'] + body['height']]
        body.update(text=_text(body)[match.end():],
                    left=anchor, width=right-anchor, line_info=[], speaker_column_repaired=True,
                    speaker_prefix_split=prefix)
        blocks.insert(blocks.index(body), label)
    return blocks
