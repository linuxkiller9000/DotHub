"""Conservative metadata discovery for public dotfile sources.

No repository/archive or Reddit media is downloaded. Findings enter the local
submission queue, where an administrator can inspect and import them.
"""
from __future__ import annotations
import argparse, ipaddress, json, os, re, socket, sys, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from backend.database import DB_PATH, connect, initialize

USER_AGENT='DotHub-Discovery/1.0 (+local Linux customization catalog)'
MAX_FEED_BYTES=1_000_000
GITHUB_QUERIES=('dotfiles in:name','hyprland dotfiles','niri dotfiles','linux rice')
REDDIT_QUERIES=('dotfiles','linux rice','hyprland dotfiles','niri dotfiles')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None

def _opener(): return urllib.request.build_opener(NoRedirect())

def _public_host(url):
    parsed=urllib.parse.urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Source URL must be HTTPS without embedded credentials')
    if parsed.port not in (None,443): raise ValueError('Non-standard HTTPS ports are not allowed')
    host=parsed.hostname.lower()
    if host in ('localhost','localhost.localdomain') or host.endswith('.local'):
        raise ValueError('Private or local source hosts are not allowed')
    try: addresses={item[4][0] for item in socket.getaddrinfo(host,None)}
    except OSError as exc: raise ValueError(f'Could not resolve source host: {exc}') from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Source resolves to a non-public address')
    return parsed

def fetch_json(url,headers=None,timeout=15,max_bytes=1_000_000):
    _public_host(url)
    request=urllib.request.Request(url,headers={'User-Agent':USER_AGENT,'Accept':'application/vnd.github+json, application/json',**(headers or {})})
    with _opener().open(request,timeout=timeout) as response:
        data=response.read(max_bytes+1)
        if len(data)>max_bytes: raise ValueError('Source response exceeds size limit')
        return json.loads(data.decode('utf-8'))

def _candidate(db,method,key,title,url,creator='',license='',notes=''):
    if not key or not title or not url: return False
    db.execute('''INSERT OR IGNORE INTO submissions(method,title,source_url,creator,license,status,notes,external_key)
      VALUES(?,?,?,?,?,'pending',?,?)''',(method,title[:180],url,creator[:120],license[:100],notes[:600],key[:300]))
    return db.execute('SELECT changes()').fetchone()[0]==1

def collect_github(db,fetch=fetch_json,queries=GITHUB_QUERIES):
    """Collect repository metadata from GitHub's public repository search."""
    added=0; checked=0
    for query in queries:
        url='https://api.github.com/search/repositories?'+urllib.parse.urlencode({'q':query,'sort':'updated','order':'desc','per_page':30})
        result=fetch(url)
        for repo in result.get('items',[]):
            name=repo.get('full_name','')
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',name): continue
            web_url=repo.get('html_url','')
            if urllib.parse.urlparse(web_url).hostname!='github.com': continue
            license_obj=repo.get('license') or {}
            license_name=license_obj.get('spdx_id') or 'Unknown'
            notes=f"GitHub candidate · {repo.get('description') or 'No repository description'} · {repo.get('stargazers_count',0)} stars · default branch {repo.get('default_branch') or 'unknown'}"
            added+=_candidate(db,'github','github:'+name.lower(),name,web_url,(repo.get('owner') or {}).get('login',''),license_name,notes)
            checked+=1
        db.commit()
    return {'source':'github','checked':checked,'added':added}

def collect_reddit(db,token,fetch=fetch_json,queries=REDDIT_QUERIES):
    """Use Reddit OAuth only; never fall back to HTML, RSS, cookies, or scraping."""
    if not token: return {'source':'reddit','checked':0,'added':0,'skipped':'authorized API token is not configured'}
    added=0; checked=0
    for query in queries:
        url='https://oauth.reddit.com/search.json?'+urllib.parse.urlencode({'q':query,'sort':'new','limit':50,'type':'link'})
        payload=fetch(url,{'Authorization':'Bearer '+token,'Accept':'application/json'})
        for item in ((payload.get('data') or {}).get('children') or []):
            post=item.get('data') or {}; post_id=post.get('id'); permalink=post.get('permalink')
            if not post_id or not permalink or not permalink.startswith('/r/') or '..' in permalink: continue
            title=(post.get('title') or '').strip()
            if not title: continue
            link='https://www.reddit.com'+permalink
            notes=f"Reddit discovery · r/{post.get('subreddit','unknown')} · Open the source post to inspect the linked dotfiles."
            added+=_candidate(db,'reddit','reddit:'+post_id,title,link,post.get('author',''),'',notes); checked+=1
        db.commit()
    return {'source':'reddit','checked':checked,'added':added}

