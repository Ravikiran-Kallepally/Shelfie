"""Shelfie - track books, read free classics, follow friends' shelves (SQLite).

Run:  uvicorn app:app
"""
import asyncio
import csv
import io
import json
import re
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import reads
import social
from auth import current_user, optional_user
from store import BASE, DATA, ITEM_SQL, STATUSES, VISIBILITY, db, migrate, now, row_to_item

CACHE = DATA / "cache"
CACHE.mkdir(exist_ok=True)

migrate()


@asynccontextmanager
async def lifespan(_app):
    warm = asyncio.create_task(warm_catalog())
    yield
    warm.cancel()


app = FastAPI(title="Shelfie", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # book text shrinks ~3x
app.include_router(auth.router)
app.include_router(social.router)
app.include_router(reads.router)


@app.middleware("http")
async def guard(request: Request, call_next):
    # cookies make every write ride along on cross-site requests; refuse writes from other sites
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


# ---------- models ----------
class BookIn(BaseModel):
    book_id: Optional[int] = None  # add a book that's already in the catalog, e.g. from a friend's shelf
    title: str = ""
    author: str = ""
    cover_url: Optional[str] = None
    pages: Optional[int] = None
    ol_key: Optional[str] = None
    isbn: Optional[str] = None
    gutenberg_id: Optional[int] = None
    status: str = "want"


class EntryPatch(BaseModel):
    status: Optional[str] = None
    progress: Optional[int] = Field(default=None, ge=0)
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review: Optional[str] = None
    visibility: Optional[str] = None
    genres: Optional[str] = None
    moods: Optional[str] = None
    tags: Optional[str] = None
    warnings: Optional[str] = None
    position: Optional[float] = Field(default=None, ge=0, le=1)


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


KINDS = ("highlight", "bookmark")
COLORS = ("yellow", "green", "blue", "pink", "red")


class AnnotationIn(BaseModel):
    book_id: int
    kind: str = "highlight"
    block: int = Field(ge=0)
    off: int = Field(default=0, ge=0)
    end_block: Optional[int] = Field(default=None, ge=0)
    end_off: Optional[int] = Field(default=None, ge=0)
    color: str = "yellow"
    note: str = Field(default="", max_length=20000)
    text: str = Field(default="", max_length=2000)
    pos: Optional[float] = Field(default=None, ge=0, le=1)  # where in the book, for spoiler-free sharing


class LogIn(BaseModel):
    book_id: int
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")  # the reader's local date
    seconds: int = Field(default=0, ge=0, le=3600)
    pages: int = Field(default=0, ge=0, le=2000)


class BulkIn(BaseModel):
    entry_ids: list[int] = Field(min_length=1, max_length=2000)
    status: Optional[str] = None
    list_id: Optional[int] = None
    remove: bool = False


class AnnotationPatch(BaseModel):
    color: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=20000)
    shared: Optional[bool] = None


# ---------- helpers ----------
def my_entry(conn, uid: int, entry_id: int) -> dict:
    row = conn.execute("SELECT * FROM entries WHERE id=? AND user_id=?", (entry_id, uid)).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


def my_list(conn, uid: int, list_id: int):
    if not conn.execute("SELECT 1 FROM lists WHERE id=? AND user_id=?", (list_id, uid)).fetchone():
        raise HTTPException(404, "list not found")


