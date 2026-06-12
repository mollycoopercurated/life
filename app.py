"""
Instagram post downloader — local web app.

Paste an Instagram profile URL (or username), browse the most recent posts,
tick the ones you want, and download their images/videos to ./downloads.

Public profiles only. Instagram blocks most logged-out access, so log in once by
importing a browser session (see the README). Posts are read from Instagram's
`web_profile_info` endpoint, which returns a profile's most recent posts without
the GraphQL queries that Instagram has been rejecting.
"""

import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

import instaloader

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
SESSION_DIR = os.path.join(BASE_DIR, ".sessions")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

# Login. Instagram blocks most anonymous access, so we reuse a logged-in
# session. IG_USERNAME selects which cached session file in .sessions/ to load
# (created by importing a browser session — see the README). IG_PASSWORD is a
# fallback for instaloader's direct login, which Instagram often rejects.
IG_USERNAME = os.environ.get("IG_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
_logged_in_as = None  # username we currently have a session for, if any

# The public web app id Instagram's own site sends; required by web_profile_info.
IG_APP_ID = "936619743392459"

# A browser-ish UA helps avoid being blocked.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# instaloader is not thread-safe; serialise access to it.
_il_lock = threading.Lock()

# Remember each post's media URLs from the listing so downloads don't need any
# further Instagram metadata calls — we just fetch the files from the CDN.
# shortcode -> {"username": str, "items": [(url, ext), ...]}
_media_cache = {}


def make_loader() -> instaloader.Instaloader:
    """An Instaloader configured to save media only (no metadata/comments)."""
    return instaloader.Instaloader(
        dirname_pattern=os.path.join(DOWNLOAD_DIR, "{profile}"),
        download_comments=False,
        download_geotags=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        compress_json=False,
        quiet=True,
    )


_loader = make_loader()


def ensure_login() -> None:
    """Load the cached session if IG_USERNAME is configured.

    Tries the saved session file first (created by importing a browser session);
    only falls back to username/password login if no session file exists. Safe to
    call repeatedly. Raises on failure so callers can surface the reason.
    """
    global _logged_in_as
    if not IG_USERNAME or _logged_in_as == IG_USERNAME:
        return

    session_file = os.path.join(SESSION_DIR, IG_USERNAME)
    try:
        _loader.load_session_from_file(IG_USERNAME, session_file)
        _logged_in_as = IG_USERNAME
        return
    except FileNotFoundError:
        pass

    if not IG_PASSWORD:
        raise RuntimeError(
            "No cached session found at .sessions/%s and IG_PASSWORD is not set. "
            "Import a browser session (see the README) or set IG_PASSWORD." % IG_USERNAME
        )
    _loader.login(IG_USERNAME, IG_PASSWORD)
    _loader.save_session_to_file(session_file)
    _logged_in_as = IG_USERNAME


class ProfileError(Exception):
    """Carries an HTTP status and message back to the API layer."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def _extract_media(node) -> list:
    """Return [(media_url, extension), ...] for a post node (handles carousels)."""
    items = []
    children = node.get("edge_sidecar_to_children")
    if children and children.get("edges"):
        for child in children["edges"]:
            cn = child["node"]
            if cn.get("is_video") and cn.get("video_url"):
                items.append((cn["video_url"], "mp4"))
            elif cn.get("display_url"):
                items.append((cn["display_url"], "jpg"))
    else:
        if node.get("is_video") and node.get("video_url"):
            items.append((node["video_url"], "mp4"))
        elif node.get("display_url"):
            items.append((node["display_url"], "jpg"))
    return items


def fetch_profile(username: str) -> dict:
    """Fetch a profile and its recent posts via Instagram's web_profile_info API.

    Uses the logged-in session's cookies. Returns the raw ``user`` dict. Raises
    ProfileError with a friendly message and HTTP status on failure.
    """
    sess = _loader.context._session
    headers = {
        "User-Agent": BROWSER_UA,
        "X-IG-App-ID": IG_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{username}/",
        "Accept": "*/*",
    }
    try:
        resp = sess.get(
            "https://www.instagram.com/api/v1/users/web_profile_info/",
            params={"username": username},
            headers=headers,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise ProfileError(f"Could not reach Instagram: {exc}", 502)

    if resp.status_code == 404:
        raise ProfileError(f"No Instagram profile found for '{username}'.", 404)
    if resp.status_code in (401, 403):
        raise ProfileError(
            "Instagram rejected the request — your saved session may have expired. "
            "Re-import your browser session (see the README) and try again.",
            401,
        )
    if resp.status_code == 429:
        raise ProfileError(
            "Instagram is rate-limiting you. Wait a few minutes and try again.", 429
        )
    if resp.status_code != 200:
        raise ProfileError(
            f"Instagram returned an unexpected status ({resp.status_code}).", 502
        )
    try:
        user = resp.json()["data"]["user"]
    except (ValueError, KeyError, TypeError):
        raise ProfileError("Instagram returned an unexpected response.", 502)
    if user is None:
        raise ProfileError(f"No Instagram profile found for '{username}'.", 404)
    return user


def load_posts(username: str, count: int) -> dict:
    """Return profile metadata + a list of recent posts, caching media for download."""
    user = fetch_profile(username)
    timeline = user.get("edge_owner_to_timeline_media", {}) or {}
    edges = timeline.get("edges", []) or []

    posts = []
    for edge in edges[:count]:
        node = edge.get("node", {})
        shortcode = node.get("shortcode")
        if not shortcode:
            continue
        items = _extract_media(node)
        _media_cache[shortcode] = {"username": username, "items": items}

        cap_edges = (node.get("edge_media_to_caption") or {}).get("edges") or []
        caption = cap_edges[0]["node"]["text"] if cap_edges else ""
        likes = (node.get("edge_liked_by") or node.get("edge_media_preview_like") or {}).get("count", 0)
        ts = node.get("taken_at_timestamp")
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        is_carousel = bool((node.get("edge_sidecar_to_children") or {}).get("edges"))

        posts.append(
            {
                "shortcode": shortcode,
                "thumb": node.get("display_url", ""),
                "is_video": bool(node.get("is_video")),
                "is_carousel": is_carousel,
                "media_count": max(1, len(items)),
                "caption": caption[:140],
                "date": date,
                "likes": likes,
                "url": f"https://www.instagram.com/p/{shortcode}/",
            }
        )

    return {
        "username": username,
        "full_name": user.get("full_name") or "",
        "post_count": timeline.get("count", len(posts)),
        "is_private": bool(user.get("is_private")),
        "posts": posts,
    }


def parse_username(value: str) -> str:
    """Accept a username, @username, or any instagram.com URL and return the username."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        return value[1:].strip("/")
    if "instagram.com" in value:
        from urllib.parse import urlparse

        path = urlparse(value if "://" in value else "https://" + value).path
        parts = [p for p in path.split("/") if p]
        skip = {"p", "reel", "reels", "tv", "stories", "explore", "accounts"}
        if parts and parts[0] not in skip:
            return parts[0]
        return ""
    return value.strip("/").split("/")[0]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Report whether a logged-in session is configured (the UI shows a hint if not)."""
    return jsonify({"logged_in": bool(IG_USERNAME), "username": IG_USERNAME or None})


@app.route("/api/posts")
def api_posts():
    """Return metadata for the most recent posts of a public profile."""
    username = parse_username(request.args.get("profile", ""))
    if not username or not re.fullmatch(r"[A-Za-z0-9._]{1,40}", username):
        return jsonify({"error": "Please enter a valid Instagram username or profile URL."}), 400

    try:
        count = max(1, min(int(request.args.get("count", 12)), 50))
    except ValueError:
        count = 12

    try:
        with _il_lock:
            ensure_login()
            data = load_posts(username, count)
    except ProfileError as exc:
        return jsonify({"error": str(exc)}), exc.status
    except Exception as exc:  # noqa: BLE001 - surface anything else to the UI
        return jsonify({"error": f"Login or fetch failed: {exc}"}), 500

    if data["is_private"]:
        return jsonify(
            {"error": f"'{username}' is a private account. This tool only supports public profiles."}
        ), 403

    return jsonify(data)


@app.route("/api/thumb")
def api_thumb():
    """Proxy a thumbnail image so the browser doesn't deal with CDN/referrer quirks."""
    url = request.args.get("u", "")
    if not url.startswith("https://"):
        return Response("bad url", status=400)
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return Response("could not fetch thumbnail", status=502)
    return Response(
        r.content,
        content_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _download_media(username: str, shortcode: str) -> int:
    """Download all media for one post from the cached CDN URLs. Returns file count."""
    entry = _media_cache.get(shortcode)
    if entry is None:
        # Cache miss (e.g. app restarted) — re-fetch this profile's recent posts.
        load_posts(username, 50)
        entry = _media_cache.get(shortcode)
    if entry is None or not entry["items"]:
        raise ProfileError("This post is no longer in the recent list — reload posts.", 404)

    target_dir = os.path.join(DOWNLOAD_DIR, username)
    os.makedirs(target_dir, exist_ok=True)
    items = entry["items"]
    saved = 0
    for idx, (url, ext) in enumerate(items, start=1):
        suffix = "" if len(items) == 1 else f"_{idx}"
        path = os.path.join(target_dir, f"{shortcode}{suffix}.{ext}")
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(r.content)
        saved += 1
    return saved


@app.route("/api/download", methods=["POST"])
def api_download():
    """Download the selected posts (all images/videos, including carousels)."""
    data = request.get_json(silent=True) or {}
    username = parse_username(data.get("profile", ""))
    shortcodes = data.get("shortcodes", [])

    if not username or not re.fullmatch(r"[A-Za-z0-9._]{1,40}", username):
        return jsonify({"error": "Invalid profile."}), 400
    if not isinstance(shortcodes, list) or not shortcodes:
        return jsonify({"error": "No posts selected."}), 400

    results = []
    with _il_lock:
        try:
            ensure_login()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Login failed: {exc}"}), 401
        for shortcode in shortcodes:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", str(shortcode)):
                results.append({"shortcode": shortcode, "ok": False, "error": "invalid shortcode"})
                continue
            try:
                files = _download_media(username, shortcode)
                results.append({"shortcode": shortcode, "ok": True, "media_count": files})
            except Exception as exc:  # noqa: BLE001 - report per-post failures
                results.append({"shortcode": shortcode, "ok": False, "error": str(exc)})
            time.sleep(0.3)  # be gentle on the CDN

    ok_count = sum(1 for r in results if r["ok"])
    return jsonify(
        {
            "downloaded": ok_count,
            "total": len(results),
            "folder": os.path.join("downloads", username),
            "results": results,
        }
    )


@app.route("/downloads/<path:filename>")
def serve_download(filename):
    """Let the user open downloaded files directly from the browser."""
    return send_from_directory(DOWNLOAD_DIR, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Instagram downloader running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
