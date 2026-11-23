"""DotHub local WSGI application. Uses only the Python standard library."""
from __future__ import annotations
import html, io, json, mimetypes, os, re, shutil, socket, sqlite3, subprocess, tempfile, threading, time, urllib.error, urllib.request, zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse
from wsgiref.simple_server import make_server
from .database import DATA_HOME, DB_PATH, add_resource, connect, initialize, resource_dict

ROOT=Path(__file__).resolve().parent.parent
STORAGE=Path(os.environ.get('DOTHUB_STORAGE',DATA_HOME/'storage'))
MAX_UPLOAD=30*1024*1024; MAX_ENTRIES=3000; MAX_UNPACKED=150*1024*1024
HTML='text/html; charset=utf-8'

def esc(v): return html.escape(str(v or ''),quote=True)
def safe_http_url(value): return urlparse(value or '').scheme in ('http','https') and bool(urlparse(value or '').netloc)
def local_peer(environ): return environ.get('REMOTE_ADDR','') in ('127.0.0.1','::1')
def validate_github_url(url):
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname not in ('github.com','www.github.com') or parsed.username or parsed.password:
        raise ValueError('Enter a public https://github.com/owner/repository URL')
    bits=parsed.path.strip('/').split('/')
    if len(bits)<2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+',x) for x in bits[:2]):
        raise ValueError('Enter a standard public GitHub owner/repository URL')
    return bits[0],bits[1].removesuffix('.git')
def body(environ,limit=MAX_UPLOAD):
    try: n=int(environ.get('CONTENT_LENGTH') or 0)
    except ValueError: n=0
    if n<0 or n>limit: raise ValueError('Request is too large')
    return environ['wsgi.input'].read(n)
def response(start,status,data,ctype=HTML,headers=()):
    payload=data.encode() if isinstance(data,str) else data
    start(status,[('Content-Type',ctype),('Content-Length',str(len(payload))),('X-Content-Type-Options','nosniff'),('Referrer-Policy','strict-origin-when-cross-origin'),*headers]); return [payload]
def json_response(start,obj,status='200 OK'): return response(start,status,json.dumps(obj), 'application/json; charset=utf-8')

def safe_zip(blob,dest):
    dest=Path(dest); dest.mkdir(parents=True,exist_ok=True)
    total=0; kept=[]
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        infos=z.infolist()
        if len(infos)>MAX_ENTRIES: raise ValueError('Archive contains too many entries')
        for info in infos:
            name=info.filename.replace('\\','/')
            p=Path(name)
            mode=(info.external_attr >> 16) & 0xffff
            if p.is_absolute() or '..' in p.parts or (mode & 0o170000)==0o120000: raise ValueError('Unsafe archive path or symbolic link')
            if info.flag_bits & 1: raise ValueError('Encrypted ZIP files are not accepted')
            if info.file_size>MAX_UNPACKED: raise ValueError('Archive member exceeds extraction limit')
            total+=info.file_size
            if total>MAX_UNPACKED: raise ValueError('Archive exceeds extraction limit')
            target=(dest/p).resolve()
            if dest.resolve() not in target.parents and target!=dest.resolve(): raise ValueError('Unsafe archive path')
            if info.is_dir(): target.mkdir(parents=True,exist_ok=True); continue
            if not re.fullmatch(r'[\w .@+()\-]+',p.name,re.UNICODE): raise ValueError('Archive contains an unsafe filename')
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(info) as src, target.open('wb') as out: shutil.copyfileobj(src,out)
            kept.append(name)
    return kept

def submission_preview(submission):
    base=Path(submission['storage_path']).resolve(); root=(STORAGE/'submissions').resolve()
    if root not in base.parents or not base.is_dir(): return '<p class="muted">Submission files are unavailable.</p>'
    files=sorted(p for p in base.rglob('*') if p.is_file() and not p.is_symlink())
    listing=''.join(f'<li>{esc(p.relative_to(base).as_posix())} <span class="muted">{p.stat().st_size} bytes</span></li>' for p in files[:120])
    previews=[]
    for p in files:
        if p.suffix.lower() in ('.sh','.py','.conf','.kdl','.json','.md','.txt','.css','.ini'):
            try: sample=p.read_bytes()[:4000].decode('utf-8',errors='replace')
            except OSError: continue
            previews.append(f'<details><summary>Preview {esc(p.relative_to(base).as_posix())}</summary><pre>{esc(sample)}</pre></details>')
            if len(previews)>=5: break
    return f'<details class="file-inspection"><summary>Inspect {len(files)} extracted files (nothing is executed)</summary><ul>{listing or "<li>No files</li>"}</ul>{"".join(previews)}</details>'

