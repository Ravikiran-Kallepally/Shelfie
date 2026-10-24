"""Buddy reads: friends reading one book together.

Members see each other's progress. Comments and friends' highlights are only sent up to where
*you* are in the book (the server decides, so nothing ahead of you ever reaches your device).
"""
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import social
from auth import current_user
from store import db, now

router = APIRouter()
AHEAD = 0.002  # a sliver past your position, so a comment left on the page you're on shows


class ReadIn(BaseModel):
    book_id: int
    usernames: list[str] = Field(default=[], max_length=20)


class CommentIn(BaseModel):
    pos: float = Field(ge=0, le=1)
    chapter: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=4000)


def ensure_entry(conn, uid: int, book_id: int) -> int:
    """Joining a buddy read puts the book on your Reading shelf."""
    row = conn.execute("SELECT id, status FROM entries WHERE user_id=? AND book_id=?", (uid, book_id)).fetchone()
    if row:
        if row["status"] == "want":
            conn.execute("UPDATE entries SET status='reading', started_at=coalesce(started_at, ?), updated_at=? WHERE id=?",
                         (now(), now(), row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO entries(user_id,book_id,status,started_at,updated_at) VALUES(?,?,'reading',?,?)",
        (uid, book_id, now(), now())).lastrowid


def my_position(conn, uid: int, book_id: int) -> float:
    row = conn.execute("SELECT position, status FROM entries WHERE user_id=? AND book_id=?", (uid, book_id)).fetchone()
    if not row:
        return 0.0
    return 1.0 if row["status"] == "read" else row["position"]


def membership(conn, rid: int, uid: int, joined: bool = True) -> dict:
    row = conn.execute("SELECT r.*, m.status FROM reads r JOIN read_members m ON m.read_id=r.id"
                       " WHERE r.id=? AND m.user_id=?", (rid, uid)).fetchone()
    if not row or row["status"] == "left" or (joined and row["status"] != "joined"):
        raise HTTPException(404, "Buddy read not found")
    return dict(row)


def read_json(conn, rid: int, uid: int) -> dict:
    r = conn.execute("SELECT r.id, r.created_at, b.id AS book_id, b.title, b.author, b.cover_url, b.gutenberg_id,"
                     " m.status AS my_status FROM reads r JOIN books b ON b.id=r.book_id"
                     " JOIN read_members m ON m.read_id=r.id AND m.user_id=? WHERE r.id=?", (uid, rid)).fetchone()
    members = []
    for m in conn.execute(
            "SELECT u.id, u.username, u.display_name, m.status, e.position, e.status AS shelf"
            " FROM read_members m JOIN users u ON u.id=m.user_id"
            " LEFT JOIN entries e ON e.user_id=u.id AND e.book_id=?"
            " WHERE m.read_id=? AND m.status<>'left' ORDER BY m.joined_at", (r["book_id"], rid)):
        pos = 1.0 if m["shelf"] == "read" else (m["position"] or 0)
        members.append({"username": m["username"], "display_name": m["display_name"], "status": m["status"],
                        "pct": round(pos * 100), "pos": pos, "me": m["id"] == uid})
    return dict(r) | {"members": members}


@router.get("/api/me/friends")
def my_friends(me: dict = Depends(current_user)):
    """People you follow who follow you back: who you can invite to a buddy read."""
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT u.username, u.display_name FROM follows a JOIN follows b"
            " ON b.follower_id=a.followee_id AND b.followee_id=a.follower_id AND b.status='accepted'"
            " JOIN users u ON u.id=a.followee_id WHERE a.follower_id=? AND a.status='accepted'"
            " ORDER BY u.display_name", (me["id"],)).fetchall()
        return [dict(r) for r in rows]


