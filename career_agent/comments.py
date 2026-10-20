"""Thoughtful comment drafts for posts by HRs / founders you follow.

LinkedIn gives no API to read other people's feeds, so you paste posts you
want to engage with into inbox/posts_to_comment.md (format below) and the
agent drafts opinionated comments for each.

    URL: https://www.linkedin.com/posts/...
    Author: Priya Sharma, Founder @ Acme
    <post text>
    ---
    <next post>
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import db
from .humanize import clean
from .llm import schema_object

INBOX_NAME = "posts_to_comment.md"


def parse_inbox(text: str) -> list[dict]:
    items = []
    for block in text.split("\n---"):
        url, author, body = None, None, []
        for line in block.strip().splitlines():
            lower = line.lower()
            if lower.startswith("url:") and url is None and not body:
                url = line[4:].strip()
            elif lower.startswith("author:") and author is None and not body:
                author = line[7:].strip()
            else:
                body.append(line)
        post = "\n".join(body).strip()
        if post:
            items.append({"url": url, "author": author, "text": post})
    return items


# Comment types adapted from linkedin-agent-skill (MIT): pick by what the post is.
COMMENT_TYPES = [
    (1, "Add a datum", "the post makes a claim I can support with a number from my own work"),
    (2, "Add the missing case", "the post is right but incomplete: 'This holds until X. Then...'"),
    (3, "Respectful disagree", "I genuinely think it is wrong: name the real agreement first, then the fork"),
    (4, "Extend one line", "one sentence in the post is the good one: quote it, then build on it"),
    (5, "Ask the real question", "the post skipped the hard part: one specific question"),
    (6, "The receipt", "I have done the thing described: what happened, in two sentences"),
    (7, "The correction", "there is a factual error: be right, brief, kind and sure"),
    (8, "The reframe", "right facts, wrong frame: 'Another way to read this:'"),
    (9, "The one-liner", "the post needs nothing, I just want presence: under 12 words, funny or true"),
]
REPEAT_WINDOW_DAYS = 7


def _repeat_note(conn, author: str | None, draft_id: int) -> str:
    """Commenting on the same few people every day is visible and looks like what it is."""
    if not author:
        return ""
    since = (datetime.now() - timedelta(days=REPEAT_WINDOW_DAYS)).isoformat(timespec="seconds")
    earlier = conn.execute(
        "SELECT COUNT(*) FROM comment_drafts WHERE lower(author)=lower(?) AND created_at >= ? AND id < ?",
        (author, since, draft_id)).fetchone()[0]
    if earlier < 3:
        return ""
    return f"Already {earlier} comments for {author} this week. Spread your comments across more people."


def _row(conn, r) -> dict:
    options = [o if isinstance(o, dict) else {"type": "Comment", "text": o} for o in json.loads(r["options"])]
    return {"author": r["author"], "url": r["post_url"], "text": r["post_text"], "options": options,
            "note": _repeat_note(conn, r["author"], r["id"])}


def todays_drafts(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM comment_drafts WHERE created_at >= ? ORDER BY id", (db.today(),)).fetchall()
    return [_row(conn, r) for r in rows]


def draft(conn, llm, items: list[dict], keep_words=()) -> list[dict]:
    if not items:
        return []
    posts = "\n\n".join(
        f"<post index=\"{i}\" author=\"{p.get('author') or 'unknown'}\">\n{p['text']}\n</post>"
        for i, p in enumerate(items))
    types = "\n".join(f"{n}. {name}: {when}" for n, name, when in COMMENT_TYPES)
    prompt = (
        "For each LinkedIn post below, write 2 comment options I could post, of 2 DIFFERENT types "
        "from this list, picked by what the post actually is (never default to type 1):\n"
        f"{types}\n\n"
        "Rules: 2-4 sentences (type 9: under 12 words); one idea; say the specific thing, so the comment "
        "could not sit under any other post; never restate the post; never open with 'Great post', "
        "'Love this', 'So true', 'Couldn't agree more', 'This resonates' or the author's name and an "
        "exclamation mark; no emoji, no hashtags; never ask for a job or pitch myself. Use only experience "
        "that is in my profile; if a type needs a fact I don't have, pick another type.\n"
        "Then say which option you would post and why, in one sentence.\n\n" + posts
    )
    option = schema_object({"type_id": {"type": "integer"}, "text": {"type": "string"}})
    item = schema_object({
        "index": {"type": "integer"},
        "options": {"type": "array", "items": option},
        "recommended": {"type": "integer"},
        "reason": {"type": "string"},
    })
    result = llm.json(prompt, schema_object({"comments": {"type": "array", "items": item}}))
    names = {n: name for n, name, _ in COMMENT_TYPES}
    drafts = []
    for c in result["comments"]:
        if not 0 <= c["index"] < len(items):
            continue
        post = items[c["index"]]
        options = [{"type": names.get(o["type_id"], "Comment"), "text": clean(o["text"], keep_words)[0]}
                   for o in c["options"]]
        if 0 <= c["recommended"] < len(options):
            options[c["recommended"]]["recommended"] = c["reason"]
        cur = conn.execute(
            "INSERT INTO comment_drafts (created_at, author, post_url, post_text, options) VALUES (?, ?, ?, ?, ?)",
            (db.now(), post.get("author"), post.get("url"), post["text"], json.dumps(options, ensure_ascii=False)))
        drafts.append(_row(conn, conn.execute("SELECT * FROM comment_drafts WHERE id=?", (cur.lastrowid,)).fetchone()))
    conn.commit()
    return drafts


def run_inbox(cfg, conn, llm) -> list[dict]:
    """Draft comments for everything in the inbox file, then archive it."""
    inbox = cfg.inbox_dir / INBOX_NAME
    if not inbox.exists() or not inbox.read_text(encoding="utf-8").strip():
        return []
    text = inbox.read_text(encoding="utf-8")
    drafts = draft(conn, llm, parse_inbox(text), cfg.content.get("keep_words") or [])
    archive = cfg.inbox_dir / "processed"
    archive.mkdir(exist_ok=True)
    with open(archive / f"{db.today()}.md", "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n---\n")
    inbox.write_text("", encoding="utf-8")
    return drafts
