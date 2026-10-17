"""Official LinkedIn API: OAuth sign-in and publishing posts to your own profile.

Only the self-serve products "Sign In with LinkedIn using OpenID Connect" and
"Share on LinkedIn" are used. LinkedIn does not offer an API for messaging,
commenting on other people's posts, or reading the feed to regular developers,
so those parts of the agent produce drafts that you send yourself.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx2 as httpx

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
SCOPES = "openid profile email w_member_social"

# Characters with special meaning in LinkedIn's "little text format" (post
# commentary). Unescaped, they can silently truncate a post.
_RESERVED = set("\\|{}@[]()<>#*_~")
_HASHTAG = re.compile(r"(?<![\w&])#(\w+)")


class LinkedInError(RuntimeError):
    pass


def to_little_text(text: str) -> str:
    """Escape post text for the Posts API, keeping #hashtags clickable."""
    parts = _HASHTAG.split(text)
    out = []
    for i, part in enumerate(parts):
        if i % 2:  # captured hashtag word
            out.append("{hashtag|\\#|" + part + "}")
        else:
            out.append("".join("\\" + c if c in _RESERVED else c for c in part))
    return "".join(out)


def candidate_versions(today: date, count: int = 6) -> list[str]:
    """LinkedIn-Version header values to try, newest first (starting last month).
    LinkedIn supports each monthly version for about a year."""
    year, month = today.year, today.month
    versions = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        versions.append(f"{year}{month:02d}")
    return versions


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"


# ---- OAuth ----------------------------------------------------------------------

def authorize(token_path: Path, client_id: str, client_secret: str, redirect_uri: str) -> dict:
    """Run the browser sign-in once and store the access token (valid ~60 days)."""
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": SCOPES,
    })
    print("Opening LinkedIn sign-in. If the browser does not open, visit:\n" + url)
    webbrowser.open(url)
    code = _wait_for_code(redirect_uri, state)

    with httpx.Client(timeout=30) as http:
        resp = http.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        })
        if resp.status_code != 200:
            raise LinkedInError(f"Token exchange failed ({resp.status_code}): {resp.text}")
        token = resp.json()
        info = http.get(USERINFO_URL, headers={"Authorization": f"Bearer {token['access_token']}"})
        if info.status_code != 200:
            raise LinkedInError(f"Could not read your LinkedIn profile ({info.status_code}): {info.text}")
        user = info.json()

    saved = {
        "access_token": token["access_token"],
        "expires_at": int(time.time()) + int(token.get("expires_in", 0)),
        "person_urn": f"urn:li:person:{user['sub']}",
        "name": user.get("name"),
    }
    token_path.write_text(json.dumps(saved, indent=2))
    return saved


def _wait_for_code(redirect_uri: str, state: str) -> str:
    parsed = urlparse(redirect_uri)
    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path == parsed.path:
                result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"LinkedIn connected. You can close this tab.")

        def log_message(self, *args):
            pass

    server = HTTPServer((parsed.hostname or "localhost", parsed.port or 80), Handler)
    server.timeout = 5
    deadline = time.time() + 300
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise LinkedInError("Timed out waiting for LinkedIn sign-in (5 minutes). Run the command again.")
    if "error" in result:
        raise LinkedInError(f"LinkedIn sign-in failed: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise LinkedInError("LinkedIn sign-in failed: state mismatch (possible stale tab). Try again.")
    return result["code"]


# ---- Publishing -----------------------------------------------------------------

class LinkedInClient:
    def __init__(self, token_path: Path, http: httpx.Client | None = None, version: str | None = None):
        if not token_path.exists():
            raise LinkedInError("LinkedIn is not connected. Run `python -m career_agent linkedin-auth`.")
        self.token = json.loads(token_path.read_text())
        self.http = http or httpx.Client(timeout=30)
        self.version = version

    def days_left(self) -> int:
        return int((self.token["expires_at"] - time.time()) // 86400)

    def publish(self, text: str) -> str:
        """Publish a public text post as you. Returns the post URN."""
        if self.days_left() < 0:
            raise LinkedInError("LinkedIn token expired. Run `python -m career_agent linkedin-auth` again.")
        body = {
            "author": self.token["person_urn"],
            "commentary": to_little_text(text),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        versions = [self.version] if self.version else candidate_versions(date.today())
        for version in versions:
            resp = self.http.post(POSTS_URL, json=body, headers={
                "Authorization": f"Bearer {self.token['access_token']}",
                "LinkedIn-Version": version,
                "X-Restli-Protocol-Version": "2.0.0",
            })
            if resp.status_code == 201:
                return resp.headers.get("x-restli-id", "")
            if resp.status_code == 401:
                raise LinkedInError("LinkedIn rejected the token. Run `python -m career_agent linkedin-auth` again.")
            if resp.status_code == 426 or "VERSION" in resp.text:
                continue  # that monthly API version is not available; try an older one
            raise LinkedInError(f"LinkedIn post failed ({resp.status_code}): {resp.text}")
        raise LinkedInError(f"No supported LinkedIn-Version among {versions}. Set LINKEDIN_API_VERSION in .env.")
