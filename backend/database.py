"""SQLite schema and small persistence helpers for DotHub."""
from __future__ import annotations
import json, os, sqlite3
from pathlib import Path

DATA_HOME = Path(os.environ.get("DOTHUB_DATA", Path.home() / ".local/share/dothub"))
DB_PATH = Path(os.environ.get("DOTHUB_DB", DATA_HOME / "dothub.sqlite3"))

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS environments(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL DEFAULT 'window-manager');
CREATE TABLE IF NOT EXISTS distributions(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS licenses(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, redistributable INTEGER NOT NULL DEFAULT 0, url TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS resources(id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', creator TEXT NOT NULL DEFAULT 'Unknown', source_url TEXT NOT NULL DEFAULT '', repository_url TEXT NOT NULL DEFAULT '', license_id INTEGER REFERENCES licenses(id), redistribution_allowed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')), verification TEXT NOT NULL DEFAULT 'Untested' CHECK(verification IN ('Verified','Untested','Outdated','Incompatible')), version TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT (date('now')), created TEXT NOT NULL DEFAULT (datetime('now')), download_count INTEGER NOT NULL DEFAULT 0, archive_path TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS resource_categories(resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, category_id INTEGER REFERENCES categories(id), PRIMARY KEY(resource_id,category_id));
CREATE TABLE IF NOT EXISTS resource_environments(resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, environment_id INTEGER REFERENCES environments(id), PRIMARY KEY(resource_id,environment_id));
CREATE TABLE IF NOT EXISTS resource_distributions(resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, distribution_id INTEGER REFERENCES distributions(id), version TEXT DEFAULT '', PRIMARY KEY(resource_id,distribution_id));
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS resource_tags(resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, tag_id INTEGER REFERENCES tags(id), PRIMARY KEY(resource_id,tag_id));
CREATE TABLE IF NOT EXISTS media(id INTEGER PRIMARY KEY, resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, path TEXT NOT NULL, alt TEXT NOT NULL DEFAULT '', media_type TEXT NOT NULL DEFAULT 'image');
CREATE TABLE IF NOT EXISTS dependencies(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', website TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Unverified', checked TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS install_methods(id INTEGER PRIMARY KEY, dependency_id INTEGER REFERENCES dependencies(id) ON DELETE CASCADE, platform TEXT NOT NULL, package TEXT DEFAULT '', command TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'unverified', source_url TEXT DEFAULT '', verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS resource_dependencies(resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE, dependency_id INTEGER REFERENCES dependencies(id), notes TEXT DEFAULT '', PRIMARY KEY(resource_id,dependency_id));
CREATE TABLE IF NOT EXISTS setups(id INTEGER PRIMARY KEY, resource_id INTEGER UNIQUE REFERENCES resources(id) ON DELETE CASCADE, instructions TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS setup_resources(setup_id INTEGER REFERENCES setups(id) ON DELETE CASCADE, resource_id INTEGER REFERENCES resources(id), version_requirement TEXT DEFAULT '', PRIMARY KEY(setup_id,resource_id));
CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, method TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL, creator TEXT DEFAULT '', license TEXT DEFAULT '', storage_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')), created TEXT NOT NULL DEFAULT (datetime('now')), notes TEXT DEFAULT '', external_key TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS moderation(id INTEGER PRIMARY KEY, submission_id INTEGER REFERENCES submissions(id) ON DELETE CASCADE, action TEXT NOT NULL, note TEXT DEFAULT '', created TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS collection_sources(id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'feed', name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL DEFAULT (datetime('now')), last_checked TEXT DEFAULT '', last_status TEXT DEFAULT '', last_error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied TEXT NOT NULL DEFAULT (datetime('now')));
CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
CREATE INDEX IF NOT EXISTS idx_resources_created ON resources(created);
CREATE INDEX IF NOT EXISTS idx_submission_status ON submissions(status);
'''
CATEGORIES = ['Window Managers','Desktop Environments','Configurations','Wallpapers','Themes','Icons','Cursors','Widgets','Bars','Scripts','Applications','Fonts','Other Resources','Setups']
ENVIRONMENTS = [('Niri','window-manager'),('Hyprland','window-manager'),('Sway','window-manager'),('i3','window-manager'),('AwesomeWM','window-manager'),('Qtile','window-manager'),('KDE Plasma','desktop-environment'),('GNOME','desktop-environment'),('XFCE','desktop-environment')]

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try: return super().__exit__(exc_type,exc,tb)
        finally: self.close()

def connect(path: Path|str|None=None):
    db=sqlite3.connect(path or DB_PATH, factory=ClosingConnection); db.row_factory=sqlite3.Row; db.execute('PRAGMA foreign_keys=ON'); return db

def initialize(path: Path|str|None=None):
    target=Path(path or DB_PATH); target.parent.mkdir(parents=True,exist_ok=True)
    with connect(target) as db:
        db.executescript(SCHEMA)
        columns={row['name'] for row in db.execute('PRAGMA table_info(resources)')}
        current=db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
        if current < 1: db.execute('INSERT INTO schema_migrations(version) VALUES(1)')
        if current < 2:
            if 'redistribution_allowed' not in columns:
                db.execute('ALTER TABLE resources ADD COLUMN redistribution_allowed INTEGER NOT NULL DEFAULT 0')
            db.execute('INSERT INTO schema_migrations(version) VALUES(2)')
        current=db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
        if current < 3:
            submission_sql=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='submissions'").fetchone()[0].lower()
            submission_columns={row['name'] for row in db.execute('PRAGMA table_info(submissions)')}
            if 'check(method in' in submission_sql:
                db.commit()
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE submissions_new(id INTEGER PRIMARY KEY, method TEXT NOT NULL, title TEXT NOT NULL, source_url TEXT NOT NULL, creator TEXT DEFAULT '', license TEXT DEFAULT '', storage_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')), created TEXT NOT NULL DEFAULT (datetime('now')), notes TEXT DEFAULT '', external_key TEXT NOT NULL DEFAULT '')''')
                oldcols={row['name'] for row in db.execute('PRAGMA table_info(submissions)')}
                ext="external_key" if 'external_key' in oldcols else "''"
                db.execute(f'''INSERT INTO submissions_new(id,method,title,source_url,creator,license,storage_path,status,created,notes,external_key) SELECT id,method,title,source_url,creator,license,storage_path,status,created,notes,{ext} FROM submissions''')
                db.execute('DROP TABLE submissions')
                db.execute('ALTER TABLE submissions_new RENAME TO submissions')
                db.commit(); db.execute('PRAGMA foreign_keys=ON')
                db.execute('CREATE INDEX IF NOT EXISTS idx_submission_status ON submissions(status)')
            elif 'external_key' not in submission_columns:
                db.execute("ALTER TABLE submissions ADD COLUMN external_key TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_submission_external_key ON submissions(method,external_key) WHERE external_key<>''")
            db.execute('INSERT INTO schema_migrations(version) VALUES(3)')
        db.executemany('INSERT OR IGNORE INTO categories(name,slug) VALUES(?,?)', [(c,c.lower().replace(' ','-')) for c in CATEGORIES])
        db.executemany('INSERT OR IGNORE INTO environments(name,kind) VALUES(?,?)', ENVIRONMENTS)
        db.executemany('INSERT OR IGNORE INTO distributions(name) VALUES(?)', [(n,) for n in ['Arch Linux','Debian/Ubuntu','Fedora','openSUSE']])
        db.execute('INSERT OR IGNORE INTO licenses(name,redistributable,url) VALUES(?,?,?)', ('Unknown',0,''))
    return target

def add_resource(db, data, status='published'):
    import re
    slug=data.get('slug') or re.sub(r'[^a-z0-9]+','-',data['title'].lower()).strip('-')
    cur=db.execute('''INSERT INTO resources(slug,title,description,creator,source_url,repository_url,license_id,redistribution_allowed,status,verification,version,archive_path,metadata)
      VALUES(?,?,?,?,?,?,(SELECT id FROM licenses WHERE name=?),?,?,?,?,?,?)''',
      (slug,data['title'],data.get('description',''),data.get('creator','Unknown'),data.get('source_url',''),data.get('repository_url',''),data.get('license','Unknown'),1 if data.get('redistributable') else 0,status,data.get('verification','Untested'),data.get('version',''),data.get('archive_path',''),json.dumps(data.get('metadata',{}))))
    rid=cur.lastrowid
    for category in data.get('categories',[]):
        db.execute('INSERT OR IGNORE INTO categories(name,slug) VALUES(?,?)',(category,category.lower().replace(' ','-')))
        db.execute('INSERT OR IGNORE INTO resource_categories SELECT ?,id FROM categories WHERE name=?',(rid,category))
    for env in data.get('environments',[]):
        db.execute('INSERT OR IGNORE INTO environments(name) VALUES(?)',(env,))
        db.execute('INSERT OR IGNORE INTO resource_environments SELECT ?,id FROM environments WHERE name=?',(rid,env))
    for tag in data.get('tags',[]):
        db.execute('INSERT OR IGNORE INTO tags(name) VALUES(?)',(tag,))
        db.execute('INSERT OR IGNORE INTO resource_tags SELECT ?,id FROM tags WHERE name=?',(rid,tag))
    if 'Setups' in data.get('categories',[]): db.execute('INSERT INTO setups(resource_id,instructions) VALUES(?,?)',(rid,data.get('instructions','')))
    return rid

def resource_dict(db,row):
    result=dict(row)
    for key,sql in [('categories','SELECT c.name FROM categories c JOIN resource_categories x ON x.category_id=c.id WHERE x.resource_id=?'),('environments','SELECT e.name FROM environments e JOIN resource_environments x ON x.environment_id=e.id WHERE x.resource_id=?'),('tags','SELECT t.name FROM tags t JOIN resource_tags x ON x.tag_id=t.id WHERE x.resource_id=?'),('media','SELECT path,alt,media_type FROM media WHERE resource_id=?')]: result[key]=[dict(r) if key=='media' else r[0] for r in db.execute(sql,(row['id'],))]
    return result
