"""Database: connection, schema and migrations."""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
# SHELFIE_DATA points at a persistent volume when deployed; locally everything sits beside the code
DATA = Path(os.environ.get("SHELFIE_DATA", BASE))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "shelfie.db"

STATUSES = ("want", "reading", "read", "dnf")
VISIBILITY = ("public", "friends", "private")
SCHEMA_VERSION = 2


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# Books are a catalog shared by everyone; entries, lists and annotations belong to a user.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    bio TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    goal INTEGER NOT NULL DEFAULT 0
);
-- one-time links that let someone set a new password without email
CREATE TABLE IF NOT EXISTS reset_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at TEXT
);
-- status: 'accepted', or 'pending' while a private account hasn't approved it
CREATE TABLE IF NOT EXISTS follows (
    follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    followee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (follower_id, followee_id)
);
CREATE INDEX IF NOT EXISTS follows_followee ON follows(followee_id, status);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    pages INTEGER,
    ol_key TEXT UNIQUE,
    isbn TEXT,
    gutenberg_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS books_gutenberg ON books(gutenberg_id);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'want',
    progress INTEGER NOT NULL DEFAULT 0,
    rating REAL,
    review TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'public',
    genres TEXT NOT NULL DEFAULT '',
    moods TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    warnings TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    position REAL NOT NULL DEFAULT 0,
    UNIQUE (user_id, book_id)
);
CREATE INDEX IF NOT EXISTS entries_user ON entries(user_id, status);
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS list_books (
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    PRIMARY KEY (list_id, book_id)
);
-- highlights and bookmarks in the reader; positions are (block index, char offset in the
-- block's text). ids come from the client so offline-created ones can be replayed safely.
CREATE TABLE IF NOT EXISTS annotations (
    id TEXT PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    block INTEGER NOT NULL,
    off INTEGER NOT NULL DEFAULT 0,
    end_block INTEGER,
    end_off INTEGER,
    color TEXT NOT NULL DEFAULT 'yellow',
    note TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    shared INTEGER NOT NULL DEFAULT 0,
    pos REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS annotations_owner ON annotations(user_id, book_id);
-- time spent in the reader, per local day and book; feeds streaks and stats
CREATE TABLE IF NOT EXISTS reading_log (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    seconds INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, book_id)
);
-- buddy reads: friends reading one book together, with spoiler-gated discussion
CREATE TABLE IF NOT EXISTS reads (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
-- status: invited, joined or left
CREATE TABLE IF NOT EXISTS read_members (
    read_id INTEGER NOT NULL REFERENCES reads(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (read_id, user_id)
);
-- pos is a 0..1 fraction of the book, so each reader only sees comments up to where they are
CREATE TABLE IF NOT EXISTS read_comments (
    id INTEGER PRIMARY KEY,
    read_id INTEGER NOT NULL REFERENCES reads(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pos REAL NOT NULL,
    chapter TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS read_comments_read ON read_comments(read_id, pos);
-- the friends feed. Who may see an event is decided when it is read, from the entry's
-- current visibility, so making a book private also hides its history.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    annotation_id TEXT REFERENCES annotations(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_user ON events(user_id, id);
CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, event_id)
);
"""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _to_v1(conn):
    """Single-user database -> accounts. Rows get user_id NULL; the first account to sign up
    claims them (see auth.signup). entries and lists lose their old global UNIQUE constraints,
    which SQLite can only do by rebuilding the table."""
    if "gutenberg_id" not in _cols(conn, "books"):
        conn.execute("ALTER TABLE books ADD COLUMN gutenberg_id INTEGER")
    if "position" not in _cols(conn, "entries"):
        conn.execute("ALTER TABLE entries ADD COLUMN position REAL NOT NULL DEFAULT 0")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")  # only takes effect outside a transaction
    conn.executescript("""
    BEGIN;
    CREATE TABLE entries_v1 (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'want',
        progress INTEGER NOT NULL DEFAULT 0,
        rating REAL,
        review TEXT NOT NULL DEFAULT '',
        visibility TEXT NOT NULL DEFAULT 'public',
        genres TEXT NOT NULL DEFAULT '',
        moods TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        warnings TEXT NOT NULL DEFAULT '',
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL,
        position REAL NOT NULL DEFAULT 0,
        UNIQUE (user_id, book_id)
    );
    INSERT INTO entries_v1 (id, book_id, status, progress, rating, review, visibility, genres, moods,
                            tags, warnings, started_at, finished_at, updated_at, position)
        SELECT id, book_id, status, progress, rating, review, visibility, genres, moods,
               tags, warnings, started_at, finished_at, updated_at, position FROM entries;
    DROP TABLE entries;
    ALTER TABLE entries_v1 RENAME TO entries;
    CREATE TABLE lists_v1 (
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        UNIQUE (user_id, name)
    );
    INSERT INTO lists_v1 (id, name) SELECT id, name FROM lists;
    DROP TABLE lists;
    ALTER TABLE lists_v1 RENAME TO lists;
    COMMIT;
    """)
    if "annotations" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        if "user_id" not in _cols(conn, "annotations"):
            conn.execute("ALTER TABLE annotations ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        if "shared" not in _cols(conn, "annotations"):
            conn.execute("ALTER TABLE annotations ADD COLUMN shared INTEGER NOT NULL DEFAULT 0")
        conn.execute("DROP INDEX IF EXISTS annotations_book")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    if bad:
        raise RuntimeError(f"migration left broken references: {bad[:5]}")


def migrate():
    with closing(db()) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "books" in existing and version < 1:
            # keep a copy of the pre-accounts database in case anything goes wrong
            backup = DB_PATH.with_name(DB_PATH.stem + ".before-accounts.db")
            if not backup.exists():
                with closing(sqlite3.connect(backup)) as dst:
                    conn.backup(dst)
            conn.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS books")[0])  # users..follows first
            _to_v1(conn)
        # v2 only adds columns and tables. Checked against the tables as they are now (a v0
        # database has just been rebuilt above); ALTER first so SCHEMA's indexes find them.
        now_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, col, ddl in (("users", "goal", "goal INTEGER NOT NULL DEFAULT 0"),
                                ("annotations", "pos", "pos REAL")):
            if table in now_tables and col not in _cols(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        conn.commit()
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()


ITEM_SQL = """
SELECT e.id AS entry_id, e.user_id, b.id AS book_id, b.title, b.author, b.cover_url, b.pages,
       b.gutenberg_id, e.position, e.status, e.progress, e.rating, e.review, e.visibility, e.genres, e.moods,
       e.tags, e.warnings, e.started_at, e.finished_at, e.updated_at,
       (SELECT group_concat(l.id) FROM list_books lb JOIN lists l ON l.id = lb.list_id
         WHERE lb.book_id = b.id AND l.user_id IS e.user_id) AS list_ids,
       (SELECT count(*) FROM annotations a WHERE a.book_id = b.id AND a.user_id IS e.user_id) AS notes_count
FROM entries e JOIN books b ON b.id = e.book_id
"""


def row_to_item(r):
    d = dict(r)
    d["rating"] = d["rating"] if d["rating"] else None
    return d
