# Shelfie

Track your books, read free classics, and follow friends' shelves. Mobile-first, works offline.

## Run
    pip install -r requirements.txt
    python -m uvicorn app:app
    # open http://127.0.0.1:8000

The first account you create is yours and keeps any library made before accounts existed.
After that Shelfie is invite-only: make an invite link from the Friends tab or your
profile settings and send it to a friend. Joining through it makes you friends.

To use it on your phone at home, start it on your network and open
`http://<this computer's IP>:8000` on the phone (same Wi-Fi):

    python -m uvicorn app:app --host 0.0.0.0

Offline reading and keeping the screen awake need https (see Deploy).

## Code
- `app.py`: library, lists, import/export, highlights, the Gutenberg catalog and book text, page routes
- `auth.py`: accounts, invites, sessions (scrypt password hashes, hashed session tokens)
- `social.py`: profiles, follows, feed, likes, and `visible_to()`, the one place deciding who sees what
- `store.py`: database schema and migrations (a copy is kept before each structural migration)
- `static/index.html`: the whole app UI; `static/sw.js`: offline support

## Privacy
Each book is Everyone, Friends (people you follow who follow you back) or Only me.
A private account must approve followers. Highlights stay private unless you share one.
Anyone with your link can see a public profile, signed in or not.

## Deploy (Fly.io)
1. Install `flyctl` and sign in (`fly auth login`).
2. Edit `fly.toml`: pick a unique `app` name and a `primary_region` near you.
3. `fly launch --no-deploy --copy-config`, then `fly volumes create shelfie_data --size 1`
4. `fly deploy`, then open `https://<app>.fly.dev` and create your account.

To bring your current library: before step 4's first sign-up, copy `shelfie.db` to the volume
(`fly ssh sftp shell`, then `put shelfie.db /data/shelfie.db`) and restart the app.
SQLite lives on one volume, so run exactly one machine.

## Features
- Shelves (Want to read / Reading / Read / Did not finish), progress, half-star ratings, reviews
- Genres, moods, tags, content warnings, lists
- Book search via Open Library; import Goodreads/StoryGraph CSV; export CSV
- Discover & read 70k+ free public-domain books (Project Gutenberg) in a paginated reader
  with themes, fonts, contents, highlights, notes and bookmarks; works offline
- Profiles with shareable links, follows, a friends feed with likes, "reading now",
  books in common, one-tap "Want to read" from a friend's shelf
