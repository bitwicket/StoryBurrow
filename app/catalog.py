import json
import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.environ.get('STORYBURROW_CATALOG_DB', '/data/catalog.sqlite3'))
SCHEMA_VERSION = 2


def connect(db_path=None):
    path = Path(db_path) if db_path is not None else DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    return conn


def initialize(db_path=None):
    with connect(db_path) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f'Catalog schema {version} is newer than supported {SCHEMA_VERSION}')
        if version == 0:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS games (
                    game_id TEXT PRIMARY KEY,
                    display_title TEXT NOT NULL,
                    description TEXT,
                    vndb_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS installations (
                    installation_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL REFERENCES games(game_id),
                    directory_name TEXT NOT NULL UNIQUE,
                    launcher_name TEXT NOT NULL,
                    installed_version TEXT,
                    source_type TEXT NOT NULL,
                    source_filename TEXT,
                    source_url TEXT,
                    installed_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('installed','missing','retired'))
                );
                CREATE INDEX IF NOT EXISTS idx_installations_game ON installations(game_id);
                CREATE TABLE IF NOT EXISTS metadata_fields (
                    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                    field_name TEXT NOT NULL,
                    field_value_json TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (game_id, field_name)
                );
                CREATE TABLE IF NOT EXISTS game_assets (
                    asset_id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL REFERENCES games(game_id) ON DELETE CASCADE,
                    asset_type TEXT NOT NULL,
                    storage_path TEXT,
                    source_url TEXT,
                    source_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_reviews (
                    upload_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    title TEXT NOT NULL,
                    installed_version TEXT,
                    vndb_id TEXT,
                    vndb_snapshot_json TEXT,
                    local_snapshot_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            ''')
            db.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')

        # Schema v2: installation-level save namespaces; nullable for unknown games.
        columns = {row[1] for row in db.execute('PRAGMA table_info(installations)')}
        for name in ('save_directory', 'save_directory_source', 'save_directory_confidence'):
            if name not in columns:
                db.execute(f'ALTER TABLE installations ADD COLUMN {name} TEXT')
        if version < 2:
            db.execute('PRAGMA user_version = 2')

        # Stage 3B additive ownership fields. Existing installations remain legacy.
        columns = {row[1] for row in db.execute('PRAGMA table_info(installations)')}
        for field in ('managed_save_namespace', 'save_mode'):
            if field not in columns:
                db.execute(f'ALTER TABLE installations ADD COLUMN {field} TEXT')


def save_review(upload_id, owner, title, version, vndb_id, vndb_snapshot, local_snapshot, db_path=None):
    if not (isinstance(title, str) and 0 < len(title.strip()) <= 200):
        raise ValueError('Title must be 1–200 characters')
    if not (isinstance(version, str) and len(version) <= 100):
        raise ValueError('Version must be 100 characters or fewer')
    if vndb_id is not None and (not isinstance(vndb_id, str) or len(vndb_id) > 40):
        raise ValueError('Invalid VNDB ID')
    now = int(time.time())
    with connect(db_path) as db:
        db.execute('''
            INSERT INTO import_reviews
              (upload_id,owner,title,installed_version,vndb_id,vndb_snapshot_json,local_snapshot_json,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(upload_id) DO UPDATE SET
              title=excluded.title, installed_version=excluded.installed_version,
              vndb_id=excluded.vndb_id, vndb_snapshot_json=excluded.vndb_snapshot_json,
              local_snapshot_json=excluded.local_snapshot_json, updated_at=excluded.updated_at
            WHERE import_reviews.owner=excluded.owner
        ''', (upload_id, owner, title.strip(), version.strip(), vndb_id,
              json.dumps(vndb_snapshot, ensure_ascii=False) if vndb_snapshot is not None else None,
              json.dumps(local_snapshot, ensure_ascii=False), now))
        row = db.execute('SELECT owner FROM import_reviews WHERE upload_id=?', (upload_id,)).fetchone()
        if row is None or row['owner'] != owner:
            raise PermissionError('Import review belongs to another user')
    return {'upload_id': upload_id, 'title': title.strip(), 'installed_version': version.strip(),
            'vndb_id': vndb_id, 'updated_at': now}


def delete_review(upload_id, owner, db_path=None):
    with connect(db_path) as db:
        db.execute('DELETE FROM import_reviews WHERE upload_id=? AND owner=?', (upload_id, owner))


def get_review(upload_id, owner, db_path=None):
    with connect(db_path) as db:
        row = db.execute('SELECT * FROM import_reviews WHERE upload_id=? AND owner=?', (upload_id, owner)).fetchone()
        return dict(row) if row else None


def register_installation(upload_id, owner, folder_name, launcher, source_type, source_filename, db_path=None, *, installation_id=None, managed_save_namespace=None):
    import uuid
    now = int(time.time())
    game_id = str(uuid.uuid4())
    installation_id = installation_id or str(uuid.uuid4())
    uuid.UUID(installation_id)
    with connect(db_path) as db:
        review = db.execute('SELECT * FROM import_reviews WHERE upload_id=? AND owner=?',
                            (upload_id, owner)).fetchone()
        if review is None:
            raise ValueError('Save a review before installing')
        db.execute('''INSERT INTO games(game_id,display_title,description,vndb_id,created_at,updated_at)
                      VALUES (?,?,?,?,?,?)''',
                   (game_id, review['title'], None, review['vndb_id'], now, now))
        db.execute('''INSERT INTO installations
                      (installation_id,game_id,directory_name,launcher_name,installed_version,
                       source_type,source_filename,source_url,installed_at,status,managed_save_namespace,save_mode)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (installation_id, game_id, folder_name, launcher, review['installed_version'],
                    source_type, source_filename, None, now, 'installed', managed_save_namespace,
                    'managed' if managed_save_namespace else 'legacy'))
        local = json.loads(review['local_snapshot_json'])
        for field, value, source in (
            ('title', review['title'], 'user_review'),
            ('installed_version', review['installed_version'], 'user_review'),
            ('local_inspection', local, 'import_inspector'),
        ):
            db.execute('''INSERT INTO metadata_fields
                          (game_id,field_name,field_value_json,source_type,source_ref,updated_at)
                          VALUES (?,?,?,?,?,?)''',
                       (game_id, field, json.dumps(value, ensure_ascii=False), source, None, now))
        if review['vndb_snapshot_json']:
            db.execute('''INSERT INTO metadata_fields
                          (game_id,field_name,field_value_json,source_type,source_ref,updated_at)
                          VALUES (?,?,?,?,?,?)''',
                       (game_id, 'vndb_snapshot', review['vndb_snapshot_json'], 'vndb', review['vndb_id'], now))
        db.execute('DELETE FROM import_reviews WHERE upload_id=? AND owner=?', (upload_id, owner))
    return {'game_id': game_id, 'installation_id': installation_id, 'directory_name': folder_name}


def installed_by_directory(db_path=None):
    with connect(db_path) as db:
        rows = db.execute('''SELECT i.directory_name, g.game_id, g.display_title,
                                    i.installed_version, g.vndb_id, i.save_directory, i.save_directory_source, i.save_directory_confidence
                             FROM installations i JOIN games g ON g.game_id=i.game_id
                             WHERE i.status='installed' ''').fetchall()
    return {r['directory_name']: dict(r) for r in rows}


def remove_managed_installation(directory_name, expected_game_id, db_path=None):
    with connect(db_path) as db:
        rows = db.execute("SELECT installation_id, game_id FROM installations WHERE directory_name=? AND status='installed'",
                          (directory_name,)).fetchall()
        if len(rows) != 1 or rows[0]['game_id'] != expected_game_id:
            raise ValueError('Catalog installation changed during uninstall')
        db.execute('DELETE FROM installations WHERE installation_id=?', (rows[0]['installation_id'],))
        remaining = db.execute('SELECT COUNT(*) FROM installations WHERE game_id=?',
                               (expected_game_id,)).fetchone()[0]
        if remaining == 0:
            db.execute('DELETE FROM games WHERE game_id=?', (expected_game_id,))


def update_game_details(directory_name, title, installed_version, db_path=None):
    # Update only an installed catalog-managed game; never create records for legacy folders.
    if not isinstance(title, str) or not 0 < len(title.strip()) <= 200:
        raise ValueError('Title must be between 1 and 200 characters')
    if not isinstance(installed_version, str) or len(installed_version) > 100:
        raise ValueError('Version must be 100 characters or fewer')
    now = int(time.time())
    with connect(db_path) as db:
        row = db.execute('''SELECT i.game_id FROM installations i
                            WHERE i.directory_name=? AND i.status='installed' ''',
                         (directory_name,)).fetchone()
        if row is None:
            raise ValueError('This game is not managed by the catalog')
        game_id = row['game_id']
        db.execute('UPDATE games SET display_title=?, updated_at=? WHERE game_id=?',
                   (title.strip(), now, game_id))
        db.execute('''UPDATE installations SET installed_version=?
                      WHERE directory_name=? AND status='installed' ''',
                   (installed_version.strip(), directory_name))
        for field, value in [('title', title.strip()),
                             ('installed_version', installed_version.strip())]:
            db.execute('''INSERT INTO metadata_fields
                          (game_id,field_name,field_value_json,source_type,source_ref,updated_at)
                          VALUES (?,?,?,?,?,?)
                          ON CONFLICT(game_id,field_name) DO UPDATE SET
                            field_value_json=excluded.field_value_json,
                            source_type=excluded.source_type,
                            source_ref=NULL,
                            updated_at=excluded.updated_at''',
                       (game_id, field, json.dumps(value, ensure_ascii=False),
                        'user_edit', None, now))
    return game_id


def update_installation_save_directory(directory_name, result, db_path=None):
    name = result.get('save_directory')
    source = result.get('save_directory_source')
    confidence = result.get('save_directory_confidence', 'unknown')
    if name is not None:
        from save_inspector import _safe_id
        if _safe_id(name) != name:
            raise ValueError('Invalid save directory name')
    with connect(db_path) as db:
        cursor = db.execute("""UPDATE installations
            SET save_directory=?, save_directory_source=?, save_directory_confidence=?
            WHERE directory_name=? AND status='installed' """,
            (name, source, confidence, directory_name))
        if cursor.rowcount != 1:
            raise ValueError('Managed installation not found')
