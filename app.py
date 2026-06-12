"""
Instagram post downloader — local web app.

Paste an Instagram profile URL (or username), browse the most recent posts,
tick the ones you want, and download their images/videos to ./downloads.

Public profiles only. Instagram blocks most logged-out access, so configure a
login via the IG_USERNAME / IG_PASSWORD environment variables (the session is
cached after the first run). See the README for details.
"""

import os
import re
import threading
from urllib.parse import urlparse

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

# Optional login. Instagram now blocks most anonymous access, so to reliably
# browse/download you log in once with an account. Credentials are read from
# the environment (IG_USERNAME / IG_PASSWORD) and the session is cached to disk
# so you only authenticate once. We never store the password.
IG_USERNAME = os.environ.get("IG_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")
_logged_in_as = None  # username we currently have a session for, if any

# A browser-ish UA helps avoid being blocked when proxying CDN thumbnails.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# instaloader is not thread-safe; serialise access to it.
_il_lock = threading.Lock()


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
    """Log in (or restore a cached session) if IG_USERNAME is configured.

    Tries a saved session file first; falls back to username/password and
    caches the resulting session. Safe to call repeatedly — it's a no-op once
    a session is active. Raises on failure so callers can surface the reason.
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
            "IG_USERNAME is set but no cached session exists and IG_PASSWORD is "
            "not set. Set IG_PASSWORD once so a session can be created."
        )
    _loader.login(IG_USERNAME, IG_PASSWORD)
    _loader.save_session_to_file(session_file)
    _logged_in_as = IG_USERNAME


def parse_username(value: str) -> str:
    """Accept a username, @username, or any instagram.com URL and return the username."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        return value[1:].strip("/")
    if "instagram.com" in value:
        path = urlparse(value if "://" in value else "https://" + value).path
        # /<username>/ or /<username>/p/<shortcode>/ etc.
        parts = [p for p in path.split("/") if p]
        # Skip Instagram's own route prefixes.
        skip = {"p", "reel", "reels", "tv", "stories", "explore", "accounts"}
        if parts and parts[0] not in skip:
            return parts[0]
        return ""
    # Plain username — strip any stray slashes/spaces.
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
            profile = instaloader.Profile.from_username(_loader.context, username)
            is_private = profile.is_private
            full_name = profile.full_name
            post_count = profile.mediacount
            posts = []
            if not is_private:
                for post in profile.get_posts():
                    posts.append(
                        {
                            "shortcode": post.shortcode,
                            "thumb": post.url,
                            "is_video": post.is_video,
                            "is_carousel": post.typename == "GraphSidecar",
                            "media_count": post.mediacount,
                            "caption": (post.caption or "")[:140],
                            "date": post.date_utc.strftime("%Y-%m-%d"),
                            "likes": post.likes,
                            "url": f"https://www.instagram.com/p/{post.shortcode}/",
                        }
                    )
                    if len(posts) >= count:
                        break
    except instaloader.exceptions.ProfileNotExistsException:
        return jsonify({"error": f"No Instagram profile found for '{username}'."}), 404
    except instaloader.exceptions.LoginRequiredException:
        return jsonify(
            {"error": "Instagram is asking for a login to view this. Anonymous access "
                      "is rate-limited — wait a few minutes and try again."}
        ), 429
    except instaloader.exceptions.ConnectionException as exc:
        return jsonify(
            {"error": f"Instagram blocked or rate-limited the request: {exc}. "
                      "Wait a few minutes and try again."}
        ), 429
    except Exception as exc:  # noqa: BLE001 - surface anything else to the UI
        return jsonify({"error": f"Something went wrong: {exc}"}), 500

    if is_private:
        return jsonify(
            {"error": f"'{username}' is a private account. This tool only supports public profiles."}
        ), 403

    return jsonify(
        {
            "username": username,
            "full_name": full_name,
            "post_count": post_count,
            "posts": posts,
        }
    )


@app.route("/api/thumb")
def api_thumb():
    """Proxy a thumbnail image so the browser doesn't deal with CDN/referrer quirks."""
    url = request.args.get("u", "")
    if not url.startswith("https://"):
        return Response("bad url", status=400)
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15, stream=True)
        r.raise_for_status()
    except requests.RequestException:
        return Response("could not fetch thumbnail", status=502)
    return Response(
        r.content,
        content_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


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
                post = instaloader.Post.from_shortcode(_loader.context, shortcode)
                _loader.download_post(post, target=username)
                results.append({"shortcode": shortcode, "ok": True, "media_count": post.mediacount})
            except Exception as exc:  # noqa: BLE001 - report per-post failures
                results.append({"shortcode": shortcode, "ok": False, "error": str(exc)})

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