def page(title,content):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="Discover, customize and download Linux desktop resources"><title>{esc(title)} · DotHub</title><link rel="stylesheet" href="/static/style.css"><script defer src="/static/app.js"></script></head><body><header class="topbar"><a class="brand" href="/"><span class="mark">D</span><span>Dot<span class="cyan">Hub</span></span></a><nav aria-label="Main navigation"><a href="/">Home</a><a href="/explore">Explore</a><a href="/setups">Setups</a><a href="/submit">Submit</a><a href="/about">About</a></nav></header><main>{content}</main><footer>DotHub <span>Discover. Customize. Download. Install.</span><a href="/about">About this project</a></footer></body></html>'''

def cards(rows,empty='No published resources yet. Local demo content is labeled clearly.'):
    if not rows: return f'<div class="empty"><div class="empty-icon">⌕</div><h3>Nothing here yet</h3><p>{esc(empty)}</p><a class="button secondary" href="/submit">Submit a resource</a></div>'
    out='<div class="gallery">'
    for r in rows:
        cats=', '.join(r['categories']) or 'Resource'; env=', '.join(r['environments']) or 'Environment agnostic'; media=r['media'][0]['path'] if r['media'] else ''
        cover=f'<img loading="lazy" src="{esc(media)}" alt="{esc(r["media"][0]["alt"])}">' if media else '<div class="cover-placeholder"><span>DH</span><small>Preview unavailable</small></div>'
        source=f'<a href="{esc(r["source_url"])}" rel="noopener noreferrer">source ↗</a>' if safe_http_url(r['source_url']) else 'source not provided'
        out+=f'<article class="card"><a class="cover" href="/resource/{quote(r["slug"])}">{cover}</a><div class="card-body"><div class="eyebrow">{esc(cats)}</div><h3><a href="/resource/{quote(r["slug"])}">{esc(r["title"])}</a></h3><p>{esc(r["description"][:150])}</p><div class="card-meta"><span>{esc(env)}</span><span>{esc(r["verification"])}</span></div><small>by {esc(r["creator"])} · {source}</small></div></article>'
    return out+'</div>'

def published(db,where='1=1',args=(),order='r.created DESC',limit=60,offset=0):
    rows=db.execute(f'SELECT r.* FROM resources r WHERE r.status="published" AND {where} ORDER BY {order} LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
    return [resource_dict(db,r) for r in rows]

def app(environ,start):
    path=unquote(environ.get('PATH_INFO','/')); method=environ.get('REQUEST_METHOD','GET').upper(); qs=parse_qs(environ.get('QUERY_STRING','')); peer=local_peer(environ)
    try:
        initialize(); STORAGE.mkdir(parents=True,exist_ok=True)
        with connect() as db:
            if path.startswith('/static/'):
                f=(ROOT/'frontend'/path.removeprefix('/static/')).resolve()
                if ROOT/'frontend' not in f.parents or not f.is_file(): return response(start,'404 Not Found','Not found')
                return response(start,'200 OK',f.read_bytes(),mimetypes.guess_type(f)[0] or 'application/octet-stream')
            if path=='/health': return json_response(start,{'ok':True,'database':str(DB_PATH) if peer else 'available'})
            if path=='/api/resources':
                q=(qs.get('q',[''])[0]).strip(); cat=qs.get('category',[''])[0]; env=qs.get('environment',[''])[0]
                clauses=[]; args=[]
                if q: clauses.append('(r.title LIKE ? OR r.description LIKE ? OR r.creator LIKE ? OR r.metadata LIKE ? OR EXISTS(SELECT 1 FROM resource_tags t JOIN tags x ON x.id=t.tag_id WHERE t.resource_id=r.id AND x.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_categories x JOIN categories c ON c.id=x.category_id WHERE x.resource_id=r.id AND c.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_environments x JOIN environments e ON e.id=x.environment_id WHERE x.resource_id=r.id AND e.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_dependencies rd JOIN dependencies d ON d.id=rd.dependency_id WHERE rd.resource_id=r.id AND d.name LIKE ?))'); args += [f'%{q}%']*8
                if cat: clauses.append('EXISTS(SELECT 1 FROM resource_categories x JOIN categories c ON c.id=x.category_id WHERE x.resource_id=r.id AND c.name=?)'); args.append(cat)
                if env: clauses.append('EXISTS(SELECT 1 FROM resource_environments x JOIN environments e ON e.id=x.environment_id WHERE x.resource_id=r.id AND e.name=?)'); args.append(env)
                order={'newest':'r.created DESC','alphabetical':'r.title COLLATE NOCASE ASC','popular':'r.download_count DESC,r.created DESC'}.get(qs.get('sort',['newest'])[0],'r.created DESC')
                result=published(db,' AND '.join(clauses) or '1=1',args,order,min(int(qs.get('limit',['60'])[0]),100),max(0,int(qs.get('offset',['0'])[0])))
                return json_response(start,{'items':result,'count':len(result)})
            if path=='/' or path=='/explore' or path=='/setups':
                query=qs.get('q',[''])[0]; category=qs.get('category',[''])[0]; environment=qs.get('environment',[''])[0]
                if path=='/setups': category='Setups'
                clauses=[]; args=[]
                if query: clauses.append('(r.title LIKE ? OR r.description LIKE ? OR r.creator LIKE ? OR r.metadata LIKE ? OR EXISTS(SELECT 1 FROM resource_tags t JOIN tags x ON x.id=t.tag_id WHERE t.resource_id=r.id AND x.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_categories x JOIN categories c ON c.id=x.category_id WHERE x.resource_id=r.id AND c.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_environments x JOIN environments e ON e.id=x.environment_id WHERE x.resource_id=r.id AND e.name LIKE ?) OR EXISTS(SELECT 1 FROM resource_dependencies rd JOIN dependencies d ON d.id=rd.dependency_id WHERE rd.resource_id=r.id AND d.name LIKE ?))'); args += [f'%{query}%']*8
                if category: clauses.append('EXISTS(SELECT 1 FROM resource_categories x JOIN categories c ON c.id=x.category_id WHERE x.resource_id=r.id AND c.name=?)'); args.append(category)
                if environment: clauses.append('EXISTS(SELECT 1 FROM resource_environments x JOIN environments e ON e.id=x.environment_id WHERE x.resource_id=r.id AND e.name=?)'); args.append(environment)
                where=' AND '.join(clauses) or '1=1'; rows=published(db,where,args,limit=30)
                cats=db.execute('SELECT name FROM categories ORDER BY name').fetchall(); envs=db.execute('SELECT name,kind FROM environments ORDER BY name').fetchall()
                catlinks=''.join(f'<a class="chip" href="/explore?category={quote(r[0])}">{esc(r[0])}</a>' for r in cats)
                envlinks=''.join(f'<a class="chip" href="/explore?environment={quote(r[0])}">{esc(r[0])}</a>' for r in envs)
                featured=published(db,'EXISTS(SELECT 1 FROM setups s WHERE s.resource_id=r.id)',limit=4) if path=='/' else []
                recent=published(db,limit=8) if path=='/' else []
                trending=published(db,'r.download_count>0',order='r.download_count DESC',limit=8) if path=='/' else []
                home_sections=(f'<section class="section"><div class="section-head"><div><div class="eyebrow">CURATED COLLECTIONS</div><h2>Featured setups</h2></div><a href="/setups" class="text-link">All setups ↗</a></div>{cards(featured,"No complete setups have been published yet.")}</section>' if path=='/' else '')
                if path=='/':
                    home_sections+=f'<section class="section"><div class="section-head"><div><div class="eyebrow">RECENTLY ADDED</div><h2>Fresh resources</h2></div></div>{cards(recent)}</section>'
                    if trending: home_sections+=f'<section class="section"><div class="section-head"><div><div class="eyebrow">MOST DOWNLOADED</div><h2>Trending now</h2></div></div>{cards(trending)}</section>'
                content=f'''<section class="hero"><div class="hero-copy"><div class="eyebrow"><span class="live-dot"></span> COMMUNITY DISCOVERY · LOCAL BUILD</div><h1>Your Linux desktop,<br><span>your signature.</span></h1><p>Discover thoughtful configs, themes, wallpapers, and complete setups for the desktop you love.</p><form class="search" action="/explore"><span>⌕</span><input name="q" value="{esc(query)}" placeholder="Search resources, apps, environments…" aria-label="Search resources"><button>Search</button></form><div class="hero-note">No account needed <i>·</i> Every submission reviewed <i>·</i> Source and license shown</div></div><div class="hero-art"><div class="orbit o1"></div><div class="orbit o2"></div><div class="window"><div class="window-head"><b></b><b></b><b></b><span>dot.hub — discover</span></div><div class="window-content"><div class="visual-bars"><i></i><i></i><i></i><i></i><i></i><i></i></div><div class="visual-lines"><i></i><i></i><i></i></div><label>DESKTOP / NIRI</label></div></div><div class="floating-tag">✦ curated by people</div></div></section>
                <section class="section"><div class="section-head"><div><div class="eyebrow">START EXPLORING</div><h2>Browse by category</h2></div><a href="/explore" class="text-link">All resources ↗</a></div><div class="chips">{catlinks}</div></section>
                <section class="section"><div class="section-head"><div><div class="eyebrow">COMPATIBILITY</div><h2>Choose your environment</h2></div></div><div class="chips">{envlinks}</div></section>
                {home_sections}<section class="section"><div class="section-head"><div><div class="eyebrow">{('RESULTS FOR “'+esc(query)+'”') if query else 'MADE FOR DISCOVERY'}</div><h2>{'Explore resources' if path!='/setups' else 'Complete setups'}</h2></div><div class="sort-control"><label for="sort">Sort</label><select id="sort" onchange="sortCards(this.value)"><option value="newest">Recently added</option><option value="alphabetical">A–Z</option></select></div></div>{cards(rows)}</section>'''
                return response(start,'200 OK',page('Home' if path=='/' else ('Setups' if path=='/setups' else 'Explore'),content))
            if path=='/about':
                content='<section class="page-intro"><div class="eyebrow">ABOUT DOTHUB</div><h1>Customization, shared thoughtfully.</h1><p>DotHub is a discovery and resource management platform for Linux desktop customization. This local installation starts empty; sample community content is not included.</p><div class="notice"><strong>Local build:</strong> Browse and download published resources, or submit a ZIP/public repository for local review. This interface is served on localhost by default.</div><h2>Responsible sharing</h2><p>Resources remain private until a local administrator reviews attribution, license, files, and compatibility. Imported scripts are never executed.</p></section>'
                return response(start,'200 OK',page('About',content))
            if path=='/submit' and method=='GET':
                content='''<section class="page-intro"><div class="eyebrow">SHARE WITH THE COMMUNITY</div><h1>Submit a resource</h1><p>ZIP uploads and public GitHub repositories enter a private local review queue. Nothing is published automatically.</p><div class="form-grid"><form class="panel" action="/submit/zip" method="post" enctype="multipart/form-data"><h2>Upload a ZIP</h2><label>Resource name<input name="title" required maxlength="120"></label><label>Your name or handle<input name="creator" maxlength="120"></label><label>License<input name="license" placeholder="e.g. MIT, CC BY 4.0, unknown"></label><label>ZIP archive<input name="file" type="file" accept=".zip,application/zip" required></label><button class="button">Send for review</button></form><form class="panel" action="/submit/github" method="post"><h2>Public GitHub repository</h2><label>Repository URL<input name="url" type="url" placeholder="https://github.com/owner/repository" required></label><label>Resource name<input name="title" required maxlength="120"></label><label>Your name or handle<input name="creator" maxlength="120"></label><button class="button">Import for review</button><p class="muted">Only public repositories are fetched. No credentials are requested.</p></form></div></section>'''
                return response(start,'200 OK',page('Submit',content))
            if path in ('/submit/zip','/submit/github') and method=='POST':
                data=body(environ)
                fields={}
                if path.endswith('/zip'):
                    content_type=environ.get('CONTENT_TYPE','')
                    msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+content_type.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+data)
                    files={}
                    for part in msg.iter_parts():
                        disp=part.get_content_disposition(); name=part.get_param('name',header='content-disposition')
                        if disp=='form-data' and name:
                            payload=part.get_payload(decode=True) or b''
                            if name=='file': files[part.get_filename() or 'upload.zip']=payload
                            else: fields[name]=payload.decode(errors='replace')
                    if len(files)!=1: raise ValueError('Choose one ZIP file')
                    filename,blob=next(iter(files.items()))
                    if not filename.lower().endswith('.zip'): raise ValueError('Only ZIP archives are accepted')
                    subid=__import__('uuid').uuid4().hex; target=STORAGE/'submissions'/subid
                    safe_zip(blob,target)
                    source='local ZIP upload'; method_name='zip'
                else:
                    fields={k:v[0] for k,v in parse_qs(data.decode()).items()}; owner,repo=validate_github_url(fields.get('url','')); repo_url=f'https://github.com/{owner}/{repo}'
                    meta_req=urllib.request.Request(f'https://api.github.com/repos/{quote(owner)}/{quote(repo)}',headers={'User-Agent':'DotHub local importer','Accept':'application/vnd.github+json'})
                    try:
                        with urllib.request.urlopen(meta_req,timeout=15) as meta_response: repo_meta=json.loads(meta_response.read(512*1024))
                    except Exception as e: raise ValueError('Could not read public repository metadata: '+str(e))
                    if repo_meta.get('private') or not repo_meta.get('default_branch'): raise ValueError('Repository is not public or has no default branch')
                    archive_url=f'https://codeload.github.com/{quote(owner)}/{quote(repo)}/zip/refs/heads/{quote(repo_meta["default_branch"],safe="")}'
                    req=urllib.request.Request(archive_url,headers={'User-Agent':'DotHub local importer'})
                    try:
                        with urllib.request.urlopen(req,timeout=20) as response_obj: blob=response_obj.read(MAX_UPLOAD+1)
                    except Exception as e: raise ValueError('Could not fetch the public repository archive: '+str(e))
                    if len(blob)>MAX_UPLOAD: raise ValueError('Repository archive exceeds 30 MiB')
                    subid=__import__('uuid').uuid4().hex; target=STORAGE/'submissions'/subid; safe_zip(blob,target)
                    source=repo_url; method_name='github'
                    if not fields.get('title'): fields['title']=repo_meta.get('name','Untitled')
                    detected=repo_meta.get('license') or {}; license_info=detected.get('spdx_id')
                    if license_info and license_info not in ('NOASSERTION','OTHER'): fields.setdefault('license',license_info)
                title=fields.get('title','').strip()
                if not title: raise ValueError('Resource name is required')
                db.execute('INSERT INTO submissions(method,title,source_url,creator,license,storage_path) VALUES(?,?,?,?,?,?)',(method_name,title[:120],source,fields.get('creator','')[:120],fields.get('license','Unknown')[:100],str(target)))
                return response(start,'303 See Other','',headers=[('Location','/submit?sent=1')])
            if path.startswith('/resource/'):
                slug=path.split('/',2)[2]; row=db.execute('SELECT r.*,l.name license FROM resources r LEFT JOIN licenses l ON l.id=r.license_id WHERE r.slug=? AND r.status="published"',(slug,)).fetchone()
                if not row: return response(start,'404 Not Found',page('Not found','<div class="empty"><h2>Resource not found</h2><a href="/explore">Back to Explore</a></div>'))
                r=resource_dict(db,row); env=', '.join(r['environments']) or 'Not specified'; cats=', '.join(r['categories']) or 'Resource'; tags=' '.join(f'<span class="chip">{esc(t)}</span>' for t in r['tags'])
                media=''.join(f'<img class="detail-image" src="{esc(m["path"])}" alt="{esc(m["alt"])}" loading="lazy">' for m in r['media'])
                download=f'<a class="button" href="/download/{quote(slug)}">Download archive</a>' if r['archive_path'] and r['redistribution_allowed'] else '<span class="muted">No redistributable archive is available.</span>'
                source_detail=f'<a href="{esc(r["source_url"])}" rel="noopener noreferrer">Original source ↗</a>' if safe_http_url(r['source_url']) else 'Not provided'
                repo_detail=f'<dt>Repository</dt><dd><a href="{esc(r["repository_url"])}" rel="noopener noreferrer">Repository ↗</a></dd>' if safe_http_url(r['repository_url']) else ''
                content=f'''<section class="detail"><a class="back" href="/explore">← Explore</a><div class="eyebrow">{esc(cats)}</div><h1>{esc(r['title'])}</h1><p class="lead">{esc(r['description'])}</p><div class="detail-grid"><div class="detail-main">{media or '<div class="cover-placeholder detail-placeholder"><span>DH</span><small>Preview unavailable</small></div>'}<h2>About this resource</h2><p>{esc(r['description'])}</p><h2>Installation</h2><p>Download and inspect the files before installing. Never run an imported script unless you have reviewed its contents.</p><div class="notice">Back up existing configuration files before making changes. DotHub does not apply changes to your system.</div><h2>Tags</h2><div class="chips">{tags or '<span class="muted">No tags listed</span>'}</div></div><aside class="panel details"><h3>Resource details</h3><dl><dt>Creator</dt><dd>{esc(r['creator'])}</dd><dt>Category</dt><dd>{esc(cats)}</dd><dt>Environment</dt><dd>{esc(env)}</dd><dt>License</dt><dd>{esc(r['license'] or 'Unknown')} · {'redistribution approved' if r['redistribution_allowed'] else 'redistribution not confirmed'}</dd><dt>Status</dt><dd><span class="status">{esc(r['verification'])}</span></dd><dt>Version</dt><dd>{esc(r['version'] or 'Not specified')}</dd><dt>Updated</dt><dd>{esc(r['updated'])}</dd><dt>Source</dt><dd>{source_detail}</dd>{repo_detail}</dl>{download}</aside></div></section>'''
                return response(start,'200 OK',page(r['title'],content))
            if path.startswith('/download/'):
                slug=path.split('/',2)[2]; row=db.execute('SELECT r.archive_path,r.redistribution_allowed FROM resources r WHERE r.slug=? AND r.status="published"',(slug,)).fetchone()
                if not row or not row['archive_path'] or not row['redistribution_allowed']: return response(start,'404 Not Found','No permitted archive')
                f=(STORAGE/'archives'/row['archive_path']).resolve()
                if STORAGE.resolve() not in f.parents or not f.is_file(): return response(start,'404 Not Found','Archive missing')
                db.execute('UPDATE resources SET download_count=download_count+1 WHERE slug=?',(slug,))
                return response(start,'200 OK',f.read_bytes(),'application/zip',[('Content-Disposition',f'attachment; filename="{slug}.zip"')])
            if path=='/admin' or path.startswith('/admin/'):
                if not peer: return response(start,'403 Forbidden',page('Forbidden','<div class="empty"><h2>Local administration only</h2></div>'))
                if path=='/admin':
                    counts={t:db.execute(f'SELECT count(*) FROM {t}'+(' WHERE status="pending"' if t=='submissions' else '')).fetchone()[0] for t in ('resources','submissions')}
                    pend=db.execute('SELECT * FROM submissions WHERE status="pending" ORDER BY created').fetchall()
                    cards_html=''.join(f'<div class="review-row"><div><strong>{esc(s["title"])}</strong><small>{esc(s["method"])} · {esc(s["source_url"])} · {esc(s["license"])}</small><small>{esc(s["notes"])}</small>{submission_preview(s)}</div><form method="post" action="/admin/review/{s["id"]}"><input name="note" placeholder="Review note"><label class="muted"><input type="checkbox" name="redistributable" value="1"> License allows redistribution</label><button name="action" value="approve" class="button">Approve</button><button name="action" value="reject" class="button secondary">Reject</button></form></div>' for s in pend) or '<p class="muted">No submissions pending review.</p>'
                    sources=db.execute('SELECT * FROM collection_sources ORDER BY id').fetchall()
                    source_listing=''.join(f'<li>{esc(s["name"])} · {esc(s["url"])} · {"enabled" if s["enabled"] else "disabled"} · {esc(s["last_status"] or "not checked")} {esc(s["last_error"] or "")}<form method="post" action="/admin/sources/{s["id"]}/toggle"><button class="button secondary">{"Disable" if s["enabled"] else "Enable"}</button></form></li>' for s in sources) or '<li>No custom feeds configured.</li>'
                    auto_state=os.environ.get('DOTHUB_AUTO_COLLECT','1')!='0'; interval=max(1,min(int(os.environ.get('DOTHUB_COLLECT_INTERVAL_HOURS','12')),168))
                    reddit_state='authorized token configured' if os.environ.get('DOTHUB_REDDIT_BEARER_TOKEN') else 'not connected (requires approved Reddit API access)'
                    source_panel=f'''<section class="panel"><h2>Automatic discovery</h2><p>Collector: {'running every '+str(interval)+' hours while the server is running' if auto_state else 'paused by DOTHUB_AUTO_COLLECT=0'}. GitHub: public repository metadata. Reddit: {esc(reddit_state)}. Other sites: administrator-configured RSS/Atom feeds with robots.txt checks.</p><p class="muted">It collects titles, links, and short metadata only. It does not fetch repo archives, execute content, or publish automatically.</p><form method="post" action="/admin/sources/collect"><button class="button">Run collection now</button></form><ul>{source_listing}</ul><form class="form-grid" method="post" action="/admin/sources"><label>Feed name<input name="name" required placeholder="Project blog"></label><label>Public HTTPS RSS/Atom URL<input name="url" type="url" required placeholder="https://example.org/feed.xml"></label><button class="button secondary">Add feed</button></form></section>'''
                    resources=db.execute('SELECT id,title,status FROM resources ORDER BY created DESC').fetchall()
                    resource_list=''.join(f'<li class="resource-row"><div>{esc(x["title"])} <span class="muted">({esc(x["status"])})</span></div><div class="row-actions"><a class="button secondary" href="/admin/resources/{x["id"]}/edit">Edit</a>{f'<form method="post" action="/admin/resources/{x["id"]}/publish"><button class="button">Publish</button></form>' if x["status"]=="draft" else f'<form method="post" action="/admin/resources/{x["id"]}/archive"><button class="button secondary">Archive</button></form>' if x["status"]=="published" else ''}<form method="post" action="/admin/resources/{x["id"]}/delete" onsubmit="return confirm(\'Remove this resource?\')"><button class="button secondary">Remove</button></form></div></li>' for x in resources) or '<li>No resources yet.</li>'
                    content=f'''<section class="page-intro"><div class="eyebrow">LOCAL ONLY · ADMINISTRATION</div><h1>Review desk</h1><div class="stats"><div><b>{counts['resources']}</b><span>resources</span></div><div><b>{counts['submissions']}</b><span>pending submissions</span></div><div><b>{sum(f.stat().st_size for f in STORAGE.rglob('*') if f.is_file())//1024 if STORAGE.exists() else 0} KB</b><span>storage used</span></div></div><div class="form-grid"><form class="panel" method="post" action="/admin/resources"><h2>Create resource</h2><label>Title<input name="title" required></label><label>Description<textarea name="description" rows="3"></textarea></label><label>Creator<input name="creator"></label><label>Source URL<input name="source_url" type="url"></label><label>Repository URL<input name="repository_url" type="url"></label><label>License<input name="license" value="Unknown"></label><label><input name="redistributable" type="checkbox" value="1"> License permits redistribution</label><label>Categories (comma separated)<input name="categories" placeholder="Wallpapers, Configurations"></label><label>Environments (comma separated)<input name="environments" placeholder="Niri, KDE Plasma"></label><button class="button">Save draft</button></form><form class="panel" method="post" action="/admin/dependencies"><h2>Add dependency record</h2><label>Name<input name="name" required></label><label>Description<input name="description"></label><label>Website<input name="website" type="url"></label><label>Package command<input name="command" placeholder="Leave blank unless source verified"></label><label>Platform<input name="platform" value="Arch Linux"></label><label>Source URL<input name="source_url" type="url"></label><button class="button secondary">Save unverified</button><p class="muted">Commands are stored as metadata and never run by DotHub.</p></form></div><section class="panel"><h2>Pending submissions ({counts['submissions']})</h2>{cards_html}</section><section class="panel"><h2>Resources</h2><ul>{resource_list}</ul><form method="post" action="/admin/backup"><button class="button secondary">Create database backup</button></form><a href="/admin/export">Export static site</a></section></section>'''
                    content=content.replace('<section class="panel"><h2>Pending submissions',source_panel+'<section class="panel"><h2>Pending submissions')
                    return response(start,'200 OK',page('Local admin',content))
                if path=='/admin/resources' and method=='POST':
                    f=parse_qs(body(environ).decode()); get=lambda k,d='':f.get(k,[d])[0]
                    license_name=get('license','Unknown') or 'Unknown'; db.execute('INSERT OR IGNORE INTO licenses(name,redistributable) VALUES(?,0)',(license_name,))
                    data={'title':get('title'),'description':get('description'),'creator':get('creator','Unknown'),'source_url':get('source_url'),'repository_url':get('repository_url'),'license':license_name,'redistributable':bool(get('redistributable')),'categories':[x.strip() for x in get('categories').split(',') if x.strip()],'environments':[x.strip() for x in get('environments').split(',') if x.strip()]}
                    add_resource(db,data,'draft'); return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path.startswith('/admin/resources/') and path.endswith('/edit') and method=='GET':
                    rid=int(path.rstrip('/').split('/')[-2]); r=db.execute('SELECT * FROM resources WHERE id=?',(rid,)).fetchone()
                    if not r: return response(start,'404 Not Found','Resource not found')
                    content=f'<section class="page-intro"><div class="eyebrow">LOCAL RESOURCE EDITOR</div><h1>Edit {esc(r["title"])}</h1><form class="panel" method="post" action="/admin/resources/{rid}/edit"><label>Title<input name="title" required value="{esc(r["title"])}"></label><label>Description<textarea name="description" rows="5">{esc(r["description"])}</textarea></label><label>Creator<input name="creator" value="{esc(r["creator"])}"></label><label>Source URL<input name="source_url" value="{esc(r["source_url"])}"></label><label>Repository URL<input name="repository_url" value="{esc(r["repository_url"])}"></label><label>Verification<select name="verification">{''.join(f'<option {"selected" if r["verification"]==v else ""}>{v}</option>' for v in ("Untested","Verified","Outdated","Incompatible"))}</select></label><label>Version<input name="version" value="{esc(r["version"])}"></label><button class="button">Save changes</button> <a href="/admin">Cancel</a></form></section>'
                    return response(start,'200 OK',page('Edit resource',content))
                if path.startswith('/admin/resources/') and method=='POST':
                    parts=path.rstrip('/').split('/'); rid=int(parts[-2]); action=parts[-1]
                    if action=='publish': db.execute('UPDATE resources SET status="published" WHERE id=? AND status="draft"',(rid,))
                    elif action=='archive': db.execute('UPDATE resources SET status="archived" WHERE id=? AND status="published"',(rid,))
                    elif action=='delete': db.execute('DELETE FROM resources WHERE id=?',(rid,))
                    elif action=='edit':
                        f=parse_qs(body(environ).decode()); get=lambda k,d='':f.get(k,[d])[0]
                        db.execute('UPDATE resources SET title=?,description=?,creator=?,source_url=?,repository_url=?,verification=?,version=?,updated=date("now") WHERE id=?',(get('title'),get('description'),get('creator'),get('source_url'),get('repository_url'),get('verification','Untested'),get('version'),rid))
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path=='/admin/dependencies' and method=='POST':
                    f=parse_qs(body(environ).decode()); get=lambda k,d='':f.get(k,[d])[0]
                    cur=db.execute('INSERT INTO dependencies(name,description,website,status) VALUES(?,?,?,?)',(get('name'),get('description'),get('website'),'Unverified'))
                    cmd=get('command')
                    if cmd: db.execute('INSERT INTO install_methods(dependency_id,platform,command,source_type,source_url,verified) VALUES(?,?,?,?,?,0)',(cur.lastrowid,get('platform'),cmd,'unverified',get('source_url')))
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path=='/admin/sources' and method=='POST':
                    f=parse_qs(body(environ).decode()); name=f.get('name',[''])[0].strip(); url=f.get('url',[''])[0].strip(); parsed=urlparse(url)
                    if not name or parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password: raise ValueError('Add a feed name and public HTTPS RSS/Atom URL')
                    db.execute("INSERT INTO collection_sources(kind,name,url,enabled) VALUES('feed',?,?,1) ON CONFLICT(url) DO UPDATE SET name=excluded.name,enabled=1",(name[:120],url[:1000]))
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path.startswith('/admin/sources/') and path.endswith('/toggle') and method=='POST':
                    source_id=int(path.rstrip('/').split('/')[-2])
                    db.execute('UPDATE collection_sources SET enabled=1-enabled WHERE id=?',(source_id,))
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path=='/admin/sources/collect' and method=='POST':
                    from tools.collect_sources import collect_once
                    report=collect_once(DB_PATH)
                    return response(start,'200 OK',page('Collection report',f'<section class="page-intro"><div class="eyebrow">DISCOVERY RUN</div><h1>Collection finished</h1><p>New metadata was placed in pending review. No repositories or attachments were downloaded.</p><pre>{esc(json.dumps(report,indent=2,ensure_ascii=False))}</pre><a href="/admin">Return to review desk</a></section>'))
                if path.startswith('/admin/review/') and method=='POST':
                    sid=int(path.rsplit('/',1)[-1]); f=parse_qs(body(environ).decode()); action=f.get('action',['reject'])[0]
                    if action=='approve':
                        sub=db.execute('SELECT * FROM submissions WHERE id=? AND status="pending"',(sid,)).fetchone()
                        if sub:
                            l=sub['license'] or 'Unknown'; allow=1 if f.get('redistributable',[''])[0]=='1' else 0
                            db.execute('INSERT OR IGNORE INTO licenses(name,redistributable) VALUES(?,0)',(l,))
                            archive_name=''
                            if sub['storage_path']:
                                rd=STORAGE/'archives'/f'submission-{sid}.zip'; base=Path(sub['storage_path'])
                                from tools.create_archive import create
                                create(base,rd,{'title':sub['title'],'creator':sub['creator'],'source_url':sub['source_url'],'license':l}); archive_name=rd.name
                            data={'title':sub['title'],'creator':sub['creator'],'source_url':sub['source_url'],'repository_url':sub['source_url'] if sub['method']=='github' else '', 'license':l,'redistributable':bool(allow),'archive_path':archive_name,'categories':['Other Resources'],'metadata':{'discovered_by':sub['method'],'source_note':sub['notes']}}
                            add_resource(db,data,'published')
                            db.execute('UPDATE submissions SET status="approved" WHERE id=?',(sid,))
                    else: db.execute('UPDATE submissions SET status="rejected" WHERE id=? AND status="pending"',(sid,))
                    db.execute('INSERT INTO moderation(submission_id,action,note) VALUES(?,?,?)',(sid,action,f.get('note',[''])[0]))
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path=='/admin/backup' and method=='POST':
                    dest=DATA_HOME/'backups'; dest.mkdir(parents=True,exist_ok=True); shutil.copy2(DB_PATH,dest/f'dothub-{__import__("datetime").date.today().isoformat()}.sqlite3')
                    return response(start,'303 See Other','',headers=[('Location','/admin')])
                if path=='/admin/export':
                    from tools.export_static import export
                    report=export(DB_PATH,ROOT/'export'); return response(start,'200 OK',page('Export',f'<section class="page-intro"><h1>Static export complete</h1><pre>{esc(json.dumps(report,indent=2))}</pre><a href="/admin">Return to admin</a></section>'))
            return response(start,'404 Not Found',page('Not found','<div class="empty"><h2>Page not found</h2><a href="/">Return home</a></div>'))
    except ValueError as e: return response(start,'400 Bad Request',page('Request issue',f'<section class="page-intro"><div class="notice">{esc(e)}</div><a href="/submit">Return</a></section>'))
    except (zipfile.BadZipFile,KeyError,sqlite3.Error) as e: return response(start,'400 Bad Request',page('Request issue',f'<section class="page-intro"><div class="notice">Request could not be completed: {esc(e)}</div></section>'))
    except Exception as e:
        # Keep details out of public responses; local logs retain traceback via server stderr.
        import traceback; traceback.print_exc(); return response(start,'500 Internal Server Error',page('Server error','<div class="empty"><h2>Something went wrong</h2><p>Check the local server log for details.</p></div>'))

def main():
    initialize(); STORAGE.mkdir(parents=True,exist_ok=True)
    host=os.environ.get('DOTHUB_HOST','127.0.0.1'); port=int(os.environ.get('DOTHUB_PORT','8000'))
    if host not in ('127.0.0.1','localhost','::1') and os.environ.get('DOTHUB_ALLOW_REMOTE')!='1': raise SystemExit('Remote binding requires DOTHUB_ALLOW_REMOTE=1 and a separate security boundary.')
    if os.environ.get('DOTHUB_AUTO_COLLECT','1')!='0':
        def scheduled_collection():
            time.sleep(15)
            from tools.collect_sources import watch
            watch(int(os.environ.get('DOTHUB_COLLECT_INTERVAL_HOURS','12')))
        threading.Thread(target=scheduled_collection,name='dothub-collector',daemon=True).start()
    print(f'DotHub listening on http://{host}:{port} (database: {DB_PATH})')
    with make_server(host,port,app) as server: server.serve_forever()
if __name__=='__main__': main()
