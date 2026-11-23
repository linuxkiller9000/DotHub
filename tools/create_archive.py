"""Build a portable archive with attribution, excluding unsafe entries."""
import json, zipfile
from pathlib import Path

def create(source, output, manifest):
    source=Path(source).resolve(); out=Path(output); out.parent.mkdir(parents=True,exist_ok=True)
    files=[]
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(source.rglob('*')):
            if not p.is_file() or p.is_symlink(): continue
            rel=p.relative_to(source).as_posix()
            if '..' in Path(rel).parts or rel.startswith('.'): continue
            z.write(p,rel); files.append(rel)
        meta=dict(manifest); meta['included_files']=files
        z.writestr('manifest.json',json.dumps(meta,indent=2))
        z.writestr('README.md',f"# {meta.get('title','DotHub resource')}\n\nCreator: {meta.get('creator','Unknown')}\n\nSource: {meta.get('source_url','Not provided')}\n\nLicense: {meta.get('license','Unknown')}\n\nVerify licensing and inspect files before installing. Imported scripts are not run by DotHub.\n")
    return out
