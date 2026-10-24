"""Profiles, follows, the friends feed.

Who sees what, in one place (visible_to):
  - you always see your own books
  - "friends" books: people you follow who follow you back
  - "public" books: anyone, unless the account is private, then accepted followers only
  - "private" books: only you
"""
import html
import json
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import PUBLIC_USER, current_user, optional_user
from store import ITEM_SQL, db, now, row_to_item

router = APIRouter()
MERGE = timedelta(hours=6)
# things people chose to share. Starting a book or saving it for later still feeds
# "Reading now" and profiles, but would turn the feed into a log of app activity.
FEED_KINDS = ("finished", "rated", "reviewed", "highlight", "buddy", "thought")  # repeated updates to one book within this window edit one feed item


def follow_status(conn, a: int, b: int) -> Optional[str]:
    r = conn.execute("SELECT status FROM follows WHERE follower_id=? AND followee_id=?", (a, b)).fetchone()
    return r["status"] if r else None


def visible_to(conn, viewer: Optional[dict], owner: dict) -> tuple:
    """Entry visibilities the viewer may see on the owner's shelves."""
    if viewer and viewer["id"] == owner["id"]:
        return ("public", "friends", "private")
    following = viewer is not None and follow_status(conn, viewer["id"], owner["id"]) == "accepted"
    if following and follow_status(conn, owner["id"], viewer["id"]) == "accepted":
        return ("public", "friends")
    if following or not owner["is_private"]:
        return ("public",)
    return ()


def user_by_name(conn, username: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE username=?", (username.lstrip("@"),)).fetchone()
    if not row:
        raise HTTPException(404, "No one here by that name.")
    return dict(row)


def public_user(u: dict) -> dict:
    return {k: u[k] for k in PUBLIC_USER if k != "id"} | {"is_private": bool(u["is_private"])}


def _in(vis: tuple) -> str:
    return ",".join("?" * len(vis))


# ---------- feed events (called from the library endpoints) ----------
def record(conn, user_id: int, entry_id: int, kind: str, data: Optional[dict] = None,
           annotation_id: Optional[str] = None, merge_with: tuple = ()):
    """Add a feed item. If this book had one of the `merge_with` kinds very recently, update
    that instead, so rating a book right after finishing it doesn't post twice."""
    data = data or {}
    if merge_with:
        since = (datetime.fromisoformat(now()) - MERGE).isoformat(timespec="seconds")
        recent = conn.execute(
            f"SELECT id, data FROM events WHERE entry_id=? AND kind IN ({_in(merge_with)}) AND created_at>=?"
            " ORDER BY id DESC LIMIT 1", (entry_id, *merge_with, since)).fetchone()
        if recent:
            merged = {**json.loads(recent["data"]), **data}
            conn.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(merged), recent["id"]))
            return
    conn.execute("INSERT INTO events(user_id,entry_id,annotation_id,kind,data,created_at) VALUES(?,?,?,?,?,?)",
                 (user_id, entry_id, annotation_id, kind, json.dumps(data), now()))


def record_entry_change(conn, user_id: int, entry_id: int, before: Optional[dict], after: dict):
    """Turn a shelf change into feed items: started / finished / want, ratings and reviews."""
    old_status = before["status"] if before else None
    if after["status"] != old_status:
        kind = {"reading": "started", "read": "finished", "want": "want"}.get(after["status"])
        if kind:
            data = {"rating": after["rating"]} if kind == "finished" and after["rating"] else {}
            record(conn, user_id, entry_id, kind, data)
    if before and after["rating"] and after["rating"] != before["rating"]:
        record(conn, user_id, entry_id, "rated", {"rating": after["rating"]}, merge_with=("finished", "rated"))
    if before and after["review"].strip() and after["review"] != before["review"]:
        # a review written right after finishing or rating joins that post instead of adding one
        record(conn, user_id, entry_id, "reviewed", {"reviewed": True}, merge_with=("finished", "rated", "reviewed"))


