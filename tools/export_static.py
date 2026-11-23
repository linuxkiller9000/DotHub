"""Export published DotHub records to a self-contained project-subpath-safe site."""
from __future__ import annotations
import html, json, re, shutil
from pathlib import Path
from backend.database import connect, initialize, resource_dict

def export(db_path, out_dir):
    out=Path(out_dir); out.mkdir(parents=True,exist_ok=True); report={'pages':0,'assets_copied':0,'warnings':[]}
    with connect(db_path) as db:
        rows=db.execute('SELECT r.*,l.name license FROM resources r LEFT JOIN licenses l ON l.id=r.license_id WHERE r.status="published" ORDER BY r.title').fetchall()
        resources=[resource_dict(db,r) for r in rows]
        safe=[]
        for row,r in zip(rows,resources):
            record={k:r[k] for k in ('slug','title','description','creator','source_url','repository_url','verification','version','updated','categories','environments','tags')}
            record['media']=[{'path':'media/'+Path(m['path']).name,'alt':m['alt'],'media_type':m['media_type']} for m in r['media']]
            record['license']=row['license'] or 'Unknown'; record['download']=''; record['redistribution_allowed']=bool(row['redistribution_allowed'])
            if row['archive_path'] and row['redistribution_allowed']:
                src=Path(__import__('os').environ.get('DOTHUB_STORAGE',Path.home()/'.local/share/dothub/storage'))/'archives'/row['archive_path']
                if src.is_file():
                    dest=out/'downloads'/src.name; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dest); record['download']='downloads/'+src.name; report['assets_copied']+=1
                else: report['warnings'].append(f"Missing permitted archive for {row['slug']}")
            for media in r['media']:
                src=Path(__import__('os').environ.get('DOTHUB_STORAGE',Path.home()/'.local/share/dothub/storage'))/media['path'].lstrip('/')
                if src.is_file():
                    dest=out/'media'/src.name; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dest); report['assets_copied']+=1
                else: report['warnings'].append(f"Missing media {media['path']} for {row['slug']}")
            safe.append(record)
    css='body{margin:0;background:#0b1018;color:#e8edf3;font:15px system-ui}header,main{max-width:1180px;margin:auto;padding:24px}a{color:#61d5e2;text-decoration:none}.grid{columns:4 220px;column-gap:15px}.card{break-inside:avoid;margin:0 0 15px;padding:16px;background:#141d29;border:1px solid #263342;border-radius:12px}.card img{width:100%;border-radius:8px}.muted{color:#a0afba}nav{display:flex;gap:18px;border-bottom:1px solid #263342}h1{font-size:36px}h2{font-size:18px}'
    (out/'style.css').write_text(css)
    (out/'resources.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2))
    def card(r,prefix=''):
        im=f'<img src="{prefix}{html.escape(m["path"])}" alt="{html.escape(m["alt"])}">' if r.get('media') else ''
        return f'<article class="card">{im}<div class="muted">{html.escape(", ".join(r["categories"]))}</div><h2><a href="{prefix}resources/{html.escape(r["slug"])}.html">{html.escape(r["title"])}</a></h2><p>{html.escape(r["description"])}</p></article>'
    # Resource detail page content is emitted from public fields only.
    (out/'resources').mkdir(exist_ok=True)
    for r in safe:
        related_media=r['media'][0] if r['media'] else None
        img=f'<img src="../{html.escape(related_media["path"])}" alt="{html.escape(related_media["alt"])}" style="max-width:100%">' if related_media else ''
        dl=f'<p><a href="../{html.escape(r["download"])}">Download permitted archive</a></p>' if r['download'] else '<p class="muted">No redistributable archive available.</p>'
        source_url=r['source_url'] or ''
        from urllib.parse import urlparse
        source=f'<a href="{html.escape(source_url)}" rel="noopener noreferrer">{html.escape(source_url)}</a>' if urlparse(source_url).scheme in ('http','https') and urlparse(source_url).netloc else 'Not provided'
        body=f'''{img}<p>{html.escape(r['description'])}</p><dl><dt>Creator</dt><dd>{html.escape(r['creator'])}</dd><dt>License</dt><dd>{html.escape(r['license'])}</dd><dt>Category</dt><dd>{html.escape(', '.join(r['categories']))}</dd><dt>Compatible with</dt><dd>{html.escape(', '.join(r['environments']))}</dd><dt>Status</dt><dd>{html.escape(r['verification'])}</dd></dl><p>Source: {source}</p>{dl}'''
        (out/'resources'/f'{r["slug"]}.html').write_text(f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="../style.css"><title>{html.escape(r["title"])} · DotHub</title><header><nav><a href="../index.html">DotHub</a><a href="../explore.html">Explore</a></nav></header><main><a href="../index.html">← Explore</a><h1>{html.escape(r["title"])}</h1>{body}</main></html>')
        report['pages']+=1
    all_cards=''.join(card(r) for r in safe)
    index='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="style.css"><title>DotHub — Linux customization</title><header><nav><a href="index.html">DotHub</a><a href="explore.html">Explore</a><a href="setups.html">Setups</a><a href="about.html">About</a></nav><h1>Discover. Customize. Download. Install.</h1><input id="q" placeholder="Search published resources" oninput="filter()"><p class="muted">Static public library · submissions require a separately deployed backend.</p></header><main><div id="items" class="grid">CARDS</div></main><script>function filter(){let q=document.querySelector('#q').value.toLowerCase();document.querySelectorAll('.card').forEach(x=>x.hidden=!x.innerText.toLowerCase().includes(q))}</script></html>'''.replace('CARDS',all_cards)
    (out/'index.html').write_text(index)
    (out/'explore.html').write_text((out/'index.html').read_text())
    setups=''.join(card(r) for r in safe if 'Setups' in r['categories'])
    (out/'setups.html').write_text(f'<!doctype html><html lang="en"><meta charset="utf-8"><link rel="stylesheet" href="style.css"><header><a href="index.html">DotHub</a></header><main><h1>Complete setups</h1><div class="grid">{setups or "<p>No published setups.</p>"}</div></main></html>')
    from urllib.parse import quote
    categories=sorted({c for r in safe for c in r['categories']}); environments=sorted({e for r in safe for e in r['environments']})
    (out/'categories').mkdir(exist_ok=True); (out/'environments').mkdir(exist_ok=True)
    cat_nav=' '.join(f'<a href="../categories/{quote(c, safe="")}.html">{html.escape(c)}</a>' for c in categories)
    env_nav=' '.join(f'<a href="../environments/{quote(e, safe="")}.html">{html.escape(e)}</a>' for e in environments)
    for folder,values,key in [('categories',categories,'categories'),('environments',environments,'environments')]:
        for value in values:
            selected=''.join(card(r,'../') for r in safe if value in r[key])
            slug=quote(value,safe='')
            (out/folder/f'{slug}.html').write_text(f'<!doctype html><html lang="en"><meta charset="utf-8"><link rel="stylesheet" href="../style.css"><header><nav><a href="../index.html">DotHub</a><a href="../explore.html">Explore</a></nav></header><main><h1>{html.escape(value)}</h1><p>{cat_nav if folder=="categories" else env_nav}</p><div class="grid">{selected or "<p>No published resources.</p>"}</div></main></html>')
    (out/'about.html').write_text('<!doctype html><html lang="en"><meta charset="utf-8"><link rel="stylesheet" href="style.css"><main><a href="index.html">DotHub</a><h1>About this export</h1><p>This static library contains published resources only. Submissions and administration require a separately deployed backend.</p></main></html>')
    report['pages']+=3+len(categories)+len(environments)
    report['categories']=len(categories); report['environments']=len(environments)
    broken=[]
    for page in out.rglob('*.html'):
        text=page.read_text()
        for ref in re.findall(r'(?:href|src)="([^"]+)"',text):
            if ref.startswith(('https://','http://','mailto:','#','data:')): continue
            target=(page.parent/ref.split('#',1)[0].split('?',1)[0]).resolve()
            if not target.is_file(): broken.append(f'{page.relative_to(out)} -> {ref}')
    report['broken_links']=broken
    if broken: report['warnings'].extend('Broken local link: '+x for x in broken)
    (out/'validation-report.json').write_text(json.dumps(report,indent=2))
    return report

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--db',default=None); parser.add_argument('--output',default='export'); args=parser.parse_args()
    print(json.dumps(export(args.db or initialize(),args.output),indent=2))
