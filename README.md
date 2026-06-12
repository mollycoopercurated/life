# Instagram Downloader

A small local web app: paste an Instagram profile URL (or username), browse the
most recent posts in a grid, tick the ones you want, and download their
images/videos to a local folder. Carousels and videos are handled
automatically.

For personal use. Respect Instagram's Terms of Service and copyright.

## Setup

```bash
pip install -r requirements.txt
python3 app.py
```

Then open <http://localhost:5000>.

## Important: Instagram requires a login

Instagram now blocks almost all **logged-out** access — anonymous requests get a
`403 Forbidden`. So while this tool targets *public* profiles, you still need to
be signed in with **some** Instagram account for it to fetch anything.

You log in **once**; the session is cached to `.sessions/` and reused, so you
don't enter your password again.

Set these environment variables before starting the app:

```bash
export IG_USERNAME="your_instagram_username"
export IG_PASSWORD="your_password"   # only needed the first time, to create the session
python3 app.py
```

After the first successful run you can drop `IG_PASSWORD` — the cached session in
`.sessions/<username>` is used automatically.

Notes:
- The password is only used to create the session and is **never stored** — only
  the resulting session cookie is cached in `.sessions/` (which is gitignored).
- Use an account you're comfortable scripting with. Heavy use can trigger
  Instagram rate limits or checkpoints; if that happens, wait a while.
- If your account has two-factor auth, instaloader's programmatic login may
  prompt/fail. The most reliable alternative is to import your browser session —
  see [instaloader's docs](https://instaloader.github.io/troubleshooting.html#login-error)
  and drop the resulting file at `.sessions/<username>`.

## How to use

1. Paste a profile URL (`https://instagram.com/nasa`) or just a username
   (`nasa`) and choose how many recent posts to load.
2. Click **Load posts** — recent posts appear as a thumbnail grid. Badges show
   `▶ video` or `▦ N` for multi-image carousels.
3. Click posts to select them (or **Select all**).
4. Click **Download selected** — files are saved to `downloads/<username>/`.

## How it works

- **Backend** (`app.py`, Flask):
  - `GET /api/posts?profile=&count=` — fetches recent post metadata via
    [instaloader](https://instaloader.github.io/).
  - `GET /api/thumb?u=` — proxies CDN thumbnails so the browser avoids
    referrer/expiry quirks.
  - `POST /api/download` — downloads the selected posts (media only; no
    metadata/comment files).
  - `GET /api/status` — reports whether a login session is configured.
- **Frontend** (`templates/index.html`): a single self-contained page — grid,
  selection, and download handling in vanilla JS.

## Project layout

```
app.py                 Flask backend
templates/index.html   Single-page UI
requirements.txt       Python dependencies
downloads/             Saved media (gitignored)
.sessions/             Cached login session (gitignored)
```
