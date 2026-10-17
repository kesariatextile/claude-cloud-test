import json
import time
from datetime import date

import httpx2 as httpx
import pytest

from career_agent import posts
from career_agent.linkedin import LinkedInClient, LinkedInError, candidate_versions, to_little_text
from tests.conftest import FakeLLM


def test_little_text_escapes_reserved_and_keeps_hashtags():
    text = "Shipped (finally) a dashboard @work! #DataAnalytics #SQL_tips"
    assert to_little_text(text) == (
        "Shipped \\(finally\\) a dashboard \\@work! {hashtag|\\#|DataAnalytics} {hashtag|\\#|SQL_tips}")


def test_little_text_leaves_html_entities_and_mid_word_hash_alone():
    assert to_little_text("C# & F#") == "C\\# & F\\#"


def test_candidate_versions_start_last_month_and_cross_year():
    assert candidate_versions(date(2026, 2, 10), 3) == ["202601", "202512", "202511"]


def _token(tmp_path, expires_in=86400 * 30):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"access_token": "tok", "expires_at": int(time.time()) + expires_in,
                                "person_urn": "urn:li:person:abc", "name": "Asha"}))
    return path


def test_publish_falls_back_to_older_version(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers["LinkedIn-Version"])
        body = json.loads(request.content)
        assert body["author"] == "urn:li:person:abc"
        assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
        if len(seen) == 1:
            return httpx.Response(426, json={"code": "NONEXISTENT_VERSION"})
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:42"})

    client = LinkedInClient(_token(tmp_path), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.publish("hello (world)") == "urn:li:share:42"
    assert len(seen) == 2 and seen[0] > seen[1]


def test_publish_reports_expired_token(tmp_path):
    client = LinkedInClient(_token(tmp_path, expires_in=-86400 * 2),
                            http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(201))))
    with pytest.raises(LinkedInError, match="expired"):
        client.publish("hi")


def test_publish_401_asks_to_reconnect(tmp_path):
    client = LinkedInClient(_token(tmp_path), version="202609",
                            http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(LinkedInError, match="linkedin-auth"):
        client.publish("hi")


def test_missing_token_file(tmp_path):
    with pytest.raises(LinkedInError, match="not connected"):
        LinkedInClient(tmp_path / "nope.json")


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, text):
        self.published.append(text)
        return "urn:li:share:1"


def test_daily_post_waits_for_approval_then_publishes(cfg, conn):
    llm = FakeLLM(lambda prompt, schema: {"text": "Hook line\n\nBody #SQL"})
    client = FakeClient()
    lines = posts.run_daily(cfg, conn, llm, lambda: client)
    assert "waiting for your approval" in lines[0] and not client.published
    # A second run does not generate another draft.
    posts.run_daily(cfg, conn, llm, lambda: client)
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1

    posts.set_status(conn, 1, "approved")
    lines = posts.run_daily(cfg, conn, llm, lambda: client)
    assert client.published == ["Hook line\n\nBody #SQL"] and "Published post #1" in lines[0]
    assert posts.run_daily(cfg, conn, llm, lambda: client) == ["Already posted today."]


def test_daily_post_auto_publishes_without_approval(cfg, conn):
    cfg.data["content"]["require_approval"] = False
    client = FakeClient()
    posts.run_daily(cfg, conn, FakeLLM(lambda p, s: {"text": "Auto post"}), lambda: client)
    assert client.published == ["Auto post"]
    row = conn.execute("SELECT status, linkedin_urn FROM posts").fetchone()
    assert (row["status"], row["linkedin_urn"]) == ("published", "urn:li:share:1")


def test_theme_rotates(cfg, conn):
    llm = FakeLLM(lambda p, s: {"text": "x"})
    posts.generate(cfg, conn, llm)
    posts.generate(cfg, conn, llm)
    assert [r["theme"] for r in conn.execute("SELECT theme FROM posts ORDER BY id")] == ["Theme A", "Theme B"]


def test_oauth_callback_server_checks_state():
    import socket
    import threading

    from career_agent.linkedin import _wait_for_code

    with socket.socket() as s:
        s.bind(("localhost", 0))
        port = s.getsockname()[1]
    result = {}
    thread = threading.Thread(target=lambda: result.update(code=_wait_for_code(
        f"http://localhost:{port}/callback", "xyz")))
    thread.start()
    for _ in range(50):
        try:
            httpx.get(f"http://localhost:{port}/favicon.ico")
            break
        except httpx.ConnectError:
            time.sleep(0.05)
    resp = httpx.get(f"http://localhost:{port}/callback?code=abc&state=xyz")
    thread.join(5)
    assert resp.status_code == 200 and result == {"code": "abc"}