# ---------- people ----------
def profile_items(conn, owner: dict, vis: tuple, where: str = "", args: tuple = (), viewer: Optional[dict] = None,
                  limit: int = 200, offset: int = 0) -> list:
    if not vis:
        return []
    sql = ITEM_SQL.replace("SELECT e.id", "SELECT (SELECT e2.id FROM entries e2 WHERE e2.book_id = b.id AND e2.user_id = ?) AS my_entry, e.id", 1)
    rows = conn.execute(
        sql + f" WHERE e.user_id=? AND e.visibility IN ({_in(vis)}) {where} ORDER BY e.updated_at DESC LIMIT ? OFFSET ?",
        (viewer["id"] if viewer else -1, owner["id"], *vis, *args, limit, offset)).fetchall()
    out = []
    for r in rows:
        d = row_to_item(r)
        # a friend's reading position, notes and tags stay theirs
        for k in ("position", "progress", "tags", "warnings", "list_ids", "notes_count", "user_id"):
            d.pop(k, None)
        d["pct"] = round(r["position"] * 100) if r["gutenberg_id"] else (
            round(r["progress"] / r["pages"] * 100) if r["pages"] else 0)
        out.append(d)
    return out


@router.get("/api/users")
def search_people(q: str = "", me: dict = Depends(current_user)):
    with closing(db()) as conn:
        if q.strip():
            rows = conn.execute(
                "SELECT * FROM users WHERE (username LIKE ? OR display_name LIKE ?) AND id<>? ORDER BY username LIMIT 20",
                (f"%{q.strip().lstrip('@')}%", f"%{q.strip()}%", me["id"])).fetchall()
        else:  # people your friends follow, then everyone else
            rows = conn.execute(
                "SELECT u.*, (SELECT count(*) FROM follows f JOIN follows mine ON mine.followee_id = f.follower_id"
                " AND mine.follower_id=? AND mine.status='accepted' WHERE f.followee_id=u.id AND f.status='accepted') AS mutual"
                " FROM users u WHERE u.id<>? ORDER BY mutual DESC, u.created_at DESC LIMIT 20",
                (me["id"], me["id"])).fetchall()
        return [public_user(dict(r)) | {"following": follow_status(conn, me["id"], r["id"])} for r in rows]


