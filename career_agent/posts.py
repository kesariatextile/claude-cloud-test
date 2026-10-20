"""Daily LinkedIn post: generate a draft, (optionally) wait for approval, publish.

Drafts are built on one of 21 hook formulas (data/hooks.json, adapted from
linkedin-agent-skill, MIT) and run through the humanizer before you see them.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from . import db
from .humanize import polish
from .linkedin import LinkedInClient, LinkedInError, post_url
from .llm import schema_object

HOOKS = json.loads((Path(__file__).parent / "data" / "hooks.json").read_text(encoding="utf-8"))
# A weekly mix that shows a recruiter both skill and judgement.
DEFAULT_THEMES = [
    "Proof: something real that happened at work, with a number",
    "Opinion: a position on my field that some people in it would disagree with",
    "Teach: one thing the reader can do today, from my own toolkit",
    "Story: a moment from my work with a real cost or lesson",
]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")


def pick_theme(cfg, conn) -> str:
    themes = cfg.content.get("themes") or DEFAULT_THEMES
    count = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    return themes[count % len(themes)]


def _hook_menu() -> str:
    return "\n".join(
        f"#{h['id']} {h['name']}: {h['template']} | e.g. \"{h['example']}\" | best for: {h['best_for']} | trap: {h['trap']}"
        for h in HOOKS["hooks"])


def generate(cfg, conn, llm, theme: str | None = None) -> int:
    theme = theme or pick_theme(cfg, conn)
    recent = conn.execute(
        "SELECT text, hook FROM posts WHERE status IN ('published','approved','draft') ORDER BY id DESC LIMIT 8").fetchall()
    stories = cfg.content.get("story_bank") or []
    prompt = (
        f"Write today's LinkedIn post. Theme: {theme}\n"
        f"Tone: {cfg.content.get('tone', 'conversational, practical, confident but humble')}\n"
        f"Language: {cfg.content.get('language', 'English')}\n\n"
        "Real stories/facts you may use (pick one, do not exaggerate it):\n"
        + "\n".join(f"- {s}" for s in stories)
        + "\n\nHook formulas (line 1 is the whole game; mobile truncates at ~140 characters):\n"
        + _hook_menu() + "\nHook rules: " + " ".join(HOOKS["rules"])
        + "\n\nSteps:\n"
        "1. Pick the 3 formulas that genuinely fit this theme and my material (3 different formulas) "
        "and write a line 1 for each.\n"
        "2. Choose the strongest and say why in one sentence.\n"
        "3. Write the full post on it:\n"
        "   line 1 the hook, alone; line 2 pays it off (not setup); body in 1-3 line paragraphs with a "
        "blank line between each; one line that reframes; close with ONE specific question only this "
        "post could ask, or one instruction. 900-1,300 characters. Up to 3 real hashtags on the last line. "
        "No links in the body.\n"
        "I am job hunting: show skill and judgement a hiring manager in my target industry would "
        "notice; never badmouth an employer or colleague; never sound desperate or ask for a job.\n"
        "If a number would make it stronger and I have not given one, write {{your number}} instead "
        "of inventing it.\n"
        "Do not reuse a hook formula or angle from my recent posts:\n"
        + "\n---\n".join(f"[hook {r['hook'] or '?'}] {r['text'][:300]}" for r in recent)
    )
    hook = schema_object({"id": {"type": "integer"}, "line": {"type": "string"}})
    result = llm.json(prompt, schema_object({
        "hooks": {"type": "array", "items": hook},
        "chosen_id": {"type": "integer"},
        "reason": {"type": "string"},
        "text": {"type": "string"},
    }))
    polished = polish(result["text"], llm, "post", cfg.content.get("keep_words") or [])
    names = {h["id"]: h["name"] for h in HOOKS["hooks"]}
    chosen = f"#{result['chosen_id']} {names.get(result['chosen_id'], '')}".strip()
    options = {"hooks": [{**h, "name": names.get(h["id"], "")} for h in result["hooks"]],
               "reason": result["reason"], "removed": polished.removed,
               "tells": [t["name"] for t in polished.tells], "rewritten": polished.rewritten}
    s = polished.score
    cur = conn.execute(
        """INSERT INTO posts (created_at, theme, text, status, hook, hook_options, human_score, verdict)
           VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)""",
        (db.now(), theme, polished.text, chosen, json.dumps(options, ensure_ascii=False),
         s["score"] if s else None, s["verdict"] if s else None))
    conn.commit()
    return cur.lastrowid


def summary(row) -> str:
    """One line about a draft: hook, length, humanizer result, open placeholders."""
    parts = [f"hook {row['hook'] or '-'}", f"{len(row['text'])} characters"]
    if row["human_score"] is not None:
        parts.append(f"human score {row['human_score']:.0f} {row['verdict']}")
    holes = PLACEHOLDER.findall(row["text"])
    if holes:
        parts.append(f"fill in first: {', '.join(holes)}")
    return " · ".join(parts)


def set_status(conn, post_id: int, status: str) -> None:
    row = conn.execute("SELECT text FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise ValueError(f"No post with id {post_id}")
    if status == "approved" and PLACEHOLDER.search(row["text"]):
        raise ValueError(f"Post #{post_id} still has {PLACEHOLDER.findall(row['text'])}. "
                         f"Fill them in with `post edit {post_id} --file ...` first.")
    conn.execute("UPDATE posts SET status = ? WHERE id = ?", (status, post_id))
    conn.commit()


def publish(conn, post_id: int, client: LinkedInClient) -> str:
    row = conn.execute("SELECT text FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise LinkedInError(f"No post with id {post_id}")
    if PLACEHOLDER.search(row["text"]):
        raise LinkedInError(f"Post #{post_id} still has {PLACEHOLDER.findall(row['text'])}; not publishing.")
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

    approved = conn.execute("SELECT id FROM posts WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
    if approved:
        post_id = approved["id"]
    else:
        pending = conn.execute("SELECT id FROM posts WHERE status='draft' ORDER BY id LIMIT 1").fetchone()
        post_id = pending["id"] if pending else generate(cfg, conn, llm)
        row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if cfg.content.get("require_approval", True) or PLACEHOLDER.search(row["text"]):
            return [f"Post draft #{post_id} is waiting for you ({summary(row)}). Read it with "
                    f"`python -m career_agent post show {post_id}`, then `post approve {post_id}` "
                    "(it goes live on the next run)."]

    urn = publish(conn, post_id, client_factory())
    return [f"Published post #{post_id}: {post_url(urn)}"]
