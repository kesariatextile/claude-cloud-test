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

from . import db
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


def draft(conn, llm, items: list[dict]) -> list[dict]:
    if not items:
        return []
    posts = "\n\n".join(
        f"<post index=\"{i}\" author=\"{p.get('author') or 'unknown'}\">\n{p['text']}\n</post>"
        for i, p in enumerate(items))
    prompt = (
        "For each LinkedIn post below, write 2 alternative comments I could post.\n"
        "Each comment: 25-70 words; take a clear position (agree and extend, respectfully "
        "push back, or add a nuance); add something concrete from my own experience or skills "
        "where it genuinely fits; optionally end with a short question to the author. "
        "No 'Great post!', no flattery, no hashtags, no asking for a job, no emojis.\n"
        "The two options should take different angles.\n\n" + posts
    )
    item = schema_object({
        "index": {"type": "integer"},
        "options": {"type": "array", "items": {"type": "string"}},
    })
    result = llm.json(prompt, schema_object({"comments": {"type": "array", "items": item}}))
    drafts = []
    for c in result["comments"]:
        if not 0 <= c["index"] < len(items):
            continue
        post = items[c["index"]]
        conn.execute(
            "INSERT INTO comment_drafts (created_at, author, post_url, post_text, options) VALUES (?, ?, ?, ?, ?)",
            (db.now(), post.get("author"), post.get("url"), post["text"], json.dumps(c["options"], ensure_ascii=False)))
        drafts.append({**post, "options": c["options"]})
    conn.commit()
    return drafts


def run_inbox(cfg, conn, llm) -> list[dict]:
    """Draft comments for everything in the inbox file, then archive it."""
    inbox = cfg.inbox_dir / INBOX_NAME
    if not inbox.exists() or not inbox.read_text(encoding="utf-8").strip():
        return []
    text = inbox.read_text(encoding="utf-8")
    drafts = draft(conn, llm, parse_inbox(text))
    archive = cfg.inbox_dir / "processed"
    archive.mkdir(exist_ok=True)
    with open(archive / f"{db.today()}.md", "a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n---\n")
    inbox.write_text("", encoding="utf-8")
    return drafts