def _robots_allow(url):
    parsed=_public_host(url); origin=f'{parsed.scheme}://{parsed.netloc}'
    robots_url=origin+'/robots.txt'
    try:
        req=urllib.request.Request(robots_url,headers={'User-Agent':USER_AGENT})
        with _opener().open(req,timeout=10) as response: data=response.read(256_000).decode('utf-8','replace')
    except urllib.error.HTTPError as exc:
        if exc.code==404: return True
        return False
    except (OSError,ValueError): return False
    parser=urllib.robotparser.RobotFileParser(); parser.set_url(robots_url); parser.parse(data.splitlines())
    return parser.can_fetch(USER_AGENT,url)

def collect_feed(db,url,fetch_bytes=None,robots_check=None):
    """Read a configured RSS/Atom feed only when its robots policy allows it."""
    parsed=_public_host(url)
    if not (robots_check or _robots_allow)(url): raise PermissionError('robots.txt disallows this feed or could not be checked')
    if fetch_bytes is None:
        request=urllib.request.Request(url,headers={'User-Agent':USER_AGENT,'Accept':'application/atom+xml, application/rss+xml, application/xml, text/xml'})
        with _opener().open(request,timeout=15) as response: blob=response.read(MAX_FEED_BYTES+1)
    else: blob=fetch_bytes(url)
    if len(blob)>MAX_FEED_BYTES: raise ValueError('Feed exceeds one megabyte')
    root=ET.fromstring(blob)
    added=checked=0
    for node in root.iter():
        tag=node.tag.rsplit('}',1)[-1].lower()
        if tag not in ('item','entry'): continue
        fields={child.tag.rsplit('}',1)[-1].lower():child for child in list(node)}
        title=' '.join(''.join(fields.get('title',ET.Element('title')).itertext()).split())
        link_node=fields.get('link'); link=(link_node.text or '').strip() if link_node is not None else ''
        if link_node is not None and link_node.attrib.get('href'): link=link_node.attrib['href']
        if not title or not link: continue
        absolute=urllib.parse.urljoin(url,link); p=urllib.parse.urlparse(absolute)
        if p.scheme!='https' or not p.hostname or p.username or p.password: continue
        guid_node=fields.get('guid')
        if guid_node is None: guid_node=fields.get('id')
        key=(guid_node.text or '').strip() if guid_node is not None else absolute
        if not key: key=absolute
        descnode=fields.get('description')
        if descnode is None: descnode=fields.get('summary')
        if descnode is None: descnode=fields.get('content')
        excerpt=' '.join(''.join(descnode.itertext()).split())[:400] if descnode is not None else ''
        added+=_candidate(db,'feed','feed:'+parsed.hostname+':'+key,title,absolute,parsed.hostname,'',f"Discovered via configured feed {url}. {excerpt}"); checked+=1
    db.commit()
    return {'source':url,'checked':checked,'added':added}

def collect_once(db_path=DB_PATH,reddit_token=None):
    initialize(db_path); report=[]
    token=reddit_token or os.environ.get('DOTHUB_REDDIT_BEARER_TOKEN','')
    with connect(db_path) as db:
        for collector in (lambda:collect_github(db),lambda:collect_reddit(db,token)):
            try: report.append(collector())
            except Exception as exc: report.append({'source':'github' if not report else 'reddit','error':str(exc)[:300]})
        feeds=db.execute('SELECT id,name,url FROM collection_sources WHERE enabled=1 ORDER BY id').fetchall()
        for feed in feeds:
            try:
                result=collect_feed(db,feed['url']); result['name']=feed['name']; status='ok'; error=''
            except Exception as exc:
                result={'source':feed['url'],'checked':0,'added':0,'error':str(exc)[:300]}; status='error'; error=str(exc)[:300]
            db.execute('UPDATE collection_sources SET last_checked=?,last_status=?,last_error=? WHERE id=?',(datetime.now(timezone.utc).isoformat(),status,error,feed['id']))
            report.append(result)
    return report

def watch(interval_hours=12):
    interval=max(1,min(int(interval_hours),168))*3600
    while True:
        try:
            for result in collect_once(): print(json.dumps(result,ensure_ascii=False),flush=True)
        except Exception as exc: print(json.dumps({'error':str(exc)[:300]}),file=sys.stderr,flush=True)
        time.sleep(interval)

def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--watch',action='store_true'); parser.add_argument('--interval-hours',type=int,default=12)
    args=parser.parse_args()
    if args.watch: watch(args.interval_hours)
    else: print(json.dumps(collect_once(),ensure_ascii=False,indent=2))

if __name__=='__main__': main()
