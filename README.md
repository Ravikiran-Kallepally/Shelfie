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

## Android app (`mobile/`)
A Capacitor shell around the web app, so the APK always runs the server's current UI. It adds
the hardware back button (closes sheets, then the reader, then goes back), a themed status bar,
keep-screen-on while reading, Android's share sheet, a splash screen and an offline screen.

Needs Node 22+, JDK 21 and the Android SDK.

    cd mobile
    npm install
    node set-server.mjs https://shelfie-ravikiran.fly.dev   # or http://<your PC's IP>:8000 to test
    npm run apk        # debug build -> android/app/build/outputs/apk/debug/app-debug.apk
    cd android && gradlew.bat assembleRelease   # signed release -> .../apk/release/app-release.apk

Release builds are signed with `android/shelfie-release.jks`, configured in
`android/keystore.properties`. Both are kept out of git; back them up, because every future
update must be signed with the same key or Android refuses to install it.

Plain `http://` is allowed only so a test build can reach a server on your own Wi-Fi; point
release builds at an https address. Icons and splash screens are generated from
`mobile/assets/` (`python make-assets.py`, then `npx capacitor-assets generate --android`).

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
Live at https://shelfie-ravikiran.fly.dev (app `shelfie-ravikiran`, region `ord`, one machine
with a 1 GB volume that Fly snapshots daily). To ship a change:

    fly deploy --ha=false

Keep it to one machine: the SQLite database lives on that machine's volume. Useful commands:
`fly logs`, `fly ssh console`, `fly volumes snapshots list shelfie_data`.

To run your own copy: `fly apps create <name>`, `fly volumes create shelfie_data --size 1`,
set `app` and `primary_region` in `fly.toml`, then deploy. The first account created on a fresh
server becomes the owner, so sign up right after the first deploy (or upload an existing
`shelfie.db` to `/data` with `fly ssh sftp put` and restart).

The Android app offered on the website is `/data/downloads/Shelfie.apk` on the volume.

## Features
- Shelves (Want to read / Reading / Read / Did not finish), progress, half-star ratings, reviews
- Genres, moods, tags, content warnings, lists
- Book search via Open Library; import Goodreads/StoryGraph CSV; export CSV
- Discover & read 70k+ free public-domain books (Project Gutenberg) in a paginated reader
  with themes, fonts, contents, highlights, notes and bookmarks; works offline
- Profiles with shareable links, follows, a friends feed with likes, "reading now",
  books in common, one-tap "Want to read" from a friend's shelf