@router.get("/api/users/{username}")
def profile(username: str, viewer: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        vis = visible_to(conn, viewer, owner)
        me = viewer is not None and viewer["id"] == owner["id"]
        counts = {
            "followers": conn.execute("SELECT count(*) FROM follows WHERE followee_id=? AND status='accepted'", (owner["id"],)).fetchone()[0],
            "following": conn.execute("SELECT count(*) FROM follows WHERE follower_id=? AND status='accepted'", (owner["id"],)).fetchone()[0],
        }
        stats = {"read": 0, "reading": 0, "want": 0, "dnf": 0, "read_this_year": 0, "pages": 0}
        if vis:
            for r in conn.execute(f"SELECT status, count(*) c FROM entries WHERE user_id=? AND visibility IN ({_in(vis)}) GROUP BY status",
                                  (owner["id"], *vis)):
                stats[r["status"]] = r["c"]
            year = now()[:4]
            r = conn.execute(
                f"SELECT count(*) c, coalesce(sum(b.pages),0) p FROM entries e JOIN books b ON b.id=e.book_id"
                f" WHERE e.user_id=? AND e.status='read' AND e.visibility IN ({_in(vis)}) AND substr(e.finished_at,1,4)=?",
                (owner["id"], *vis, year)).fetchone()
            stats["read_this_year"], stats["pages"] = r["c"], r["p"]
        in_common = []
        if viewer and not me and vis:
            in_common = [dict(r) for r in conn.execute(
                f"SELECT b.id AS book_id, b.title, b.cover_url FROM entries e JOIN entries mine ON mine.book_id=e.book_id"
                f" AND mine.user_id=? AND mine.status='read' JOIN books b ON b.id=e.book_id"
                f" WHERE e.user_id=? AND e.status='read' AND e.visibility IN ({_in(vis)}) LIMIT 50",
                (viewer["id"], owner["id"], *vis))]
        return {
            "user": public_user(owner),
            "me": me,
            "signed_in": viewer is not None,
            "following": follow_status(conn, viewer["id"], owner["id"]) if viewer and not me else None,
            "follows_you": bool(viewer and not me and follow_status(conn, owner["id"], viewer["id"]) == "accepted"),
            "can_see": bool(vis),
            "counts": counts,
            "stats": stats,
            "reading": profile_items(conn, owner, vis, "AND e.status='reading'", viewer=viewer, limit=12),
            "in_common": in_common,
        }


@router.get("/api/users/{username}/books")
def profile_books(username: str, status: str = "read", offset: int = 0,
                  viewer: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        vis = visible_to(conn, viewer, owner)
        if status == "favourites":
            return profile_items(conn, owner, vis, "AND e.rating >= 4.5", viewer=viewer, limit=60, offset=offset)
        return profile_items(conn, owner, vis, "AND e.status=?", (status,), viewer=viewer, limit=60, offset=offset)


@router.get("/api/users/{username}/highlights")
def profile_highlights(username: str, viewer: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        vis = visible_to(conn, viewer, owner)
        if not vis:
            return []
        rows = conn.execute(
            f"SELECT a.id, a.text, a.note, a.color, a.created_at, b.id AS book_id, b.title, b.author, b.cover_url"
            f" FROM annotations a JOIN entries e ON e.book_id=a.book_id AND e.user_id=a.user_id JOIN books b ON b.id=a.book_id"
            f" WHERE a.user_id=? AND a.shared=1 AND a.kind='highlight' AND e.visibility IN ({_in(vis)})"
            f" ORDER BY a.created_at DESC LIMIT 100", (owner["id"], *vis)).fetchall()
        return [dict(r) for r in rows]


def _people(conn, sql: str, uid: int, viewer: Optional[dict]):
    rows = conn.execute(sql, (uid,)).fetchall()
    return [public_user(dict(r)) | {"following": follow_status(conn, viewer["id"], r["id"]) if viewer else None,
                                    "me": bool(viewer and viewer["id"] == r["id"])} for r in rows]


@router.get("/api/users/{username}/followers")
def followers(username: str, viewer: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        if not visible_to(conn, viewer, owner):
            raise HTTPException(403, "This account is private.")
        return _people(conn, "SELECT u.* FROM follows f JOIN users u ON u.id=f.follower_id"
                             " WHERE f.followee_id=? AND f.status='accepted' ORDER BY f.created_at DESC", owner["id"], viewer)


@router.get("/api/users/{username}/following")
def following(username: str, viewer: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        if not visible_to(conn, viewer, owner):
            raise HTTPException(403, "This account is private.")
        return _people(conn, "SELECT u.* FROM follows f JOIN users u ON u.id=f.followee_id"
                             " WHERE f.follower_id=? AND f.status='accepted' ORDER BY f.created_at DESC", owner["id"], viewer)


@router.post("/api/users/{username}/follow")
def follow(username: str, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        if owner["id"] == me["id"]:
            raise HTTPException(400, "You can't follow yourself.")
        status = "pending" if owner["is_private"] else "accepted"
        # an existing follow (or request) stays as it is
        conn.execute("INSERT OR IGNORE INTO follows VALUES(?,?,?,?)", (me["id"], owner["id"], status, now()))
        conn.commit()
        return {"following": follow_status(conn, me["id"], owner["id"])}


@router.delete("/api/users/{username}/follow")
def unfollow(username: str, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        owner = user_by_name(conn, username)
        conn.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (me["id"], owner["id"]))
        conn.commit()
    return {"following": None}


@router.get("/api/me/requests")
def follow_requests(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        rows = conn.execute("SELECT u.* FROM follows f JOIN users u ON u.id=f.follower_id"
                            " WHERE f.followee_id=? AND f.status='pending' ORDER BY f.created_at", (me["id"],)).fetchall()
        return [public_user(dict(r)) for r in rows]


@router.post("/api/me/requests/{username}")
def accept_request(username: str, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        other = user_by_name(conn, username)
        conn.execute("UPDATE follows SET status='accepted' WHERE follower_id=? AND followee_id=?", (other["id"], me["id"]))
        conn.commit()
    return {"ok": True}


@router.delete("/api/me/requests/{username}")
def decline_request(username: str, me: dict = Depends(current_user)):
    """Also removes an existing follower."""
    with closing(db()) as conn:
        other = user_by_name(conn, username)
        conn.execute("DELETE FROM follows WHERE follower_id=? AND followee_id=?", (other["id"], me["id"]))
        conn.commit()
    return {"ok": True}


# ---------- feed ----------
@router.get("/api/feed")
def feed(before: int = 0, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT ev.id, ev.kind, ev.data, ev.created_at, ev.user_id, ev.annotation_id,"
            " u.username, u.display_name, u.is_private, e.visibility, e.rating, e.review, e.id AS entry_id,"
            " b.id AS book_id, b.title, b.author, b.cover_url, b.gutenberg_id,"
            " a.text AS quote, a.note AS quote_note, a.color AS quote_color, a.shared AS quote_shared,"
            " (SELECT count(*) FROM likes l WHERE l.event_id=ev.id) AS likes,"
            " EXISTS(SELECT 1 FROM likes l WHERE l.event_id=ev.id AND l.user_id=:me) AS liked,"
            " (SELECT e2.id FROM entries e2 WHERE e2.book_id=b.id AND e2.user_id=:me) AS my_entry,"
            " (SELECT count(*) FROM event_comments c WHERE c.event_id=ev.id) AS comments"
            " FROM events ev JOIN users u ON u.id=ev.user_id JOIN entries e ON e.id=ev.entry_id"
            " JOIN books b ON b.id=e.book_id LEFT JOIN annotations a ON a.id=ev.annotation_id"
            " WHERE (ev.user_id=:me OR ev.user_id IN (SELECT followee_id FROM follows WHERE follower_id=:me AND status='accepted'))"
            f" AND ev.kind IN ({','.join(repr(k) for k in FEED_KINDS)})"
            " AND (:before=0 OR ev.id < :before) ORDER BY ev.id DESC LIMIT 120",
            {"me": me["id"], "before": before}).fetchall()
        vis_cache: dict[int, tuple] = {}
        out = []
        for r in rows:
            if r["user_id"] not in vis_cache:
                vis_cache[r["user_id"]] = visible_to(conn, me, {"id": r["user_id"], "is_private": r["is_private"]})
            if r["visibility"] not in vis_cache[r["user_id"]]:
                continue
            if r["kind"] == "highlight" and not r["quote_shared"]:
                continue
            d = dict(r)
            d["data"] = json.loads(d["data"])
            d["liked"] = bool(d["liked"])
            d["mine"] = r["user_id"] == me["id"]
            if d["kind"] != "reviewed" and not d["data"].get("reviewed"):
                d.pop("review")
            for k in ("is_private", "visibility", "quote_shared", "user_id"):
                d.pop(k)
            out.append(d)
            if len(out) == 30:
                break
        for d in out:
            d["recent_comments"] = comment_rows(conn, d["id"], me, limit=2) if d["comments"] else []
        # where the next page starts; 0 = nothing older
        cursor = out[-1]["id"] if len(out) == 30 else rows[-1]["id"] if len(rows) == 120 else 0
        return {"events": out, "before": cursor}


@router.get("/api/friends/reading")
def friends_reading(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        friends = conn.execute("SELECT u.* FROM follows f JOIN users u ON u.id=f.followee_id"
                               " WHERE f.follower_id=? AND f.status='accepted'", (me["id"],)).fetchall()
        out = []
        for f in friends:
            owner = dict(f)
            for item in profile_items(conn, owner, visible_to(conn, me, owner), "AND e.status='reading'", viewer=me, limit=3):
                out.append(item | {"username": owner["username"], "display_name": owner["display_name"]})
        out.sort(key=lambda x: x["updated_at"], reverse=True)
        return out[:20]


def _visible_event(conn, me: dict, event_id: int) -> dict:
    r = conn.execute("SELECT ev.user_id, u.is_private, e.visibility FROM events ev JOIN users u ON u.id=ev.user_id"
                     " JOIN entries e ON e.id=ev.entry_id WHERE ev.id=?", (event_id,)).fetchone()
    if not r or r["visibility"] not in visible_to(conn, me, {"id": r["user_id"], "is_private": r["is_private"]}):
        raise HTTPException(404)
    return dict(r)


@router.put("/api/events/{event_id}/like")
def like(event_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        _visible_event(conn, me, event_id)
        conn.execute("INSERT OR IGNORE INTO likes(user_id,event_id,created_at) VALUES(?,?,?)", (me["id"], event_id, now()))
        conn.commit()
        return {"likes": conn.execute("SELECT count(*) FROM likes WHERE event_id=?", (event_id,)).fetchone()[0]}


@router.delete("/api/events/{event_id}/like")
def unlike(event_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        conn.execute("DELETE FROM likes WHERE user_id=? AND event_id=?", (me["id"], event_id))
        conn.commit()
        return {"likes": conn.execute("SELECT count(*) FROM likes WHERE event_id=?", (event_id,)).fetchone()[0]}


# ---------- link previews ----------
def profile_meta(username: str) -> str:
    """Open Graph tags so a shared /@name link shows a card in chat apps. Public accounts only."""
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row or row["is_private"]:
            return ""
        owner = dict(row)
        cur = profile_items(conn, owner, ("public",), "AND e.status='reading'", limit=1)
        last = cur or profile_items(conn, owner, ("public",), "AND e.status='read'", limit=1)
        n = conn.execute("SELECT count(*) FROM entries WHERE user_id=? AND status='read' AND visibility='public'",
                         (owner["id"],)).fetchone()[0]
    title = f"{owner['display_name']} (@{owner['username']}) on Shelfie"
    desc = (f"Reading {cur[0]['title']} · " if cur else "") + f"{n} book{'s' if n != 1 else ''} read"
    tags = [("og:title", title), ("og:description", desc), ("og:type", "profile")]
    if last and last[0]["cover_url"]:
        tags.append(("og:image", last[0]["cover_url"]))
    meta = "".join(f'<meta property="{k}" content="{html.escape(v)}">' for k, v in tags)
    return meta + f'<meta name="twitter:card" content="summary"><meta name="description" content="{html.escape(desc)}">'


# ---------- posting ----------
class PostIn(BaseModel):
    entry_id: int
    text: str = Field(default="", max_length=2000)
    quote: str = Field(default="", max_length=1000)


class FinishIn(BaseModel):
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    review: str = Field(default="", max_length=20000)
    quote: str = Field(default="", max_length=1000)


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _my_entry(conn, me: dict, entry_id: int) -> dict:
    row = conn.execute("SELECT * FROM entries WHERE id=? AND user_id=?", (entry_id, me["id"])).fetchone()
    if not row:
        raise HTTPException(404, "That book isn't on your shelves.")
    return dict(row)


@router.post("/api/posts")
def post_thought(body: PostIn, me: dict = Depends(current_user)):
    """A thought about a book you're reading, optionally with a line from it."""
    text, quote = body.text.strip(), body.quote.strip()
    if not text and not quote:
        raise HTTPException(400, "Write something to post.")
    with closing(db()) as conn:
        _my_entry(conn, me, body.entry_id)
        record(conn, me["id"], body.entry_id, "thought", {"text": text, "quote": quote})
        conn.commit()
    return {"ok": True}


@router.post("/api/library/{entry_id}/finish")
def finish(entry_id: int, body: FinishIn, me: dict = Depends(current_user)):
    """The "How was it?" step after finishing: rating, a few words and a favourite line join
    the "finished" post instead of becoming separate ones."""
    if body.rating is not None and (body.rating * 2) % 1:
        raise HTTPException(400, "rating must be in half-star steps")
    with closing(db()) as conn:
        e = _my_entry(conn, me, entry_id)
        t = now()
        conn.execute("UPDATE entries SET status='read', finished_at=coalesce(finished_at, ?), started_at=coalesce(started_at, ?),"
                     " position=CASE WHEN position>0 THEN 1 ELSE position END,"
                     " rating=coalesce(?, rating), review=CASE WHEN ?<>'' THEN ? ELSE review END, updated_at=? WHERE id=?",
                     (t, t, body.rating or None, body.review.strip(), body.review.strip(), t, entry_id))
        data = {"rating": body.rating or e["rating"]}
        if body.review.strip():
            data["reviewed"] = True
        if body.quote.strip():
            data["quote"] = body.quote.strip()
        record(conn, me["id"], entry_id, "finished", data, merge_with=("finished", "rated", "reviewed"))
        conn.commit()
    return {"ok": True}


@router.delete("/api/events/{event_id}")
def delete_post(event_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        conn.execute("DELETE FROM events WHERE id=? AND user_id=?", (event_id, me["id"]))
        conn.commit()
    return {"ok": True}


# ---------- comments ----------
def comment_rows(conn, event_id: int, me: dict, limit: int = 200) -> list:
    rows = conn.execute(
        "SELECT * FROM (SELECT c.id, c.text, c.created_at, c.user_id, u.username, u.display_name FROM event_comments c"
        " JOIN users u ON u.id=c.user_id WHERE c.event_id=? ORDER BY c.id DESC LIMIT ?) ORDER BY id",
        (event_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["mine"] = d.pop("user_id") == me["id"]
        out.append(d)
    return out


@router.get("/api/events/{event_id}/comments")
def list_comments(event_id: int, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        _visible_event(conn, me, event_id)
        return comment_rows(conn, event_id, me)


@router.post("/api/events/{event_id}/comments")
def add_comment(event_id: int, body: CommentIn, me: dict = Depends(current_user)):
    with closing(db()) as conn:
        _visible_event(conn, me, event_id)
        cid = conn.execute("INSERT INTO event_comments(event_id,user_id,text,created_at) VALUES(?,?,?,?)",
                           (event_id, me["id"], body.text.strip(), now())).lastrowid
        conn.commit()
        return {"id": cid, "text": body.text.strip(), "created_at": now(), "username": me["username"],
                "display_name": me["display_name"], "mine": True}


@router.delete("/api/events/{event_id}/comments/{comment_id}")
def delete_comment(event_id: int, comment_id: int, me: dict = Depends(current_user)):
    """Your own comment, or any comment on your own post."""
    with closing(db()) as conn:
        conn.execute("DELETE FROM event_comments WHERE id=? AND event_id=? AND (user_id=?"
                     " OR event_id IN (SELECT id FROM events WHERE user_id=?))", (comment_id, event_id, me["id"], me["id"]))
        conn.commit()
    return {"ok": True}


# ---------- activity: what used to clutter the feed, now in its own place ----------
def _activity(conn, me: dict) -> list:
    rows = conn.execute("""
        SELECT 'like' AS kind, l.created_at AS created_at, u.username, u.display_name, ev.id AS event_id, b.title, NULL AS text
          FROM likes l JOIN events ev ON ev.id=l.event_id JOIN users u ON u.id=l.user_id
          JOIN entries e ON e.id=ev.entry_id JOIN books b ON b.id=e.book_id
         WHERE ev.user_id=:me AND l.user_id<>:me AND l.created_at IS NOT NULL
        UNION ALL
        SELECT 'comment', c.created_at, u.username, u.display_name, ev.id, b.title, c.text
          FROM event_comments c JOIN events ev ON ev.id=c.event_id JOIN users u ON u.id=c.user_id
          JOIN entries e ON e.id=ev.entry_id JOIN books b ON b.id=e.book_id
         WHERE ev.user_id=:me AND c.user_id<>:me
        UNION ALL
        SELECT CASE f.status WHEN 'pending' THEN 'request' ELSE 'follow' END, f.created_at, u.username, u.display_name, NULL, NULL, NULL
          FROM follows f JOIN users u ON u.id=f.follower_id WHERE f.followee_id=:me
        UNION ALL
        SELECT 'buddy', m.joined_at, cu.username, cu.display_name, r.id, b.title, NULL
          FROM read_members m JOIN reads r ON r.id=m.read_id JOIN users cu ON cu.id=r.created_by JOIN books b ON b.id=r.book_id
         WHERE m.user_id=:me AND r.created_by<>:me AND m.status='invited'
        UNION ALL
        SELECT 'joined', i.used_at, u.username, u.display_name, NULL, NULL, NULL
          FROM invites i JOIN users u ON u.id=i.used_by WHERE i.created_by=:me
        ORDER BY created_at DESC LIMIT 80""", {"me": me["id"]}).fetchall()
    joined = {r["username"] for r in rows if r["kind"] == "joined"}
    # someone who joined through your invite follows you automatically; "joined" says it better
    return [dict(r) for r in rows if not (r["kind"] == "follow" and r["username"] in joined)]


@router.get("/api/activity")
def activity(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        items = _activity(conn, me)
        seen = me.get("activity_seen") or ""
        for it in items:
            it["unread"] = (it["created_at"] or "") > seen
        conn.execute("UPDATE users SET activity_seen=? WHERE id=?", (now(), me["id"]))
        conn.commit()
    return items


@router.get("/api/activity/unread")
def activity_unread(me: dict = Depends(current_user)):
    with closing(db()) as conn:
        seen = me.get("activity_seen") or ""
        return {"unread": sum(1 for it in _activity(conn, me) if (it["created_at"] or "") > seen)}


@router.get("/api/people/suggested")
def suggested(me: dict = Depends(current_user)):
    """Friends of friends you don't follow yet, for a quiet feed."""
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT u.*, count(*) AS mutual FROM follows mine JOIN follows theirs ON theirs.follower_id=mine.followee_id"
            " AND theirs.status='accepted' JOIN users u ON u.id=theirs.followee_id"
            " WHERE mine.follower_id=? AND mine.status='accepted' AND u.id<>?"
            " AND u.id NOT IN (SELECT followee_id FROM follows WHERE follower_id=?)"
            " GROUP BY u.id ORDER BY mutual DESC LIMIT 8", (me["id"], me["id"], me["id"])).fetchall()
        return [public_user(dict(r)) | {"mutual": r["mutual"]} for r in rows]
