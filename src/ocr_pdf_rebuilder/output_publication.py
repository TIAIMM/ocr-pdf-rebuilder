"""Build a whole document generation before replacing any existing product."""
from contextlib import contextmanager
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile

@contextmanager
def staged_products(targets):
    targets=[Path(p) for p in targets]
    state=targets[0].parent/'.ocr-state'
    state.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.publish-',dir=state) as temp:
        temp=Path(temp)
        staged=[temp/p.name for p in targets]
        yield staged
        backups=[];replaced=[]
        try:
            for target in targets:
                backup=temp/('previous-'+target.name)
                if target.exists():
                    try: os.link(target,backup)
                    except OSError: shutil.copy2(target,backup)
                    backups.append((target,backup))
            for target,new in zip(targets,staged):
                if new.exists(): os.replace(new,target)
                elif target.exists(): target.unlink()
                replaced.append(target)
        except BaseException:
            old=dict(backups)
            for target in reversed(replaced):
                if target in old: os.replace(old[target],target)
                else: target.unlink(missing_ok=True)
            raise

def publish_document(function):
    signature=inspect.signature(function)
    @wraps(function)
    def wrapped(*args,**kwargs):
        bound=signature.bind(*args,**kwargs);bound.apply_defaults()
        names=['output_pdf','output_image_pdf','output_md','output_searchable_pdf']
        if bound.arguments.get('output_searchable_pdf') is None:
            pdf=Path(bound.arguments['output_pdf'])
            bound.arguments['output_searchable_pdf']=pdf.with_name(pdf.stem+'_searchable.pdf')
        targets=[bound.arguments[k] for k in names]
        with staged_products(targets) as staged:
            for name,path in zip(names,staged): bound.arguments[name]=path
            result=function(*bound.args,**bound.kwargs)
        namespace=function.__globals__
        shared=namespace.get('shared')
        log_dir=namespace.get('LOG_DIR')
        if log_dir:
            qc=Path(log_dir)/(Path(bound.arguments['pdf_path']).stem+'_qc.json')
            if qc.exists():
                payload=json.loads(qc.read_text(encoding='utf-8'));payload.pop('_checkpoint_sha256',None)
                replacements={str(a):str(b) for a,b in zip(staged,targets)}
                def remap(value):
                    if isinstance(value,str): return replacements.get(value,value)
                    if isinstance(value,list): return [remap(v) for v in value]
                    if isinstance(value,dict): return {k:remap(v) for k,v in value.items()}
                    return value
                writer=namespace.get('write_checkpoint') or shared.write_checkpoint
                writer(qc,remap(payload))
        return result
    return wrapped
