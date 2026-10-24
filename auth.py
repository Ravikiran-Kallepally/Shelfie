"""Accounts: invite-only sign-up, password login, cookie sessions."""
import hashlib
import hmac
import re
import secrets
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from store import db, now

router = APIRouter()
COOKIE = "shelfie_session"
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,20}$")
# usernames that would clash with app paths like /@discover
RESERVED = {"admin", "api", "static", "join", "reset", "discover", "friends", "me", "settings", "login",
            "signup", "about", "help", "shelfie", "support", "root"}
PUBLIC_USER = ("id", "username", "display_name", "bio", "is_private", "created_at")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        want = bytes.fromhex(h)
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    # only a hash is stored, so a leaked database can't be used to log in
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(request: Request, response: Response, user_id: int):
    token = secrets.token_urlsafe(32)
    with closing(db()) as conn:
        conn.execute("INSERT INTO sessions(token_hash,user_id,created_at) VALUES(?,?,?)",
                     (_token_hash(token), user_id, now()))
        conn.commit()
    response.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")


def optional_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_token_hash(token),),
        ).fetchone()
    return dict(row) if row else None


def current_user(user: Optional[dict] = Depends(optional_user)) -> dict:
    if not user:
        raise HTTPException(401, "Sign in to continue")
    return user


def member(user: dict = Depends(current_user)) -> dict:
    """Social features (friends, feed, buddy reads, invites) need a real account, not a guest."""
    if user.get("is_guest"):
        raise HTTPException(403, "Create an account to read with friends.")
    return user


def me_json(u: dict) -> dict:
    return {**{k: u[k] for k in PUBLIC_USER}, "is_admin": bool(u["is_admin"]), "goal": u.get("goal") or 0,
            "is_guest": bool(u.get("is_guest"))}


# a few tries a minute per address+name stops password guessing without bothering people
_attempts: dict[str, list[float]] = {}


def throttle(key: str, limit: int = 8, window: int = 300):
    t = time.time()
    recent = [x for x in _attempts.get(key, []) if t - x < window]
    if len(recent) >= limit:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    recent.append(t)
    _attempts[key] = recent


def check_username(name: str) -> str:
    username = name.strip().lower().lstrip("@")
    if not USERNAME_RE.match(username) or username in RESERVED or username.startswith("guest_"):
        raise HTTPException(400, "Usernames are 3–20 letters, numbers or underscores.")
    return username


class SignupIn(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=60)
    invite: str = ""


class LoginIn(BaseModel):
    username: str
    password: str


class MePatch(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=60)
    bio: Optional[str] = Field(default=None, max_length=300)
    is_private: Optional[bool] = None
    goal: Optional[int] = Field(default=None, ge=0, le=1000)  # books this year


class ResetIn(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=200)


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=8, max_length=200)


@router.get("/api/auth/state")
def auth_state(user: Optional[dict] = Depends(optional_user)):
    with closing(db()) as conn:
        setup = conn.execute("SELECT count(*) FROM users WHERE is_guest=0").fetchone()[0] == 0
    return {"setup": setup, "user": me_json(user) if user else None}


@router.get("/api/invites/{code}")
def invite_info(code: str):
    """Who invited you, shown on the sign-up screen."""
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT u.username, u.display_name FROM invites i JOIN users u ON u.id = i.created_by"
            " WHERE i.code=? AND i.used_by IS NULL", (code,)).fetchone()
    if not row:
        raise HTTPException(404, "This invite link isn't valid or has already been used.")
    return dict(row)


@router.post("/api/auth/signup")
def signup(body: SignupIn, request: Request, response: Response):
    throttle("signup:" + (request.client.host if request.client else ""), limit=10, window=3600)
    username = check_username(body.username)
    with closing(db()) as conn:
        first = conn.execute("SELECT count(*) FROM users WHERE is_guest=0").fetchone()[0] == 0
        invite = None
        if not first:
            invite = conn.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL",
                                  (body.invite.strip(),)).fetchone()
            if not invite:
                raise HTTPException(403, "Shelfie is invite-only. Ask a friend for an invite link.")
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "That username is taken.")
        uid = conn.execute(
            "INSERT INTO users(username,display_name,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
            (username, body.display_name.strip() or username, hash_password(body.password), int(first), now()),
        ).lastrowid
        if first:
            # the person setting Shelfie up keeps the library they built before accounts existed
            for table in ("entries", "lists", "annotations"):
                conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (uid,))
        else:
            conn.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?", (uid, now(), invite["code"]))
            # whoever invited you is a friend from the start
            for a, b in ((uid, invite["created_by"]), (invite["created_by"], uid)):
                conn.execute("INSERT OR IGNORE INTO follows VALUES(?,?, 'accepted', ?)", (a, b, now()))
        conn.commit()
        user = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    start_session(request, response, uid)
    return me_json(user)


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    username = body.username.strip().lower().lstrip("@")
    throttle(f"login:{request.client.host if request.client else ''}:{username}")
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Wrong username or password.")
    start_session(request, response, row["id"])
    return me_json(dict(row))


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with closing(db()) as conn:
            user = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                                (_token_hash(token),)).fetchone()
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            if user and user["is_guest"]:
                # a guest has no password, so signing out is goodbye: don't leave their data orphaned
                conn.execute("DELETE FROM users WHERE id=?", (user["id"],))
            conn.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
