"""Our own copy of Project Gutenberg's catalogue: every book, every language, searchable
instantly. Built from Gutenberg's published CSV (no API key) into its own SQLite file, refreshed
weekly in the background, so search doesn't depend on any third-party service being up."""
import csv
import gzip
import io
import re
import sqlite3
import threading
import time
from contextlib import closing

import httpx

from store import DATA

CATALOG_URL = "https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz"
PATH = DATA / "catalog.db"
MAX_AGE = 7 * 86400
_building = threading.Lock()

# languages offered in Discover, by size of the collection
LANGUAGES = [("en", "English"), ("fr", "Français"), ("de", "Deutsch"), ("es", "Español"), ("it", "Italiano"),
             ("pt", "Português"), ("nl", "Nederlands"), ("fi", "Suomi"), ("zh", "中文"), ("sv", "Svenska"),
             ("el", "Ελληνικά"), ("hu", "Magyar"), ("la", "Latina"), ("eo", "Esperanto")]


def display_authors(raw: str) -> str:
    """'Balzac, Honoré de, 1799-1850; Wormeley, Katharine' -> 'Honoré de Balzac, Katharine Wormeley'."""
    names = []
    parts = [p for p in raw.split(";") if "Illustrator" not in p]  # nobody searches a classic by its illustrator
    for part in parts[:2]:
        part = re.sub(r"\[[^\]]*\]", "", part)  # [Translator] and the like
        bits = [b.strip() for b in part.split(",") if b.strip() and not re.search(r"\d", b)]
        if not bits:
            continue
        names.append(f"{' '.join(bits[1:])} {bits[0]}".strip() if len(bits) > 1 else bits[0])
    return ", ".join(names)


def clean_title(t: str) -> str:
    """Library-catalogue leftovers out of titles: 'Lucifer : $b roman moderne' -> 'Lucifer: roman moderne'."""
    t = re.sub(r"\s*\$[a-z]\s*", " ", t)
    return re.sub(r"\s+:", ":", " ".join(t.split()))


def _build():
    raw = httpx.get(CATALOG_URL, timeout=120, follow_redirects=True).content
    rows = csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8"))
    tmp = PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    with closing(sqlite3.connect(tmp)) as c:
        c.executescript("""
            CREATE TABLE books (gid INTEGER PRIMARY KEY, title TEXT, authors TEXT, author_raw TEXT,
                                lang TEXT, subjects TEXT, shelves TEXT, issued TEXT);
            CREATE VIRTUAL TABLE books_fts USING fts5(title, authors, subjects, content='books', content_rowid='gid',
                                                     tokenize='unicode61 remove_diacritics 2');
        """)
        batch = []
        for r in rows:
            if r["Type"] != "Text" or not r["Text#"].isdigit() or r["Title"].startswith("Index of the Project Gutenberg"):
                continue
            title = clean_title(r["Title"])
            batch.append((int(r["Text#"]), title, display_authors(r["Authors"]), r["Authors"],
                          r["Language"].split(";")[0].strip(), r["Subjects"], r["Bookshelves"], r["Issued"]))
        c.executemany("INSERT INTO books VALUES (?,?,?,?,?,?,?,?)", batch)
        c.execute("INSERT INTO books_fts(rowid, title, authors, subjects) SELECT gid, title, authors, subjects FROM books")
        c.execute("CREATE INDEX books_lang ON books(lang)")
        c.commit()
    tmp.replace(PATH)  # readers keep using the old file until this instant
    return len(batch)


def refresh(force: bool = False):
    """Rebuild if missing or a week old. Safe to call from several places: one build at a time."""
    if not force and PATH.exists() and time.time() - PATH.stat().st_mtime < MAX_AGE:
        return
    if not _building.acquire(blocking=False):
        return
    try:
        _build()
    except Exception as e:  # keep serving the old catalogue; try again next start
        print(f"catalog refresh failed: {e!r}")
    finally:
        _building.release()


def ready() -> bool:
    return PATH.exists()


def _query(q: str) -> str:
    """User text -> an FTS5 query: every word must match, the last one as a prefix ("pride and prej")."""
    words = re.findall(r"\w+", q.lower())[:8]
    if not words:
        return ""
    return " ".join(f'"{w}"' for w in words[:-1]) + (" " if len(words) > 1 else "") + f'"{words[-1]}"*'


def _norm(t: str) -> str:
    t = " ".join(re.findall(r"\w+", t.lower()))
    return re.sub(r"^(the|a|an|le|la|les|der|die|das|el|il) ", "", t)


def search(q: str, lang: str = "", limit: int = 30, offset: int = 0, titles_only: bool = False) -> list[dict]:
    """Full-text match, then re-ranked so the book people mean comes first: an exact title beats
    a title that merely contains the words ("The Odyssey" before "A Martian Odyssey")."""
    if not ready():
        return []
    fts = _query(q)
    if not fts:
        return []
    if titles_only:
        fts = "{title authors}: (" + fts + ")"
    sql = ("SELECT b.gid, b.title, b.authors, b.lang, bm25(books_fts, 10.0, 4.0, 1.0) AS score"
           " FROM books_fts f JOIN books b ON b.gid=f.rowid"
           " WHERE books_fts MATCH ?" + (" AND b.lang=?" if lang else "") +
           " ORDER BY score, b.gid LIMIT ?")
    args = [fts, *([lang] if lang else []), max(200, offset + limit)]
    with closing(sqlite3.connect(f"file:{PATH}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(sql, args)]
        except sqlite3.OperationalError:
            return []
    want = _norm(q)

    def rank(i_r):
        i, r = i_r
        t = _norm(r["title"].split(":")[0].split(";")[0])
        writer = set(_norm(r["authors"].split(",")[0]).split())
        bonus = (4 if want and set(want.split()) <= writer  # "dickens": his own books, before books about him
                 else 3 if t == want else 1.5 if t.startswith(want) else 0)
        # an author's works: oldest ids first, which is roughly how loved they are (Gutenberg did the classics first)
        return (r["lang"] != "en" and not lang, -bonus, r["gid"] if bonus == 4 else i)

    rows = [r for _, r in sorted(enumerate(rows), key=rank)]
    for r in rows:
        r.pop("score")
    return rows[offset:offset + limit]


def known(gids: list[int]) -> dict[int, dict]:
    """Which of these Gutenberg ids are readable texts, with their language."""
    if not ready() or not gids:
        return {}
    with closing(sqlite3.connect(f"file:{PATH}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(f"SELECT gid, title, authors, lang FROM books WHERE gid IN ({','.join('?' * len(gids))})", gids)
        return {r["gid"]: dict(r) for r in rows}
