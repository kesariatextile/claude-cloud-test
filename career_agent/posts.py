"""Daily LinkedIn post: generate a draft, (optionally) wait for approval, publish."""

from __future__ import annotations

from datetime import date

from . import db
from .linkedin import LinkedInClient, LinkedInError, post_url
from .llm import schema_object

DEFAULT_THEMES = [
    "A lesson from real work I have done",
    "A practical tip from my field",
    "My opinion on a trend in my target industry",
    "What I am learning during my job search",
]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def pick_theme(cfg, conn) -> str:
    themes = cfg.content.get("themes") or DEFAULT_THEMES
    count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    return themes[count % len(themes)]


def generate(cfg, conn, llm, theme: str | None = None) -> int:
    theme = theme or pick_theme(cfg, conn)
    recent = [r["text"] for r in conn.execute(
        "SELECT text FROM posts WHERE status IN ('published','approved','draft') ORDER BY id DESC LIMIT 8")]
    stories = cfg.content.get("story_bank") or []
    prompt = (
        f"Write today's LinkedIn post. Theme: {theme}\n\n"
        f"Tone: {cfg.content.get('tone', 'conversational, practical, confident but humble')}\n"
        f"Language: {cfg.content.get('language', 'English')}\n"
        "Real stories/facts you may use (pick at most one, do not exaggerate it):\n"
        + "\n".join(f"- {s}" for s in stories)
        + "\n\nGuidelines:\n"
        "- 120-220 words. First line is a hook that makes people stop scrolling.\n"
        "- Short paragraphs (1-2 lines each), plain text, at most 2 emojis.\n"
        "- Show expertise in the niche; it should make a recruiter or founder in the target "
        "industry think 'this person knows their stuff'. Do not beg for a job.\n"
        "- End with a genuine question to invite comments, then 3 relevant hashtags on the last line.\n"
        "- Do not repeat the angle or hook of these recent posts:\n"
        + "\n---\n".join(recent or ["(none yet)"])
    )
    result = llm.json(prompt, schema_object({"text": {"type": "string"}}))
    cur = conn.execute(
        "INSERT INTO posts (created_at, theme, text, status) VALUES (?, ?, ?, 'draft')",
        (db.now(), theme, result["text"].strip()),
    )
    conn.commit()
    return cur.lastrowid


def set_status(conn, post_id: int, status: str) -> None:
    if conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id)).rowcount == 0:
        raise ValueError(f"No post with id {post_id}")
    conn.commit()


def publish(conn, post_id: int, client: LinkedInClient) -> str:
    row = conn.execute("SELECT text FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise LinkedInError(f"No post with id {post_id}")
    try:
        urn = client.publish(row["text"])
    except LinkedInError as e:
        conn.execute("UPDATE posts SET status='failed', error=? WHERE id=?", (str(e), post_id))
        conn.commit()
        raise
    conn.execute(
        "UPDATE posts SET status='published', linkedin_urn=?, published_at=?, error=NULL WHERE id=?",
        (urn, db.now(), post_id),
    )
    conn.commit()
    return urn


def run_daily(cfg, conn, llm, client_factory) -> list[str]:
    """Returns human-readable lines for the daily digest."""
    days = [d.lower()[:3] for d in cfg.content.get("post_days", WEEKDAYS[:5])]
    if WEEKDAYS[date.today().weekday()] not in days:
        return ["No post scheduled today."]
    if conn.execute("SELECT 1 FROM posts WHERE status='published' AND published_at >= ?", (db.today(),)).fetchone():
        return ["Already posted today."]

    require_approval = cfg.content.get("require_approval", True)
    approved = conn.execute("SELECT id FROM posts WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
    if approved:
        post_id = approved["id"]
    else:
        pending = conn.execute("SELECT id FROM posts WHERE status='draft' ORDER BY id LIMIT 1").fetchone()
        post_id = pending["id"] if pending else generate(cfg, conn, llm)
        if require_approval:
            return [f"Post draft #{post_id} is waiting for your approval: "
                    f"`python -m career_agent post approve {post_id}` (it goes live on the next run)."]

    urn = publish(conn, post_id, client_factory())
    return [f"Published post #{post_id}: {post_url(urn)}"]
