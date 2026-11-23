import io, json, os, tempfile, unittest, zipfile, subprocess, shutil
from unittest.mock import patch
from pathlib import Path
from backend.database import initialize, connect, add_resource
from backend.main import safe_zip, app, validate_github_url
from tools.export_static import export
from tools.create_archive import create
from tools.collect_sources import collect_github, collect_reddit, collect_feed

class DotHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.db=self.root/'test.sqlite3'; initialize(self.db)
    def tearDown(self): self.tmp.cleanup()
    def test_empty_and_resource_search_data(self):
        with connect(self.db) as db:
            self.assertEqual(db.execute('select count(*) from resources').fetchone()[0],0)
            add_resource(db,{'title':'Niri Starter','description':'A compact desktop setup','categories':['Configurations'],'environments':['Niri'],'tags':['tiling']})
            self.assertEqual(db.execute('select title from resources').fetchone()[0],'Niri Starter')
    def test_safe_zip_and_traversal_rejected(self):
        good=io.BytesIO()
        with zipfile.ZipFile(good,'w') as z:z.writestr('config/niri.kdl','layout {}')
        names=safe_zip(good.getvalue(),self.root/'files'); self.assertEqual(names,['config/niri.kdl'])
        bad=io.BytesIO()
        with zipfile.ZipFile(bad,'w') as z:z.writestr('../escape','x')
        with self.assertRaises(ValueError):safe_zip(bad.getvalue(),self.root/'bad')
        self.assertFalse((self.root/'escape').exists())
        linked=io.BytesIO()
        with zipfile.ZipFile(linked,'w') as z:
            info=zipfile.ZipInfo('link');info.create_system=3;info.external_attr=(0o120777 << 16);z.writestr(info,'../../outside')
        with self.assertRaises(ValueError):safe_zip(linked.getvalue(),self.root/'linked')
    def test_github_url_validation_is_offline(self):
        self.assertEqual(validate_github_url('https://github.com/example/dots.git'),('example','dots'))
        for url in ['http://github.com/a/b','https://github.com.evil.test/a/b','https://user@github.com/a/b','https://github.com/a']:
            with self.assertRaises(ValueError): validate_github_url(url)
    def test_github_discovery_deduplicates_and_does_not_fetch_archives(self):
        repo={'full_name':'example/dotfiles','html_url':'https://github.com/example/dotfiles','owner':{'login':'example'},'license':{'spdx_id':'MIT'},'description':'Desktop config','stargazers_count':5,'default_branch':'main'}
        calls=[]
        def fetch(url): calls.append(url); return {'items':[repo]}
        with connect(self.db) as db:
            result=collect_github(db,fetch,queries=('dotfiles','niri dotfiles'))
            self.assertEqual(result,{'source':'github','checked':2,'added':1})
            row=db.execute('select method,title,source_url,storage_path,status from submissions').fetchone()
            self.assertEqual(tuple(row),('github','example/dotfiles','https://github.com/example/dotfiles','','pending'))
        self.assertEqual(len(calls),2)
        self.assertTrue(all('/search/repositories?' in url for url in calls))
    def test_reddit_uses_oauth_api_only_and_feed_deduplicates(self):
        def reddit_fetch(url,headers):
            self.assertTrue(url.startswith('https://oauth.reddit.com/'))
            self.assertEqual(headers['Authorization'],'Bearer approved-token')
            return {'data':{'children':[{'data':{'id':'abc','permalink':'/r/unixporn/comments/abc/config/','title':'My dotfiles','subreddit':'unixporn','author':'alice'}}]}}
        xml=b'<rss><channel><item><title>Rice post</title><link>https://example.org/dots</link><guid>post-1</guid><description>Desktop config</description></item></channel></rss>'
        with connect(self.db) as db:
            reddit=collect_reddit(db,'approved-token',reddit_fetch,queries=('dotfiles',))
            self.assertEqual(reddit['added'],1)
            with patch('tools.collect_sources._public_host',return_value=__import__('urllib.parse').parse.urlparse('https://example.org/feed.xml')):
                feed=collect_feed(db,'https://example.org/feed.xml',lambda u:xml,lambda u:True)
                again=collect_feed(db,'https://example.org/feed.xml',lambda u:xml,lambda u:True)
            self.assertEqual(feed['added'],1); self.assertEqual(again['added'],0)
            methods={r[0] for r in db.execute('select distinct method from submissions')}
            self.assertEqual(methods,{'reddit','feed'})
    def test_submission_method_migration_allows_discovery_sources(self):
        legacy=self.root/'legacy.sqlite3'
        raw=__import__('sqlite3').connect(legacy)
        raw.executescript("CREATE TABLE submissions(id INTEGER PRIMARY KEY,method TEXT NOT NULL CHECK(method IN ('zip','github')),title TEXT NOT NULL,source_url TEXT NOT NULL,creator TEXT DEFAULT '',license TEXT DEFAULT '',storage_path TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),created TEXT NOT NULL DEFAULT (datetime('now')),notes TEXT DEFAULT ''); CREATE TABLE moderation(id INTEGER PRIMARY KEY,submission_id INTEGER REFERENCES submissions(id) ON DELETE CASCADE,action TEXT NOT NULL,note TEXT DEFAULT '',created TEXT NOT NULL DEFAULT (datetime('now'))); INSERT INTO submissions(method,title,source_url) VALUES('github','Existing','https://github.com/example/old'); INSERT INTO moderation(submission_id,action) VALUES(1,'reviewed');")
        raw.close(); initialize(legacy)
        with connect(legacy) as db:
            db.execute("INSERT INTO submissions(method,title,source_url) VALUES('reddit','Found post','https://www.reddit.com/r/example/abc')")
            self.assertEqual(db.execute('select count(*) from moderation').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
    def test_export_contains_only_published_and_local_pages(self):
        with connect(self.db) as db:
            add_resource(db,{'title':'Public Niri','description':'Published','categories':['Configurations'],'environments':['Niri']},'published')
            add_resource(db,{'title':'Draft item'},'draft')
        dest=self.root/'site'; report=export(self.db,dest)
        self.assertEqual(report['pages'],6); self.assertTrue((dest/'resources/public-niri.html').is_file())
        self.assertTrue((dest/'categories/Configurations.html').is_file()); self.assertTrue((dest/'environments/Niri.html').is_file())
        self.assertEqual(report['broken_links'],[])
        self.assertFalse((dest/'resources/draft-item.html').exists())
        self.assertIn('Public Niri',(dest/'index.html').read_text())
    def test_archive_adds_attribution_manifest(self):
        src=self.root/'src';src.mkdir();(src/'a.conf').write_text('hello')
        target=create(src,self.root/'a.zip',{'title':'A','license':'MIT'})
        with zipfile.ZipFile(target) as z:
            self.assertIn('manifest.json',z.namelist()); self.assertIn('README.md',z.namelist()); self.assertIn('a.conf',z.namelist())
    def test_local_http_search_moderation_and_remote_admin_guard(self):
        import backend.database as database
        import backend.main as main
        def request(path,peer='127.0.0.1'):
            captured={}
            env={'PATH_INFO':path,'REQUEST_METHOD':'GET','QUERY_STRING':'','REMOTE_ADDR':peer,'CONTENT_LENGTH':'0','wsgi.input':io.BytesIO()}
            payload=b''.join(app(env,lambda status,headers:captured.update(status=status,headers=headers)))
            return captured['status'],payload
        previous=(database.DB_PATH,database.DATA_HOME,main.DB_PATH,main.STORAGE)
        database.DB_PATH=self.db; database.DATA_HOME=self.root; main.DB_PATH=self.db; main.STORAGE=self.root/'storage'
        try:
            status,home=request('/'); self.assertTrue(status.startswith('200')); self.assertIn(b'DotHub',home)
            status,admin=request('/admin'); self.assertTrue(status.startswith('200')); self.assertIn(b'Automatic discovery',admin); self.assertIn(b'Add feed',admin)
            status,_=request('/admin','192.0.2.4');self.assertTrue(status.startswith('403'))
            with connect(self.db) as db:
                folder=main.STORAGE/'submissions'/'local-submission';folder.mkdir(parents=True);(folder/'config.conf').write_text('sample')
                db.execute('INSERT INTO submissions(method,title,source_url,license,storage_path) VALUES(?,?,?,?,?)',('zip','Review me','local ZIP upload','Unknown',str(folder)))
            captured={}; payload=b'action=approve&redistributable=0'
            env={'PATH_INFO':'/admin/review/1','REQUEST_METHOD':'POST','QUERY_STRING':'','REMOTE_ADDR':'127.0.0.1','CONTENT_LENGTH':str(len(payload)),'wsgi.input':io.BytesIO(payload)}
            app(env,lambda status,headers:captured.update(status=status,headers=headers))
            self.assertTrue(captured['status'].startswith('303'))
            with connect(self.db) as db:
                self.assertEqual(db.execute('select status from submissions where id=1').fetchone()[0],'approved')
                self.assertEqual(db.execute('select status from resources where title="Review me"').fetchone()[0],'published')
                self.assertEqual(db.execute('select redistributable from licenses where name="Unknown"').fetchone()[0],0)
                self.assertEqual(db.execute('select redistribution_allowed from resources where title="Review me"').fetchone()[0],0)
                db.execute("INSERT INTO submissions(method,title,source_url,notes) VALUES('feed','Metadata only','https://example.org/post','feed summary')")
            captured={}; payload=b'action=approve&redistributable=0'
            env={'PATH_INFO':'/admin/review/2','REQUEST_METHOD':'POST','QUERY_STRING':'','REMOTE_ADDR':'127.0.0.1','CONTENT_LENGTH':str(len(payload)),'wsgi.input':io.BytesIO(payload)}
            app(env,lambda status,headers:captured.update(status=status,headers=headers))
            with connect(self.db) as db:
                self.assertEqual(db.execute('select archive_path from resources where title="Metadata only"').fetchone()[0],'')
                self.assertEqual(db.execute('select json_extract(metadata,"$.discovered_by") from resources where title="Metadata only"').fetchone()[0],'feed')
            captured={}; env={'PATH_INFO':'/api/resources','REQUEST_METHOD':'GET','QUERY_STRING':'q=Review','REMOTE_ADDR':'127.0.0.1','CONTENT_LENGTH':'0','wsgi.input':io.BytesIO()}
            result=json.loads(b''.join(app(env,lambda status,headers:captured.update(status=status,headers=headers))))
            self.assertEqual(result['items'][0]['title'],'Review me')
        finally:
            database.DB_PATH,database.DATA_HOME,main.DB_PATH,main.STORAGE=previous
    @unittest.skipUnless(shutil.which('unzip'),'unzip not available')
    def test_installer_archive_plan_does_not_write(self):
        archive=self.root/'sample.zip'
        with zipfile.ZipFile(archive,'w') as z:z.writestr('config/test.conf','x')
        result=subprocess.run(['bash',str(Path(__file__).parents[1]/'installers'/'installer.sh'),'plan',str(archive),str(self.root/'destination')],capture_output=True,text=True,check=True)
        self.assertIn('Would copy: config/test.conf',result.stdout)
        self.assertFalse((self.root/'destination').exists())

if __name__=='__main__':unittest.main()