def get_me(me: dict = Depends(current_user)):
    return me_json(me)


@router.patch("/api/me")
def patch_me(p: MePatch, me: dict = Depends(current_user)):
    data = {k: v for k, v in p.model_dump(exclude_unset=True).items() if v is not None}
    if "display_name" in data:
        data["display_name"] = data["display_name"].strip() or me["username"]
    if "is_private" in data:
        data["is_private"] = int(data["is_private"])
    if not data:
        return me_json(me)
    with closing(db()) as conn:
        conn.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in data)} WHERE id=?", [*data.values(), me["id"]])
        if data.get("is_private") == 0:
            # going public lets everyone who asked in
            conn.execute("UPDATE follows SET status='accepted' WHERE followee_id=? AND status='pending'", (me["id"],))
        conn.commit()
        return me_json(dict(conn.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()))


@router.post("/api/me/password")
def change_password(p: PasswordIn, request: Request, me: dict = Depends(current_user)):
    throttle(f"pw:{me['id']}")
    if not check_password(p.current, me["password_hash"]):
        raise HTTPException(403, "Your current password is wrong.")
    with closing(db()) as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(p.new), me["id"]))
        # sign out every other device
        token = request.cookies.get(COOKIE, "")
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (me["id"], _token_hash(token)))
        conn.commit()
    return {"ok": True}


@router.post("/api/invites")
def create_invite(me: dict = Depends(member)):
    with closing(db()) as conn:
        unused = conn.execute("SELECT count(*) FROM invites WHERE created_by=? AND used_by IS NULL",
                              (me["id"],)).fetchone()[0]
        if unused >= 20:
            raise HTTPException(429, "You have 20 unused invites already. Share those first.")
        code = secrets.token_urlsafe(9)
        conn.execute("INSERT INTO invites(code,created_by,created_at) VALUES(?,?,?)", (code, me["id"], now()))
        conn.commit()
    return {"code": code}


@router.get("/api/invites")
def my_invites(me: dict = Depends(member)):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT i.code, i.created_at, i.used_at, u.username AS used_by FROM invites i"
            " LEFT JOIN users u ON u.id = i.used_by WHERE i.created_by=? ORDER BY i.created_at DESC",
            (me["id"],)).fetchall()
    return [dict(r) for r in rows]


# ---------- getting back in without email ----------
RESET_HOURS = 48


@router.post("/api/users/{username}/reset-link")
def create_reset_link(username: str, me: dict = Depends(current_user)):
    """The person who set Shelfie up can let a locked-out friend choose a new password."""
    if not me["is_admin"]:
        raise HTTPException(403, "Only the Shelfie owner can do that.")
    with closing(db()) as conn:
        user = conn.execute("SELECT id FROM users WHERE username=?", (username.lstrip("@").lower(),)).fetchone()
        if not user:
            raise HTTPException(404, "No one here by that name.")
        token = secrets.token_urlsafe(24)
        expires = (datetime.fromisoformat(now()) + timedelta(hours=RESET_HOURS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM reset_tokens WHERE user_id=?", (user["id"],))
        conn.execute("INSERT INTO reset_tokens VALUES(?,?,?)", (_token_hash(token), user["id"], expires))
        conn.commit()
    return {"path": f"/reset/{token}", "hours": RESET_HOURS}


def _reset_user(conn, token: str):
    row = conn.execute("SELECT u.* FROM reset_tokens t JOIN users u ON u.id=t.user_id"
                       " WHERE t.token_hash=? AND t.expires_at>?", (_token_hash(token), now())).fetchone()
    if not row:
        raise HTTPException(404, "This reset link has expired or was already used. Ask for a new one.")
    return dict(row)


@router.get("/api/auth/reset/{token}")
def reset_info(token: str):
    with closing(db()) as conn:
        u = _reset_user(conn, token)
    return {"username": u["username"], "display_name": u["display_name"]}


@router.post("/api/auth/reset")
def reset_password(body: ResetIn, request: Request, response: Response):
    throttle("reset:" + (request.client.host if request.client else ""))
    with closing(db()) as conn:
        u = _reset_user(conn, body.token)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.password), u["id"]))
        conn.execute("DELETE FROM reset_tokens WHERE user_id=?", (u["id"],))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))  # sign out everywhere else
        conn.commit()
    start_session(request, response, u["id"])
    return me_json(u)