def add_book(conn, uid: int, b: BookIn, feed: bool = True) -> int:
    """Find or create the catalog book, then put it on this user's shelf (if it isn't already)."""
    if b.status not in STATUSES:
        raise HTTPException(400, "bad status")
    row = None
    if b.book_id:
        row = conn.execute("SELECT id FROM books WHERE id=?", (b.book_id,)).fetchone()
        if not row:
            raise HTTPException(404, "book not found")
    elif not b.title.strip():
        raise HTTPException(400, "title required")
    if not row and b.gutenberg_id:
        row = conn.execute("SELECT id FROM books WHERE gutenberg_id=?", (b.gutenberg_id,)).fetchone()
    if not row and b.ol_key:
        row = conn.execute("SELECT id FROM books WHERE ol_key=?", (b.ol_key,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT id FROM books WHERE lower(title)=lower(?) AND lower(author)=lower(?)",
            (b.title, b.author),
        ).fetchone()
    if row:
        book_id = row["id"]
        if b.gutenberg_id:
            # a shelf book that turns out to be free becomes readable
            conn.execute(
                "UPDATE books SET gutenberg_id=coalesce(gutenberg_id, ?), cover_url=coalesce(cover_url, ?)"
                " WHERE id=?",
                (b.gutenberg_id, b.cover_url, book_id),
            )
    else:
        cur = conn.execute(
            "INSERT INTO books(title,author,cover_url,pages,ol_key,isbn,gutenberg_id) VALUES(?,?,?,?,?,?,?)",
            (b.title.strip(), b.author, b.cover_url, b.pages, b.ol_key, b.isbn, b.gutenberg_id),
        )
        book_id = cur.lastrowid
    if not conn.execute("SELECT 1 FROM entries WHERE book_id=? AND user_id=?", (book_id, uid)).fetchone():
        started = now() if b.status == "reading" else None
        finished = now() if b.status == "read" else None
        entry_id = conn.execute(
            "INSERT INTO entries(user_id,book_id,status,started_at,finished_at,updated_at) VALUES(?,?,?,?,?,?)",
            (uid, book_id, b.status, started, finished, now()),
        ).lastrowid
        if feed:
            social.record_entry_change(conn, uid, entry_id, None, {"status": b.status, "rating": None, "review": ""})
    return book_id


# ---------- API ----------
@app.get("/api/search")
async def search(q: str, me: dict = Depends(current_user)):
    """Search Open Library. If the network is unreachable, returns an empty list
    and the UI falls back to manual add."""
    if not q.strip():
        return {"results": [], "online": True}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://openlibrary.org/search.json",
                params={
                    "q": q,
                    "limit": 20,
                    "fields": "key,title,author_name,cover_i,number_of_pages_median,isbn",
                },
            )
            r.raise_for_status()
            docs = r.json().get("docs", [])
    except Exception:
        return {"results": [], "online": False}
    out = []
    for d in docs:
        cover = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else None
        out.append(
            {
                "title": d.get("title", ""),
                "author": ", ".join(d.get("author_name", [])[:2]),
                "cover_url": cover,
                "pages": d.get("number_of_pages_median"),
                "ol_key": d.get("key"),
                "isbn": (d.get("isbn") or [None])[0],
            }
        )
    return {"results": out, "online": True}


# ---------- free books (Project Gutenberg) ----------
GUTENDEX = "https://gutendex.com/books/"
TOPICS = [
    ("", "Popular"), ("adventure", "Adventure"), ("romance", "Romance"), ("mystery", "Mystery"),
    ("science fiction", "Sci-fi"), ("fantasy", "Fantasy"), ("horror", "Horror"),
    ("children", "Children"), ("poetry", "Poetry"), ("philosophy", "Philosophy"),
    ("history", "History"), ("humor", "Humor"),
]
CATALOG_FILE = CACHE / "catalog.json"
CATALOG_TTL = 24 * 3600
# gutendex is slow (15s+ per topic), so pages are kept on disk and refreshed daily:
# {query string: [fetched_at, {"results": [...], "next": bool}]}
try:
    _catalog: dict = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
except (OSError, ValueError):
    _catalog = {}


def flip_name(name: str) -> str:
    """Gutenberg stores 'Austen, Jane'; show 'Jane Austen'."""
    last, _, first = name.partition(", ")
    return f"{first} {last}".strip() if first else name


def catalog_params(q: str = "", topic: str = "", page: int = 1, lang: str = "en") -> dict:
    params = {"page": page}
    if q.strip():
        params["search"] = q.strip()
    if topic.strip():
        params["topic"] = topic.strip()
    if lang:
        params["languages"] = lang
    return params


async def fetch_catalog(params: dict) -> Optional[dict]:
    """One catalog page, trimmed to what the UI shows. Serves a stale copy if gutendex is down."""
    key = urlencode(sorted(params.items()))
    hit = _catalog.get(key)
    if hit and time.time() - hit[0] < CATALOG_TTL:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(GUTENDEX, params=params)
        if r.status_code == 404:  # past the last page
            return {"results": [], "next": False}
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return hit[1] if hit else None
    out = []
    for d in raw.get("results", []):
        fmts = d.get("formats", {})
        if d.get("copyright") or not any(k.startswith("text/plain") for k in fmts):
            continue
        out.append(
            {
                "gutenberg_id": d["id"],
                "title": d.get("title", ""),
                "author": ", ".join(flip_name(a["name"]) for a in d.get("authors", [])[:2]),
                "cover_url": fmts.get("image/jpeg"),
            }
        )
    data = {"results": out, "next": bool(raw.get("next"))}
    _catalog[key] = [time.time(), data]
    if len(_catalog) > 500:  # drop the oldest
        for k in sorted(_catalog, key=lambda k: _catalog[k][0])[:100]:
            del _catalog[k]
    tmp = CATALOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_catalog), encoding="utf-8")
    tmp.replace(CATALOG_FILE)
    return data


async def warm_catalog():
    """Fetch the first page of every topic in the background so Discover opens instantly."""
    sem = asyncio.Semaphore(3)

    async def one(topic):
        async with sem:
            await fetch_catalog(catalog_params(topic=topic))

    await asyncio.gather(*(one(t) for t, _ in TOPICS), return_exceptions=True)


@app.get("/api/free/topics")
def free_topics():
    return [{"id": t, "label": label} for t, label in TOPICS]


