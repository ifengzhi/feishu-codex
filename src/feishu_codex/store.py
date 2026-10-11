import json
import sqlite3
import time
from pathlib import Path


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path, redact):
        self.redact = redact
        self.db = sqlite3.connect(path, isolation_level=None)
        Path(path).chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT, cwd TEXT NOT NULL,
          prompt TEXT NOT NULL, pending TEXT, thread TEXT UNIQUE, turn TEXT,
          status TEXT NOT NULL, summary TEXT DEFAULT '', created REAL, updated REAL,
          idem TEXT, fingerprint TEXT, UNIQUE(owner, idem));
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, kind TEXT, body TEXT, at REAL);
        CREATE INDEX IF NOT EXISTS events_task ON events(task,seq);
        CREATE TABLE IF NOT EXISTS requests(
          id TEXT PRIMARY KEY, task TEXT, rpc_id TEXT, epoch TEXT, method TEXT,
          params TEXT, decisions TEXT, status TEXT, expires REAL, decision TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS request_wire_id ON requests(epoch,rpc_id);
        CREATE TABLE IF NOT EXISTS dedup(
          id TEXT PRIMARY KEY, status TEXT, result TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS identities(open_id TEXT PRIMARY KEY, seen REAL);
        CREATE TABLE IF NOT EXISTS ui_sessions(
          owner TEXT PRIMARY KEY, chat TEXT, current_task TEXT, stage TEXT,
          data TEXT, flow TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS outbox(
          id TEXT PRIMARY KEY, task TEXT, chat TEXT, kind TEXT, body TEXT,
          status TEXT, attempts INTEGER DEFAULT 0, next_at REAL DEFAULT 0,
          message_id TEXT, error TEXT DEFAULT '', version INTEGER DEFAULT 1);
        ''')

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args)]

    def run(self, sql, args=()):
        return self.db.execute(sql, args)

    def task(self, tid):
        from .config import UserError
        t = self.one("SELECT * FROM tasks WHERE id=?", (tid,))
        if not t:
            raise UserError("任务不存在；请提供完整任务编号")
        return t

    def update(self, tid, **values):
        values = self.redact(values)
        values["updated"] = time.time()
        self.run("UPDATE tasks SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                 (*values.values(), tid))

    def event(self, tid, kind, body):
        self.run("INSERT INTO events(task,kind,body,at) VALUES(?,?,?,?)",
                 (tid, kind, encode(self.redact(body)), time.time()))

    def notify(self, key, task, chat, kind, body, refresh=False):
        if not chat:
            return
        body = encode(self.redact(body))
        self.run("INSERT OR IGNORE INTO outbox(id,task,chat,kind,body,status) VALUES(?,?,?,?,?,'pending')",
                 (key, task, chat, kind, body))
        if refresh:
            self.run("UPDATE outbox SET body=?,status='pending',attempts=0,next_at=0,version=version+1 WHERE id=?",
                     (body, key))