# ---------- guests: try Shelfie without an invite ----------
@router.post("/api/auth/guest")
def guest(request: Request, response: Response):
    """A private, personal-only account with no password. Reading, shelves, highlights and
    stats work; friends and the feed wait until they join with an invite."""
    throttle("guest:" + (request.client.host if request.client else ""), limit=20, window=3600)
    with closing(db()) as conn:
        for _ in range(5):
            name = "guest_" + secrets.token_hex(4)
            if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
                break
        uid = conn.execute(
            "INSERT INTO users(username,display_name,password_hash,is_private,is_guest,created_at) VALUES(?,?,?,1,1,?)",
            # an unguessable hash nobody knows the password for: guests can't sign in from elsewhere
            (name, "Guest", hash_password(secrets.token_urlsafe(32)), now())).lastrowid
        conn.commit()
        user = dict(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())
    start_session(request, response, uid)
    return me_json(user)


class UpgradeIn(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=60)
    invite: str = ""


@router.post("/api/auth/upgrade")
def upgrade(body: UpgradeIn, request: Request, me: dict = Depends(current_user)):
    """Turn a guest into a full account. Everything they read, shelved and highlighted stays."""
    if not me.get("is_guest"):
        raise HTTPException(400, "You already have an account.")
    throttle("upgrade:" + (request.client.host if request.client else ""), limit=10, window=3600)
    username = check_username(body.username)
    with closing(db()) as conn:
        first = conn.execute("SELECT count(*) FROM users WHERE is_guest=0").fetchone()[0] == 0
        invite = None
        if not first:
            invite = conn.execute("SELECT * FROM invites WHERE code=? AND used_by IS NULL", (body.invite.strip(),)).fetchone()
            if not invite:
                raise HTTPException(403, "You need an invite link to join. Request one, or ask a friend.")
        if conn.execute("SELECT 1 FROM users WHERE username=? AND id<>?", (username, me["id"])).fetchone():
            raise HTTPException(409, "That username is taken.")
        conn.execute("UPDATE users SET username=?, display_name=?, password_hash=?, is_guest=0, is_private=0, is_admin=?"
                     " WHERE id=?", (username, body.display_name.strip() or username, hash_password(body.password),
                                     int(first), me["id"]))
        if first:
            for table in ("entries", "lists", "annotations"):
                conn.execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (me["id"],))
        else:
            conn.execute("UPDATE invites SET used_by=?, used_at=? WHERE code=?", (me["id"], now(), invite["code"]))
            for a, b in ((me["id"], invite["created_by"]), (invite["created_by"], me["id"])):
                conn.execute("INSERT OR IGNORE INTO follows VALUES(?,?, 'accepted', ?)", (a, b, now()))
        conn.commit()
        return me_json(dict(conn.execute("SELECT * FROM users WHERE id=?", (me["id"],)).fetchone()))


# ---------- invite requests from the website ----------
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")


class WaitlistIn(BaseModel):
    email: str = Field(max_length=254)
    note: str = Field(default="", max_length=300)


@router.post("/api/waitlist")
def join_waitlist(body: WaitlistIn, request: Request):
    throttle("waitlist:" + (request.client.host if request.client else ""), limit=5, window=3600)
    email = body.email.strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "That doesn't look like an email address.")
    with closing(db()) as conn:
        conn.execute("INSERT INTO waitlist(email,note,created_at) VALUES(?,?,?) ON CONFLICT(email) DO NOTHING",
                     (email, body.note.strip(), now()))
        conn.commit()
    # same answer whether or not they asked before, so the list can't be probed
    return {"ok": True}


def admin(user: dict = Depends(member)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Only the Shelfie owner can do that.")
    return user


@router.get("/api/waitlist")
def waitlist(me: dict = Depends(admin)):
    with closing(db()) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM waitlist ORDER BY invite_code IS NOT NULL, created_at DESC")]


@router.post("/api/waitlist/{wid}/invite")
def invite_from_waitlist(wid: int, me: dict = Depends(admin)):
    """Make an invite link for someone on the list; you send it to them yourself."""
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM waitlist WHERE id=?", (wid,)).fetchone()
        if not row:
            raise HTTPException(404)
        code = row["invite_code"]
        if not code:
            code = secrets.token_urlsafe(9)
            conn.execute("INSERT INTO invites(code,created_by,created_at) VALUES(?,?,?)", (code, me["id"], now()))
            conn.execute("UPDATE waitlist SET invite_code=? WHERE id=?", (code, wid))
            conn.commit()
    return {"code": code, "email": row["email"]}


@router.delete("/api/waitlist/{wid}")
def remove_from_waitlist(wid: int, me: dict = Depends(admin)):
    with closing(db()) as conn:
        conn.execute("DELETE FROM waitlist WHERE id=?", (wid,))
        conn.commit()
    return {"ok": True}