@app.get("/api/free")
async def free_books(q: str = "", topic: str = "", page: int = 1, lang: str = "en",
                     me: dict = Depends(current_user)):
    """Browse/search public-domain books. Empty query = most popular."""
    data = await fetch_catalog(catalog_params(q, topic, page, lang))
    if data is None:
        return {"results": [], "next": False, "online": False}
    out = [dict(b) for b in data["results"]]
    ids = [b["gutenberg_id"] for b in out]
    if ids:
        with closing(db()) as conn:
            have = {
                r["gutenberg_id"]: r["entry_id"]
                for r in conn.execute(
                    f"SELECT b.gutenberg_id, e.id AS entry_id FROM books b JOIN entries e ON e.book_id=b.id"
                    f" WHERE e.user_id=? AND b.gutenberg_id IN ({','.join('?' * len(ids))})",
                    [me["id"], *ids],
                )
            }
        for b in out:
            b["entry_id"] = have.get(b["gutenberg_id"])
    return {"results": out, "next": data["next"], "online": True}


START_RE = re.compile(r"^\*\*\* ?START OF (THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
END_RE = re.compile(r"^\*\*\* ?END OF (THE|THIS) PROJECT GUTENBERG.*$", re.M | re.I)
ILLUS_RE = re.compile(r"\[Illustration[^\]]*\]", re.S | re.I)
# "CHAPTER IV." / "Chapter 4" / "BOOK THE FIRST" / bare "XII." -- capitalised keyword followed by a
# number or word, so prose lines that merely start with "book;" don't count
_NUM = r"\s+([IVXLCDM]+|\d+|[A-Z][A-Za-z]*)\b(\W.*)?$"
CHAPTER_RE = re.compile(
    r"^(CHAPTER|Chapter|BOOK|Book|PART|Part|VOLUME|Volume|ACT|Act|STAVE|Stave|LETTER|Letter)" + _NUM
    + r"|^(PROLOGUE|Prologue|EPILOGUE|Epilogue)\b|^[IVXLC]+\.?$"
    + r"|^[IVXLC]+\.\s+[A-Z][^a-z]*$"  # "IV. THE BOSCOMBE VALLEY MYSTERY"
)
HEADING_RE = re.compile(
    CHAPTER_RE.pattern + r"|^(SCENE|Scene)" + _NUM + r"|^(PREFACE|Preface|INTRODUCTION|Introduction|CONTENTS|Contents)\.?$"
)


def parse_gutenberg(raw: str) -> dict:
    """Plain text -> blocks the reader can lay out: ch (chapter heading), h (other heading),
    p (prose), v (verse, line breaks kept), c (centred). Also a table of contents pointing at block indexes."""
    text = raw.replace("\r\n", "\n")
    m = START_RE.search(text)
    if m:
        text = text[m.end():]
    m = END_RE.search(text)
    if m:
        text = text[: m.start()]
    text = ILLUS_RE.sub("", text)
    blocks = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [l.rstrip() for l in chunk.split("\n") if l.strip()]
        if not lines:
            continue
        joined = " ".join(l.strip() for l in lines)
        if len(lines) <= 2 and len(joined) < 80 and (HEADING_RE.match(joined) or joined.isupper()):
            if CHAPTER_RE.match(joined):
                blocks.append({"t": "ch", "s": joined})  # chapter: starts a new page
            else:
                blocks.append({"t": "h", "s": joined})
        elif len(lines) > 1 and (
            max(len(l) for l in lines) < 55 or all(l.startswith("  ") for l in lines)
        ):
            indent = min(len(l) - len(l.lstrip()) for l in lines)
            if indent >= 6:  # deeply indented = centred title pages, epigraphs
                blocks.append({"t": "c", "s": "\n".join(l.strip() for l in lines)})
            else:
                blocks.append({"t": "v", "s": "\n".join(l[indent:] for l in lines)})
        else:
            blocks.append({"t": "p", "s": joined})
    # 3+ chapter headings in a row is the book's printed contents list, not chapters
    i = 0
    while i < len(blocks):
        j = i
        while j < len(blocks) and blocks[j]["t"] == "ch":
            j += 1
        if j - i >= 3:
            for b in blocks[i:j]:
                b["t"] = "v"
        i = max(j, i + 1)
    toc = [{"title": b["s"].rstrip(".]"), "i": n} for n, b in enumerate(blocks) if b["t"] == "ch"]
    return {"blocks": blocks, "toc": toc}


_texts: dict[int, bytes] = {}  # gutenberg id -> parsed book as JSON, most recent last
_text_jobs: dict[int, asyncio.Task] = {}


async def build_text(gid: int) -> bytes:
    path = CACHE / f"pg{gid}.bin"  # raw bytes: text mode on Windows would mangle \r\n
    if not path.exists():
        urls = (
            f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt",
            f"https://www.gutenberg.org/ebooks/{gid}.txt.utf-8",
        )
        raw = None
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            for url in urls:
                try:
                    r = await client.get(url)
                    if r.status_code == 200 and r.content:
                        raw = r.content
                        break
                except httpx.HTTPError:
                    continue
        if raw is None:
            raise HTTPException(502, "couldn't download the book; check your connection")
        path.write_bytes(raw)
    text = path.read_bytes().decode("utf-8", errors="replace")
    parsed = await asyncio.to_thread(parse_gutenberg, text)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")).encode()


@app.get("/api/text/{gid}")
async def book_text(gid: int, me: dict = Depends(current_user)):
    """Parsed text of a Gutenberg book. Never changes, so the browser may cache it for good.
    A prefetch and the real open share one download via _text_jobs."""
    data = _texts.pop(gid, None)
    if data is None:
        job = _text_jobs.get(gid)
        if job is None:
            job = _text_jobs[gid] = asyncio.create_task(build_text(gid))
        try:
            data = await job
        finally:
            _text_jobs.pop(gid, None)
    _texts[gid] = data
    while len(_texts) > 12:
        del _texts[next(iter(_texts))]
    return Response(
        data,
        media_type="application/json",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.post("/api/library")
def add_to_library(b: BookIn, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        book_id = add_book(conn, me["id"], b)
        conn.commit()
        r = conn.execute(ITEM_SQL + " WHERE b.id=? AND e.user_id=?", (book_id, me["id"])).fetchone()
        return row_to_item(r)


@app.get("/api/library")
def library(status: Optional[str] = None, q: Optional[str] = None, list_id: Optional[int] = None,
            me: dict = Depends(current_user)):
    sql = ITEM_SQL + " WHERE e.user_id=?"
    args = [me["id"]]
    if status:
        sql += " AND e.status=?"
        args.append(status)
    if q:
        sql += " AND (b.title LIKE ? OR b.author LIKE ? OR e.tags LIKE ? OR e.genres LIKE ?)"
        args += [f"%{q}%"] * 4
    if list_id:
        sql += " AND b.id IN (SELECT lb.book_id FROM list_books lb JOIN lists l ON l.id=lb.list_id WHERE l.id=? AND l.user_id=?)"
        args += [list_id, me["id"]]
    sql += " ORDER BY e.updated_at DESC"
    with closing(db()) as conn:
        return [row_to_item(r) for r in conn.execute(sql, args)]


@app.patch("/api/library/{entry_id}")
def update_entry(entry_id: int, p: EntryPatch, me: dict = Depends(current_user)):
    data = p.model_dump(exclude_unset=True)
    # only rating may be cleared; every other column is NOT NULL
    data = {k: v for k, v in data.items() if v is not None or k == "rating"}
    if "status" in data and data["status"] not in STATUSES:
        raise HTTPException(400, "bad status")
    if "visibility" in data and data["visibility"] not in VISIBILITY:
        raise HTTPException(400, "bad visibility")
    if "rating" in data and data["rating"] is not None and (data["rating"] * 2) % 1 != 0:
        raise HTTPException(400, "rating must be in half-star steps")
    with closing(db()) as conn:
        cur = my_entry(conn, me["id"], entry_id)
        book = conn.execute("SELECT pages FROM books WHERE id=?", (cur["book_id"],)).fetchone()
        # opening a want-to-read book in the reader means you've started it
        if data.get("position") and "status" not in data and cur["status"] == "want":
            data["status"] = "reading"
        # status side effects
        if "status" in data and data["status"] != cur["status"]:
            if data["status"] == "reading" and not cur["started_at"]:
                data["started_at"] = now()
            if data["status"] == "read":
                data["finished_at"] = now()
                if not cur["started_at"]:
                    data["started_at"] = now()
                if book["pages"]:
                    data["progress"] = book["pages"]
        if "progress" in data and book["pages"]:
            data["progress"] = min(data["progress"], book["pages"])
            if data["progress"] >= book["pages"] and "status" not in data and cur["status"] == "reading":
                data["status"] = "read"
                data["finished_at"] = now()
        data["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in data)
        conn.execute(f"UPDATE entries SET {sets} WHERE id=?", [*data.values(), entry_id])
        social.record_entry_change(conn, me["id"], entry_id, cur, {**cur, **data})
        conn.commit()
        r = conn.execute(ITEM_SQL + " WHERE e.id=?", (entry_id,)).fetchone()
        return row_to_item(r)


@app.delete("/api/library/{entry_id}")
def remove_entry(entry_id: int, me: dict = Depends(current_user)):
    """Takes the book off your shelves. The catalog book stays: others may have it too."""
    with closing(db()) as conn:
        e = my_entry(conn, me["id"], entry_id)
        conn.execute("DELETE FROM list_books WHERE book_id=? AND list_id IN (SELECT id FROM lists WHERE user_id=?)",
                     (e["book_id"], me["id"]))
        conn.execute("DELETE FROM annotations WHERE book_id=? AND user_id=?", (e["book_id"], me["id"]))
        conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/lists")
def get_lists(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT l.id, l.name, (SELECT count(*) FROM list_books WHERE list_id=l.id) AS count "
            "FROM lists l WHERE l.user_id=? ORDER BY l.name", (me["id"],)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/lists")
def create_list(l: ListIn, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        try:
            cur = conn.execute("INSERT INTO lists(user_id,name) VALUES(?,?)", (me["id"], l.name.strip()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "list exists")
        conn.commit()
        return {"id": cur.lastrowid, "name": l.name.strip(), "count": 0}


@app.delete("/api/lists/{list_id}")
def delete_list(list_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        conn.execute("DELETE FROM lists WHERE id=? AND user_id=?", (list_id, me["id"]))
        conn.commit()
    return {"ok": True}


@app.put("/api/lists/{list_id}/books/{book_id}")
def add_to_list(list_id: int, book_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        my_list(conn, me["id"], list_id)
        try:
            conn.execute("INSERT OR IGNORE INTO list_books VALUES(?,?)", (list_id, book_id))
        except sqlite3.IntegrityError:
            raise HTTPException(404, "list or book not found")
        conn.commit()
    return {"ok": True}


@app.delete("/api/lists/{list_id}/books/{book_id}")
def remove_from_list(list_id: int, book_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        my_list(conn, me["id"], list_id)
        conn.execute("DELETE FROM list_books WHERE list_id=? AND book_id=?", (list_id, book_id))
        conn.commit()
    return {"ok": True}


@app.get("/api/stats")
def stats(today: Optional[str] = None, me: dict = Depends(current_user)):
    """Everything on the stats page. `today` is the reader's local date, so streaks follow
    their day rather than the server's. Free for everyone: no premium tier."""
    uid = me["id"]
    try:
        day0 = date.fromisoformat(today) if today else date.fromisoformat(now()[:10])
    except ValueError:
        raise HTTPException(400, "today must be YYYY-MM-DD")
    year = str(day0.year)
    with closing(db()) as conn:
        by_status = {s: 0 for s in STATUSES}
        for r in conn.execute("SELECT status, count(*) c FROM entries WHERE user_id=? GROUP BY status", (uid,)):
            by_status[r["status"]] = r["c"]
        avg = conn.execute("SELECT avg(rating) a FROM entries WHERE user_id=? AND rating > 0", (uid,)).fetchone()["a"]
        pages = conn.execute(
            "SELECT coalesce(sum(b.pages),0) p FROM entries e JOIN books b ON b.id=e.book_id "
            "WHERE e.user_id=? AND e.status='read'", (uid,)
        ).fetchone()["p"]
        by_year = {
            r["y"]: r["c"]
            for r in conn.execute(
                "SELECT substr(finished_at,1,4) y, count(*) c FROM entries "
                "WHERE user_id=? AND status='read' AND finished_at IS NOT NULL GROUP BY y ORDER BY y", (uid,)
            )
        }
        months = [0] * 12
        for r in conn.execute("SELECT substr(finished_at,6,2) m, count(*) c FROM entries WHERE user_id=? AND status='read'"
                              " AND substr(finished_at,1,4)=? GROUP BY m", (uid, year)):
            months[int(r["m"]) - 1] = r["c"]
        ratings = {str(x / 2): 0 for x in range(1, 11)}
        for r in conn.execute("SELECT rating, count(*) c FROM entries WHERE user_id=? AND rating>0 GROUP BY rating", (uid,)):
            ratings[str(r["rating"])] = r["c"]

        def top(col):
            counts: dict[str, int] = {}
            for r in conn.execute(f"SELECT {col} v FROM entries WHERE user_id=? AND {col}<>''", (uid,)):
                for v in (x.strip() for x in r["v"].split(",")):
                    if v:
                        counts[v] = counts.get(v, 0) + 1
            return sorted(counts.items(), key=lambda kv: -kv[1])[:6]

        # time in the reader: a day counts toward the streak after a minute of reading
        days = {r["day"]: r["s"] for r in conn.execute(
            "SELECT day, sum(seconds) s FROM reading_log WHERE user_id=? AND day>=? GROUP BY day",
            (uid, (day0 - timedelta(days=400)).isoformat()))}
        streak, d = 0, day0
        if days.get(d.isoformat(), 0) < 60:
            d -= timedelta(days=1)  # today not read yet: the streak is still alive until midnight
        while days.get(d.isoformat(), 0) >= 60:
            streak += 1
            d -= timedelta(days=1)
        best = run = 0
        for i in range(400, -1, -1):
            run = run + 1 if days.get((day0 - timedelta(days=i)).isoformat(), 0) >= 60 else 0
            best = max(best, run)
        week = [{"day": (day0 - timedelta(days=i)).isoformat(),
                 "minutes": round(days.get((day0 - timedelta(days=i)).isoformat(), 0) / 60)} for i in range(6, -1, -1)]
        year_secs = sum(v for k, v in days.items() if k.startswith(year))
        return {
            "by_status": by_status,
            "avg_rating": round(avg, 2) if avg else None,
            "pages_read": pages,
            "read_by_year": by_year,
            "year": int(year),
            "read_this_year": by_year.get(year, 0),
            "goal": me.get("goal") or 0,
            "months": months,
            "ratings": ratings,
            "genres": top("genres"),
            "moods": top("moods"),
            "streak": streak,
            "best_streak": best,
            "today_minutes": round(days.get(day0.isoformat(), 0) / 60),
            "week": week,
            "hours_this_year": round(year_secs / 3600, 1),
        }


@app.put("/api/log")
def log_reading(body: LogIn, me: dict = Depends(current_user)):
    """Add reading time from the in-app reader (sent in small increments)."""
    with closing(db()) as conn:
        if not conn.execute("SELECT 1 FROM books WHERE id=?", (body.book_id,)).fetchone():
            raise HTTPException(404, "book not found")
        conn.execute(
            "INSERT INTO reading_log VALUES(?,?,?,?,?) ON CONFLICT(user_id,day,book_id)"
            " DO UPDATE SET seconds=seconds+excluded.seconds, pages=pages+excluded.pages",
            (me["id"], body.day, body.book_id, body.seconds, body.pages))
        conn.commit()
    return {"ok": True}


@app.post("/api/library/bulk")
def bulk_edit(body: BulkIn, me: dict = Depends(current_user)):
    """Move, list or remove many books at once. Quietly: tidying shelves isn't news for the feed."""
    if body.status and body.status not in STATUSES:
        raise HTTPException(400, "bad status")
    with closing(db()) as conn:
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM entries WHERE user_id=? AND id IN ({','.join('?' * len(body.entry_ids))})",
            (me["id"], *body.entry_ids))]
        if not ids:
            raise HTTPException(404)
        marks = ",".join("?" * len(ids))
        if body.remove:
            books = [r["book_id"] for r in conn.execute(f"SELECT book_id FROM entries WHERE id IN ({marks})", ids)]
            bm = ",".join("?" * len(books))
            conn.execute(f"DELETE FROM list_books WHERE book_id IN ({bm}) AND list_id IN (SELECT id FROM lists WHERE user_id=?)",
                         (*books, me["id"]))
            conn.execute(f"DELETE FROM annotations WHERE user_id=? AND book_id IN ({bm})", (me["id"], *books))
            conn.execute(f"DELETE FROM entries WHERE id IN ({marks})", ids)
        if body.status:
            t = now()
            conn.execute(
                f"UPDATE entries SET status=?, updated_at=?,"
                f" started_at=CASE WHEN ? IN ('reading','read') THEN coalesce(started_at, ?) ELSE started_at END,"
                f" finished_at=CASE WHEN ?='read' THEN coalesce(finished_at, ?) ELSE finished_at END"
                f" WHERE id IN ({marks}) AND status<>?", (body.status, t, body.status, t, body.status, t, *ids, body.status))
        if body.list_id:
            my_list(conn, me["id"], body.list_id)
            conn.execute(f"INSERT OR IGNORE INTO list_books SELECT ?, book_id FROM entries WHERE id IN ({marks})",
                         (body.list_id, *ids))
        conn.commit()
    return {"updated": len(ids)}


STATUS_MAP = {
    "to-read": "want",
    "currently-reading": "reading",
    "read": "read",
    "did-not-finish": "dnf",
    "dnf": "dnf",
    "want to read": "want",
    "to read": "want",
    "currently reading": "reading",
    "did not finish": "dnf",
}


def parse_date(s: str) -> Optional[str]:
    """Goodreads uses 2021/05/03, StoryGraph 2021/05/03 or 2021-05-03."""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except ValueError:
            pass
    return None


# Goodreads' built-in shelves; any other name in "Bookshelves" is a custom shelf -> a list
GOODREADS_SHELVES = {"to-read", "currently-reading", "read", "did-not-finish", "dnf"}


@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...), me: dict = Depends(current_user)):
    """Import Goodreads / StoryGraph / generic CSV.
    Recognised columns (case-insensitive): Title, Author, My Rating / Star Rating,
    Number of Pages / Pages, Exclusive Shelf / Read Status, My Review / Review, ISBN,
    Date Read, Bookshelves (Goodreads custom shelves) / Tags (StoryGraph).
    Returns every row that couldn't be imported and why, so nothing goes missing quietly."""
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "That file is over 20 MB.")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "empty CSV")
    if not any(f and f.lower().strip() == "title" for f in reader.fieldnames):
        raise HTTPException(400, "This file has no Title column. Export your library as CSV from Goodreads or StoryGraph.")

    def pick(row, *names):
        low = {k.lower().strip(): v for k, v in row.items() if k}
        for n in names:
            if n in low and low[n] not in (None, ""):
                return low[n].strip()
        return ""

    added = updated = 0
    problems = []
    lists_made: dict[str, int] = {}
    with closing(db()) as conn:
        for line, row in enumerate(reader, start=2):  # line 1 is the header
            title = pick(row, "title")
            if not title:
                problems.append({"line": line, "title": "", "reason": "No title"})
                continue
            status = STATUS_MAP.get(pick(row, "exclusive shelf", "read status").lower(), "want")
            pages = pick(row, "number of pages", "pages")
            isbn = re.sub(r"[^0-9Xx]", "", pick(row, "isbn13", "isbn/uid", "isbn"))
            author = pick(row, "author", "authors")
            try:
                existed = conn.execute(
                    "SELECT 1 FROM entries e JOIN books b ON b.id=e.book_id WHERE e.user_id=?"
                    " AND lower(b.title)=lower(?) AND lower(b.author)=lower(?)", (me["id"], title, author)).fetchone()
                # an import of hundreds of books shouldn't flood friends' feeds
                book_id = add_book(
                    conn,
                    me["id"],
                    BookIn(
                        title=title,
                        author=author,
                        pages=int(pages) if pages.isdigit() else None,
                        isbn=isbn or None,
                        # Open Library serves covers by ISBN; default=false 404s when it has none
                        cover_url=f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false" if len(isbn) in (10, 13) else None,
                        status=status,
                    ),
                    feed=False,
                )
            except HTTPException as e:
                problems.append({"line": line, "title": title, "reason": str(e.detail)})
                continue
            if isbn and len(isbn) in (10, 13):
                conn.execute("UPDATE books SET cover_url=coalesce(cover_url, ?) WHERE id=?",
                             (f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false", book_id))
            rating = pick(row, "my rating", "star rating")
            try:
                # app uses half stars; StoryGraph allows quarters
                rating_f = round(min(max(float(rating), 0), 5) * 2) / 2 if rating else 0
            except ValueError:
                rating_f = 0
                problems.append({"line": line, "title": title, "reason": f"Rating “{rating}” isn't a number; imported without it"})
            review = pick(row, "my review", "review")
            finished = parse_date(pick(row, "date read", "last date read"))
            # blank cells keep what's already there, so re-importing doesn't wipe data
            conn.execute(
                "UPDATE entries SET rating=coalesce(?, rating), review=coalesce(nullif(?, ''), review),"
                " finished_at=CASE WHEN status='read' THEN coalesce(?, finished_at) ELSE finished_at END,"
                " progress=CASE WHEN status='read' THEN coalesce((SELECT pages FROM books WHERE id=?), progress)"
                " ELSE progress END, updated_at=? WHERE book_id=? AND user_id=?",
                (rating_f or None, review, finished, book_id, now(), book_id, me["id"]),
            )
            for shelf in (x.strip() for x in pick(row, "bookshelves", "tags").split(",")):
                if not shelf or shelf.lower() in GOODREADS_SHELVES:
                    continue
                name = shelf.replace("-", " ").strip()[:80]
                if name not in lists_made:
                    r = conn.execute("SELECT id FROM lists WHERE user_id=? AND name=?", (me["id"], name)).fetchone()
                    lists_made[name] = r["id"] if r else conn.execute(
                        "INSERT INTO lists(user_id,name) VALUES(?,?)", (me["id"], name)).lastrowid
                conn.execute("INSERT OR IGNORE INTO list_books VALUES(?,?)", (lists_made[name], book_id))
            if existed:
                updated += 1
            else:
                added += 1
        conn.commit()
    return {"imported": added, "updated": updated, "skipped": len([p for p in problems if p["reason"] == "No title"]),
            "problems": problems[:500], "lists": sorted(lists_made)}


@app.get("/api/export/highlights.md")
def export_highlights(me: dict = Depends(current_user)):
    """Every highlight and note, grouped by book, as Markdown: yours to keep, no third-party tools."""
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT b.title, b.author, a.text, a.note, a.kind, a.created_at FROM annotations a JOIN books b ON b.id=a.book_id"
            " WHERE a.user_id=? AND a.kind='highlight' ORDER BY b.title, a.block, a.off", (me["id"],)).fetchall()
    out, current = [f"# Highlights from Shelfie", f"*Exported {now()[:10]}*", ""], None
    for r in rows:
        if r["title"] != current:
            current = r["title"]
            out += ["", f"## {r['title']}", f"*{r['author']}*", ""]
        out += [f"> {r['text']}", ""]
        if r["note"]:
            out += [r["note"], ""]
    return Response("\n".join(out), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=shelfie-highlights.md"})


@app.get("/api/export/csv")
def export_csv(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        rows = conn.execute(ITEM_SQL + " WHERE e.user_id=?", (me["id"],)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Title", "Author", "Status", "Rating", "Progress", "Pages", "Review", "Tags"])
    for r in rows:
        w.writerow([r["title"], r["author"], r["status"], r["rating"] or "", r["progress"],
                    r["pages"] or "", r["review"], r["tags"]])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=shelfie-export.csv"},
    )


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


ANN_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def check_annotation_id(aid: str):
    if not ANN_ID.match(aid):
        raise HTTPException(400, "bad annotation id")


def my_annotation(conn, uid: int, aid: str) -> dict:
    check_annotation_id(aid)
    row = conn.execute("SELECT * FROM annotations WHERE id=? AND user_id=?", (aid, uid)).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.get("/api/books/{book_id}/annotations")
def get_annotations(book_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE book_id=? AND user_id=? ORDER BY block, off", (book_id, me["id"])
        ).fetchall()
        return [dict(r) for r in rows]


@app.put("/api/annotations/{aid}")
def put_annotation(aid: str, a: AnnotationIn, me: dict = Depends(current_user)):
    """Create an annotation. Idempotent, so a client can safely resend it after being offline."""
    check_annotation_id(aid)
    if a.kind not in KINDS or a.color not in COLORS:
        raise HTTPException(400, "bad kind or color")
    with closing(db()) as conn:
        if not conn.execute("SELECT 1 FROM books WHERE id=?", (a.book_id,)).fetchone():
            raise HTTPException(404, "book not found")
        conn.execute(
            "INSERT INTO annotations(id,user_id,book_id,kind,block,off,end_block,end_off,color,note,text,pos,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (aid, me["id"], a.book_id, a.kind, a.block, a.off, a.end_block, a.end_off, a.color, a.note, a.text,
             a.pos, now(), now()),
        )
        conn.commit()
        return my_annotation(conn, me["id"], aid)  # 404 if that id belongs to someone else


@app.patch("/api/annotations/{aid}")
def patch_annotation(aid: str, p: AnnotationPatch, me: dict = Depends(current_user)):
    data = {k: v for k, v in p.model_dump(exclude_unset=True).items() if v is not None}
    if "color" in data and data["color"] not in COLORS:
        raise HTTPException(400, "bad color")
    with closing(db()) as conn:
        a = my_annotation(conn, me["id"], aid)
        if "shared" in data:
            data["shared"] = int(data["shared"])
            if data["shared"] and me.get("is_guest"):
                raise HTTPException(403, "Create an account to share highlights with friends.")
            entry = conn.execute("SELECT id FROM entries WHERE book_id=? AND user_id=?", (a["book_id"], me["id"])).fetchone()
            if data["shared"] and not a["shared"]:
                if not entry:
                    raise HTTPException(400, "Add the book to your shelf to share highlights from it.")
                social.record(conn, me["id"], entry["id"], "highlight", annotation_id=aid)
            if not data["shared"]:
                conn.execute("DELETE FROM events WHERE annotation_id=?", (aid,))
        data["updated_at"] = now()
        conn.execute(
            f"UPDATE annotations SET {', '.join(f'{k}=?' for k in data)} WHERE id=?", [*data.values(), aid]
        )
        conn.commit()
        return my_annotation(conn, me["id"], aid)


@app.delete("/api/annotations/{aid}")
def delete_annotation(aid: str, me: dict = Depends(current_user)):
    check_annotation_id(aid)
    with closing(db()) as conn:
        conn.execute("DELETE FROM annotations WHERE id=? AND user_id=?", (aid, me["id"]))
        conn.commit()
    return {"ok": True}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # served from the root so it may control the whole app; no-cache so updates land promptly
    return FileResponse(
        BASE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"}
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(BASE / "static" / "icon.svg", media_type="image/svg+xml")


APK = DATA / "downloads" / "Shelfie.apk"


@app.get("/api/site", include_in_schema=False)
def site_info():
    """What the public website can offer right now."""
    return {"apk": APK.exists(), "apk_mb": round(APK.stat().st_size / 1e6, 1) if APK.exists() else None}


@app.get("/download/android", include_in_schema=False)
def download_android():
    if not APK.exists():
        raise HTTPException(404, "The Android app isn't available for download yet.")
    return FileResponse(APK, media_type="application/vnd.android.package-archive", filename="Shelfie.apk")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str, request: Request):
    """/ is the public website for visitors; everyone signed in (and the Android app, which
    identifies itself) gets the app. The app's own pages (/app, /discover, /friends, /@name,
    /join/code) always load index.html; /@name also gets link-preview tags."""
    if path.startswith(("api/", "static/")):
        raise HTTPException(404)
    if path == "" and "ShelfieApp" not in request.headers.get("user-agent", "") and not optional_user(request):
        return HTMLResponse((BASE / "static" / "site.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-cache"})
    page = (BASE / "static" / "index.html").read_text(encoding="utf-8")
    if path.startswith("@"):
        meta = social.profile_meta(path[1:].split("/")[0].lower())
        if meta:
            page = page.replace("</head>", meta + "</head>", 1)
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})
