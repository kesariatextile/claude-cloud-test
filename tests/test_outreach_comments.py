import re
from datetime import datetime, timedelta

from career_agent import comments, outreach
from tests.conftest import FakeLLM


def _messages(prompt, schema):
    ids = [int(i) for i in re.findall(r"contact_id=(\d+)", prompt)]
    return {"messages": [{"contact_id": i, "text": f"Hi {i}, " + "word " * 80, "specific": i % 2 == 1} for i in ids]}


def _add(conn, n):
    for i in range(n):
        outreach.upsert_contact(conn, {"name": f"Person {i}", "company": f"Co{i}",
                                       "linkedin_url": f"https://www.linkedin.com/in/p{i}"})
    conn.commit()


def test_import_csv_dedupes(tmp_path, conn):
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("name,title,company,linkedin_url,category,notes\n"
                        "Priya,Founder,Acme,https://www.linkedin.com/in/priya,Founder,raised\n"
                        "Priya,Founder,Acme,https://www.linkedin.com/in/priya,Founder,dup\n"
                        "Rahul,HR,Beta,,HR,\n,,,,,\n")
    assert outreach.import_csv(conn, csv_path) == 2
    assert outreach.import_csv(conn, csv_path) == 0


def test_daily_selection_priorities_and_limit(cfg, conn):
    _add(conn, 6)
    old = (datetime.now() - timedelta(days=10)).isoformat(timespec="seconds")
    conn.execute("UPDATE contacts SET status='connected' WHERE id=1")
    conn.execute("UPDATE contacts SET status='messaged', last_action_at=? WHERE id=2", (old,))
    conn.execute("UPDATE contacts SET status='messaged', last_action_at=? WHERE id=3", (datetime.now().isoformat(),))
    picked = outreach.select_for_today(cfg, conn)  # daily_limit is 3
    assert [(c["id"], k) for c, k in picked] == [(1, "dm"), (2, "follow_up"), (4, "connect")]


def test_drafts_respect_limit_and_pending_drafts_count(cfg, conn):
    _add(conn, 5)
    drafts = outreach.draft(cfg, conn, FakeLLM(_messages), outreach.select_for_today(cfg, conn))
    assert len(drafts) == 3
    assert all(len(d["text"]) <= 200 for d in drafts)  # free-account connection note limit
    # Nothing new is drafted while yesterday's three are still unsent.
    assert outreach.select_for_today(cfg, conn) == []
    outreach.mark(conn, 1, "requested")
    assert conn.execute("SELECT sent_at FROM outreach_messages WHERE contact_id=1").fetchone()[0]
    assert [c["id"] for c, _ in outreach.select_for_today(cfg, conn)] == [4]


def test_stale_drafts_are_dropped(cfg, conn):
    _add(conn, 1)
    old = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO outreach_messages (contact_id, kind, text, created_at) VALUES (1,'connect','x',?)", (old,))
    assert [c["id"] for c, _ in outreach.select_for_today(cfg, conn)] == [1]


def test_fit_cuts_at_sentence():
    text = "First sentence here. Second one is longer and goes past the limit for sure."
    assert outreach._fit(text, 30) == "First sentence here."
    assert outreach._fit(text, 200) == text
    assert outreach._fit("one two three four five six", 12) == "one two"


def test_discover_saves_people(cfg, conn):
    people = {"people": [{"name": "Neha", "title": "TA Lead", "company": "Acme", "category": "HR",
                          "linkedin_url": "", "source_url": "https://acme.example/team", "notes": "hiring"}]}
    assert outreach.discover(cfg, conn, FakeLLM(lambda p, s: people)) == 1
    assert outreach.discover(cfg, conn, FakeLLM(lambda p, s: people)) == 0


INBOX = """URL: https://www.linkedin.com/posts/a
Author: Priya, Founder
Hiring is broken.
URL: this line is body text
---
Second post without metadata
---
"""


def test_parse_inbox():
    items = comments.parse_inbox(INBOX)
    assert items[0] == {"url": "https://www.linkedin.com/posts/a", "author": "Priya, Founder",
                        "text": "Hiring is broken.\nURL: this line is body text"}
    assert items[1] == {"url": None, "author": None, "text": "Second post without metadata"}
    assert len(items) == 2


def test_run_inbox_drafts_and_archives(cfg, conn):
    (cfg.inbox_dir / comments.INBOX_NAME).write_text(INBOX)
    llm = FakeLLM(lambda p, s: {"comments": [
        {"index": 0, "options": [{"type_id": 6, "text": "A"}, {"type_id": 3, "text": "B"}],
         "recommended": 0, "reason": "own data"},
        {"index": 9, "options": [{"type_id": 1, "text": "x"}], "recommended": 0, "reason": ""}]})
    drafts = comments.run_inbox(cfg, conn, llm)
    assert drafts[0]["options"] == [{"type": "The receipt", "text": "A", "recommended": "own data"},
                                    {"type": "Respectful disagree", "text": "B"}]
    assert len(drafts) == 1
    assert (cfg.inbox_dir / comments.INBOX_NAME).read_text() == ""
    assert "Hiring is broken" in next((cfg.inbox_dir / "processed").iterdir()).read_text()
    assert comments.run_inbox(cfg, conn, llm) == []
