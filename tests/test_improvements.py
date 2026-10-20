import json
import re
import sqlite3
from datetime import datetime, timedelta

import pytest

from career_agent import cli, comments, db, outreach, posts, profile
from career_agent.linkedin import LinkedInError
from career_agent.report import write_report
from tests.conftest import FakeLLM, post_reply
from tests.test_humanize import HUMAN


class Client:
    def __init__(self):
        self.published = []

    def publish(self, text):
        self.published.append(text)
        return "urn:li:share:9"


def test_post_draft_records_hook_and_score_and_lists_all_hooks_in_prompt(cfg, conn):
    llm = FakeLLM(lambda p, s: post_reply(HUMAN, chosen=10))
    post_id = posts.generate(cfg, conn, llm)
    row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    assert row["hook"] == "#10 The Receipt" and row["human_score"] > 60
    assert json.loads(row["hook_options"])["reason"] == "numbers are mine"
    assert len(re.findall(r"^#\d+ ", llm.prompts[0], re.MULTILINE)) == 21
    assert "human score" in posts.summary(row)


def test_placeholders_block_approval_and_publishing(cfg, conn):
    cfg.data["content"]["require_approval"] = False  # even in full-auto mode
    client = Client()
    text = "Our churn fell by {{your number}} after one change.\n\nBody."
    lines = posts.run_daily(cfg, conn, FakeLLM(lambda p, s: post_reply(text)), lambda: client)
    assert "fill in first: {{your number}}" in lines[0] and client.published == []
    with pytest.raises(ValueError, match="your number"):
        posts.set_status(conn, 1, "approved")
    with pytest.raises(LinkedInError, match="not publishing"):
        posts.publish(conn, 1, client)
    conn.execute("UPDATE posts SET text='Our churn fell by 12% after one change.' WHERE id=1")
    posts.run_daily(cfg, conn, FakeLLM(lambda p, s: post_reply(text)), lambda: client)
    assert client.published == ["Our churn fell by 12% after one change."]


def _contacts(conn, n):
    for i in range(n):
        outreach.upsert_contact(conn, {"name": f"P{i}", "company": "C", "linkedin_url": f"https://li/{i}"})
    conn.commit()


def _reply(prompt, schema):
    ids = [int(i) for i in re.findall(r"contact_id=(\d+)", prompt)]
    return {"messages": [{"contact_id": i, "text": "Saw your post — great point.", "specific": True} for i in ids]}


def test_two_follow_ups_then_stop(cfg, conn):
    _contacts(conn, 1)
    llm = FakeLLM(_reply)

    def run():
        return [k for _, k in outreach.select_for_today(cfg, conn)]

    def age(days):
        ts = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        conn.execute("UPDATE contacts SET last_action_at=? WHERE id=1", (ts,))

    outreach.draft(cfg, conn, llm, outreach.select_for_today(cfg, conn))
    outreach.mark(conn, 1, "requested")
    outreach.mark(conn, 1, "connected")
    assert run() == ["dm"]
    outreach.draft(cfg, conn, llm, outreach.select_for_today(cfg, conn))
    outreach.mark(conn, 1, "messaged")
    assert run() == []
    age(6)  # follow_up_after_days is 5 in the test profile
    assert run() == ["follow_up"]
    outreach.draft(cfg, conn, llm, outreach.select_for_today(cfg, conn))
    outreach.mark(conn, 1, "messaged")
    age(3)
    assert run() == []
    age(7)
    assert run() == ["close_loop"]
    drafts = outreach.draft(cfg, conn, llm, outreach.select_for_today(cfg, conn))
    assert drafts[0]["text"] == "Saw your post, great point."  # humanized
    outreach.mark(conn, 1, "messaged")
    age(30)
    assert run() == []
    kinds = [r[0] for r in conn.execute("SELECT kind FROM outreach_messages WHERE sent_at IS NOT NULL ORDER BY id")]
    assert kinds == ["connect", "dm", "follow_up", "close_loop"]
    assert "type=close_loop" in llm.prompts[-1] and "won't follow up again" in llm.prompts[-1]


def test_daily_limit_is_capped_at_20(cfg, conn):
    cfg.data["outreach"]["daily_limit"] = 50
    _contacts(conn, 30)
    assert len(outreach.select_for_today(cfg, conn)) == outreach.MAX_DAILY


def test_repeat_author_note(conn):
    llm = FakeLLM(lambda p, s: {"comments": [{"index": 0, "options": [{"type_id": 9, "text": "True."}],
                                              "recommended": 0, "reason": "short"}]})
    notes = [comments.draft(conn, llm, [{"text": "post", "author": "Priya", "url": None}])[0]["note"] for _ in range(4)]
    assert notes[:3] == ["", "", ""] and "Already 3 comments for Priya" in notes[3]
    assert comments.todays_drafts(conn)[3]["note"] == notes[3]


def _profile_llm(prompt, schema):
    ids = re.findall(r"^- (\w+) \(max", prompt, re.MULTILINE)
    if prompt.startswith("Score"):
        rewritten = "HEADLINE:\nData Analyst | SQL" in prompt
        return {"items": [{"id": i, "points": 99 if rewritten else 2, "reason": f"{i} weak"} for i in ids]}
    return {"headline_options": ["Data Analyst | SQL, Python | Fintech dashboards — 40+ users"],
            "about_open": "I build dashboards finance teams trust.", "about_body": "More.",
            "current_role": "- Cut report time by {{your number}}", "top_skills": ["SQL", "Python", "Power BI"],
            "settings_to_fix": ["Turn on Open to Work for Data Analyst roles"]}


def test_profile_review(cfg, conn):
    path, before, after = profile.run(cfg, conn, FakeLLM(_profile_llm), "HEADLINE:\nAnalyst")
    assert before == 26  # 13 items x 2 points
    # Rewritten sections are capped at their max; the others keep their old score.
    rewritten_max = sum(profile.MAX_POINTS[k] for k in profile.REWRITTEN)
    assert after == rewritten_max + 2 * (13 - len(profile.REWRITTEN))
    text = path.read_text()
    assert "Score: 26/100 now" in text and "Fintech dashboards, 40+ users" in text and "Open to Work" in text
    assert text.index("## 1. Headline") < text.index("About: the first two lines")  # headline loses most
    report, _ = write_report(cfg, conn)
    assert "26/100" in report.read_text()


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, theme TEXT, text TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'draft', linkedin_urn TEXT, published_at TEXT, error TEXT)")
    old.execute("INSERT INTO posts (created_at, text) VALUES ('2026-01-01', 'old post')")
    old.commit()
    old.close()
    conn = db.connect(path)
    row = conn.execute("SELECT text, hook, human_score FROM posts").fetchone()
    assert tuple(row) == ("old post", None, None)


def test_humanize_command(tmp_path, capsys):
    draft = tmp_path / "draft.txt"
    draft.write_text("We leverage data — daily.")
    cli.main(["humanize", str(draft)])
    out, err = capsys.readouterr()
    assert out.strip() == "We use data, daily." and "2 machine artefacts removed" in err
