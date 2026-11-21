"""SQLite 存取：单页方志、版次、标注与待复核清单。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "data/migration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editions (
    version INTEGER PRIMARY KEY CHECK (version >= 1),
    text TEXT NOT NULL,
    source_version INTEGER REFERENCES editions(version),
    alignment_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL REFERENCES editions(version),
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    label TEXT NOT NULL,
    source_annotation_id INTEGER REFERENCES annotations(id),
    created_at TEXT NOT NULL,
    CHECK (start >= 0),
    CHECK (end > start)
);

CREATE INDEX IF NOT EXISTS idx_annotations_version
    ON annotations(version);

CREATE TABLE IF NOT EXISTS review_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL REFERENCES editions(version),
    source_annotation_id INTEGER NOT NULL,
    source_version INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    label TEXT NOT NULL,
    old_text TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    reason TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_items_version
    ON review_items(version);

-- 外发补标包：发包时冻结现行版正文、遮盖后的正文与遮盖区间。
CREATE TABLE IF NOT EXISTS dispatch_packs (
    pack_no TEXT PRIMARY KEY,
    version INTEGER NOT NULL REFERENCES editions(version),
    masked_text TEXT NOT NULL,
    masked_spans_json TEXT NOT NULL,
    source_annotation_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 外发包回收回执：一个包只收一次，原样重送返回这里保存的回执。
CREATE TABLE IF NOT EXISTS dispatch_claims (
    pack_no TEXT PRIMARY KEY REFERENCES dispatch_packs(pack_no),
    version INTEGER NOT NULL REFERENCES editions(version),
    payload_json TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 回收得到的标注：保留外发稿坐标，便于审计；标注本身同时写入 annotations。
CREATE TABLE IF NOT EXISTS dispatch_claim_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_no TEXT NOT NULL REFERENCES dispatch_claims(pack_no),
    position INTEGER NOT NULL,
    annotation_id INTEGER NOT NULL REFERENCES annotations(id),
    version INTEGER NOT NULL REFERENCES editions(version),
    masked_start INTEGER NOT NULL,
    masked_end INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    label TEXT NOT NULL,
    UNIQUE(pack_no, position),
    CHECK (masked_start >= 0),
    CHECK (masked_end > masked_start),
    CHECK (start >= 0),
    CHECK (end > start)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_claim_annotations_pack
    ON dispatch_claim_annotations(pack_no);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    path = Path(db_path())
    if path.parent and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