@router.post("/api/reads")
def create_read(body: ReadIn, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        if not conn.execute("SELECT 1 FROM books WHERE id=?", (body.book_id,)).fetchone():
            raise HTTPException(404, "book not found")
        invitees = []
        for name in dict.fromkeys(n.strip().lstrip("@").lower() for n in body.usernames if n.strip()):
            other = social.user_by_name(conn, name)
            mutual = (social.follow_status(conn, me["id"], other["id"]) == "accepted"
                      and social.follow_status(conn, other["id"], me["id"]) == "accepted")
            if other["id"] == me["id"] or not mutual:
                raise HTTPException(403, f"You can invite friends only (people who follow you back). @{name} isn't one yet.")
            invitees.append(other)
        # inviting more people to a book you're already reading together extends that read
        existing = conn.execute("SELECT r.id FROM reads r JOIN read_members m ON m.read_id=r.id"
                                " WHERE r.book_id=? AND m.user_id=? AND m.status='joined'",
                                (body.book_id, me["id"])).fetchone()
        rid = existing["id"] if existing else conn.execute(
            "INSERT INTO reads(book_id,created_by,created_at) VALUES(?,?,?)", (body.book_id, me["id"], now())).lastrowid
        conn.execute("INSERT OR IGNORE INTO read_members VALUES(?,?, 'joined', ?)", (rid, me["id"], now()))
        for o in invitees:
            conn.execute("INSERT INTO read_members VALUES(?,?, 'invited', ?) ON CONFLICT(read_id,user_id)"
                         " DO UPDATE SET status=CASE WHEN status='left' THEN 'invited' ELSE status END", (rid, o["id"], now()))
        entry_id = ensure_entry(conn, me["id"], body.book_id)
        if invitees:
            social.record(conn, me["id"], entry_id, "buddy", {"with": [o["display_name"] for o in invitees]})
        conn.commit()
        return read_json(conn, rid, me["id"])


@router.get("/api/reads")
def my_reads(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        ids = [r["read_id"] for r in conn.execute(
            "SELECT m.read_id FROM read_members m JOIN reads r ON r.id=m.read_id WHERE m.user_id=?"
            " AND m.status IN ('invited','joined') ORDER BY m.status='joined', r.created_at DESC", (me["id"],))]
        return [read_json(conn, rid, me["id"]) for rid in ids]


@router.get("/api/reads/book/{book_id}")
def read_for_book(book_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        row = conn.execute("SELECT r.id FROM reads r JOIN read_members m ON m.read_id=r.id"
                           " WHERE r.book_id=? AND m.user_id=? AND m.status='joined' ORDER BY r.id DESC LIMIT 1",
                           (book_id, me["id"])).fetchone()
        return read_json(conn, row["id"], me["id"]) if row else None


@router.post("/api/reads/{rid}/join")
def join_read(rid: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        r = membership(conn, rid, me["id"], joined=False)
        conn.execute("UPDATE read_members SET status='joined' WHERE read_id=? AND user_id=?", (rid, me["id"]))
        ensure_entry(conn, me["id"], r["book_id"])
        conn.commit()
        return read_json(conn, rid, me["id"])


@router.post("/api/reads/{rid}/leave")
def leave_read(rid: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        membership(conn, rid, me["id"], joined=False)
        conn.execute("UPDATE read_members SET status='left' WHERE read_id=? AND user_id=?", (rid, me["id"]))
        conn.commit()
    return {"ok": True}


@router.get("/api/reads/{rid}/discussion")
def discussion(rid: int, me: dict = Depends(current_user)):
    """Comments and friends' shared highlights, only up to where you are in the book."""
    with closing(db()) as conn:
        r = membership(conn, rid, me["id"])
        upto = my_position(conn, me["id"], r["book_id"]) + AHEAD
        comments = [dict(c) | {"mine": c["user_id"] == me["id"]} for c in conn.execute(
            "SELECT c.id, c.user_id, c.pos, c.chapter, c.text, c.created_at, u.username, u.display_name"
            " FROM read_comments c JOIN users u ON u.id=c.user_id WHERE c.read_id=? AND c.pos<=? ORDER BY c.pos, c.id",
            (rid, upto))]
        for c in comments:
            del c["user_id"]
        ahead = conn.execute("SELECT count(*) FROM read_comments WHERE read_id=? AND pos>?", (rid, upto)).fetchone()[0]
        margins = [dict(a) for a in conn.execute(
            "SELECT a.id, a.block, a.off, a.end_block, a.end_off, a.color, a.text, a.note, u.username, u.display_name"
            " FROM annotations a JOIN users u ON u.id=a.user_id"
            " JOIN read_members m ON m.user_id=a.user_id AND m.read_id=? AND m.status='joined'"
            " WHERE a.book_id=? AND a.user_id<>? AND a.kind='highlight' AND a.shared=1 AND coalesce(a.pos,0)<=?",
            (rid, r["book_id"], me["id"], upto))]
        return {"comments": comments, "ahead": ahead, "margins": margins, "read": read_json(conn, rid, me["id"])}


@router.post("/api/reads/{rid}/comments")
def add_comment(rid: int, body: CommentIn, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        membership(conn, rid, me["id"])
        cid = conn.execute("INSERT INTO read_comments(read_id,user_id,pos,chapter,text,created_at) VALUES(?,?,?,?,?,?)",
                           (rid, me["id"], body.pos, body.chapter.strip(), body.text.strip(), now())).lastrowid
        conn.commit()
        return {"id": cid, "pos": body.pos, "chapter": body.chapter.strip(), "text": body.text.strip(),
                "created_at": now(), "username": me["username"], "display_name": me["display_name"], "mine": True}


@router.delete("/api/reads/{rid}/comments/{cid}")
def delete_comment(rid: int, cid: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        conn.execute("DELETE FROM read_comments WHERE id=? AND read_id=? AND user_id=?", (cid, rid, me["id"]))
        conn.commit()
    return {"ok": True}
