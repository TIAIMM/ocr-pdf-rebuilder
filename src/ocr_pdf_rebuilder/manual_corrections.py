"""Source-bound, explicit page corrections kept outside model caches."""
import json
import re
from pathlib import Path

MANIFEST_NAME = 'manual-page-corrections.json'
PANGRAM = re.compile(r'\bthe\s+quick\s+brown\s+fox\s+jumps\s+over\s+the\s+lazy\s+dog\b', re.I)


def correction_path(raw_dir):
    """Keep deliberate corrections outside removable recognition job folders."""
    raw_dir=Path(raw_dir)
    return raw_dir.parent / 'manual-corrections' / (raw_dir.name + '.json')


def source_binding(path):
    path=Path(path);stat=path.stat()
    return {'filename':path.name,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def manifest_revision(path):
    path=Path(path)
    if not path.is_file():return None
    stat=path.stat()
    return {'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def manuscript_placeholder_alert(result):
    texts=[str(c.get('text') or c.get('content') or '') for c in result.get('cells',[])]
    if any('handschrift' in t.casefold() for t in texts) and any(PANGRAM.search(t) for t in texts):
        return {'kind':'possible_model_template_on_manuscript','requires_review':True}
    return None


def apply_manual_corrections(pdf_path, page_results, manifest_path):
    """Only an explicit correction for this source replaces accepted OCR.

    A pangram is an alert, not sufficient evidence for deleting real text.
    """
    for result in page_results.values():
        alert=manuscript_placeholder_alert(result)
        if alert:result['ocr_quality_alerts']=[alert]
    manifest_path=Path(manifest_path)
    if not manifest_path.is_file():return page_results
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema')!=1 or manifest.get('source')!=source_binding(pdf_path):
        raise RuntimeError('Manual page corrections do not match this source; review the source binding before rebuilding.')
    for record in manifest['pages']:
        index=record['page_number']-1
        result=page_results.setdefault(index,{})
        rejected=[c.get('__source_id') for c in result.get('cells',[])]
        result.update(cells=[dict(c) for c in record['cells']],fallback_text='',md_nohf_text='',
                      blank_page=False,needs_retry=False,ocr_text_suppressed=False,
                      visual_fallback_text_preserved=True,structural_source_page_preserved=True,
                      manual_correction={**record['evidence'],'revision':manifest['revision'],
                                         'rejected_source_ids':rejected,'human_verified':False})
        # An earlier image-only fallback must not override a deliberate correction.
        result.pop('image_fallback_path',None)
    return page_results
