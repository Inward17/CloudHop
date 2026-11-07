"""SQLite storage. Every account and project belongs to one user; queries always filter by user_id.

ponytail: one SQLite file and one connection, used from the event loop thread only. Enough for one
server; move to Postgres when Cloudhop runs on more than one machine.
"""
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, github_id INTEGER UNIQUE, login TEXT NOT NULL, name TEXT, email TEXT, avatar_url TEXT,
  created_at INTEGER NOT NULL, last_login_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', secret_enc TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL DEFAULT 'github', repo TEXT NOT NULL, account_id TEXT NOT NULL, report_hash TEXT NOT NULL,
  clouds TEXT NOT NULL DEFAULT '{}', active TEXT, previous TEXT, url TEXT, pr_url TEXT, portability TEXT, created_at INTEGER NOT NULL,
  UNIQUE (user_id, repo));
CREATE TABLE IF NOT EXISTS sessions (id_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS accounts_by_user ON accounts (user_id);
CREATE INDEX IF NOT EXISTS projects_by_user ON projects (user_id);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, at INTEGER NOT NULL, event TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checks (project_id TEXT NOT NULL, at INTEGER NOT NULL, ok INTEGER NOT NULL, status INTEGER, ms INTEGER);
CREATE INDEX IF NOT EXISTS checks_by_project ON checks (project_id, at);
CREATE INDEX IF NOT EXISTS reports_by_project ON reports (project_id, at);
CREATE TABLE IF NOT EXISTS project_secrets (project_id TEXT NOT NULL, name TEXT NOT NULL, value_enc TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY (project_id, name));
CREATE TABLE IF NOT EXISTS moves (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at INTEGER NOT NULL, status TEXT NOT NULL, plan TEXT NOT NULL, state TEXT NOT NULL DEFAULT '{}');
"""
# Columns added after the first release. SQLite cannot add them in CREATE TABLE IF NOT EXISTS, so add when missing.
COLUMNS = {'projects': [('kind', "TEXT NOT NULL DEFAULT 'cloud'"), ('hosting', "TEXT NOT NULL DEFAULT '{}'")]}

_conn = None


def connect(path=None):
    """Opens (and creates) the database. DATABASE_PATH picks the file; ':memory:' is for tests."""
    global _conn
    path = path or os.environ.get('DATABASE_PATH') or str(Path(__file__).resolve().parent.parent / 'data' / 'cloudhop.db')
    if path != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    _conn.row_factory = lambda cursor, row: {column[0]: row[index] for index, column in enumerate(cursor.description)}
    _conn.execute('PRAGMA journal_mode=WAL')
    _conn.executescript(SCHEMA)
    for table, columns in COLUMNS.items():
        have = {row['name'] for row in _conn.execute(f'PRAGMA table_info({table})').fetchall()}
        for column, spec in columns:
            if column not in have:
                _conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {spec}')
    return _conn


def conn():
    return _conn or connect()


def one(sql, *args):
    return conn().execute(sql, args).fetchone()


def all(sql, *args):  # noqa: A001 - reads naturally at call sites: db.all(...)
    return conn().execute(sql, args).fetchall()


def run(sql, *args):
    conn().execute(sql, args)


def batch(statements):
    """Runs (sql, args) pairs in one transaction."""
    connection = conn()
    with connection:
        connection.execute('BEGIN')
        for sql, args in statements:
            connection.execute(sql, args)
