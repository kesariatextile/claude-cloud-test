"""HR / founder outreach: keep a contact list, pick 10-15 people a day and draft
personalised connection notes, first messages and follow-ups for you to send.

The cadence follows linkedin-agent-skill's li-dm (MIT): an invite note that asks
for nothing, a first message that gives before it asks, then exactly two
follow-ups (+4 days with something new, +10 days to close the loop), then stop."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .humanize import clean
from .llm import schema_object

STATUSES = ["new", "requested", "connected", "messaged", "replied", "not_interested"]
# Marking a contact with one of these statuses also marks the matching draft as sent.
_SENT_KIND = {"requested": ("connect",), "messaged": ("dm", "follow_up", "close_loop")}
STALE_DRAFT_DAYS = 3
MAX_DAILY = 20  # LinkedIn throttles accounts that send more invites than this
KIND_RULES = {
    "connect": "connection request note: one specific reference to them + one line on who I am + NO ask",
    "dm": "first message after they accepted: 2-4 sentences, pick up the specific thing from the invite, "
          "give something first (a sharp observation about their product, data or hiring from my field), "
          "then ONE small ask (a 15-minute chat, who to talk to about a role, or a referral if an opening fits); "
          "no calendar link, no resume dump",
    "follow_up": "first follow-up: add something genuinely new; never 'just bumping this' or 'following up'",
    "close_loop": "last message: say kindly that I won't follow up again and mean it, leave the door open",
}


def upsert_contact(conn, c: dict) -> bool:
    """Insert a contact unless the same LinkedIn URL (or name+company) exists. Returns True if new."""
    url = (c.get("linkedin_url") or "").strip() or None
    name, company = (c.get("name") or "").strip(), (c.get("company") or "").strip()
    if not name:
        return False
    if url:
        exists = conn.execute("SELECT 1 FROM contacts WHERE linkedin_url = ?", (url,)).fetchone()
    else:
        exists = conn.execute(
            "SELECT 1 FROM contacts WHERE lower(name)=lower(?) AND lower(coalesce(company,''))=lower(?)",
            (name, company)).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO contacts (name, title, company, linkedin_url, category, notes, source_url, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?)",
        (name, c.get("title"), company or None, url, c.get("category"), c.get("notes"), c.get("source_url"), db.now()),
    )
    return True


def import_csv(conn, path: Path) -> int:
    with open(path, newline="", encoding="utf-8-sig") as f:
        added = sum(upsert_contact(conn, row) for row in csv.DictReader(f))
    conn.commit()
    return added


def discover(cfg, conn, llm, count: int = 15) -> int:
    """Use Claude + web search to find HR leads / founders in your niche (public info only)."""
    niche = cfg.niche
    known = [f"{r['name']} ({r['company']})" for r in conn.execute("SELECT name, company FROM contacts")]
    prompt = (
        f"Find {count} people I could reach out to on LinkedIn about job opportunities.\n"
        f"Target roles for me: {', '.join(niche.get('target_roles', []))}\n"
        f"Industries: {', '.join(niche.get('industries', []))}\n"
        f"Locations: {', '.join(niche.get('locations', []))}\n"
        f"Who: {', '.join(cfg.outreach.get('target_people', ['HR / Talent Acquisition leads', 'Recruiters', 'Founders or co-founders of startups']))}\n"
        "Prefer companies that are hiring for my target roles right now.\n"
        "Only include people whose name, title and company you actually saw in search results, "
        "with the URL where you saw them. Fill linkedin_url only if a linkedin.com/in/ URL for "
        "that exact person appeared in results; otherwise use an empty string. In notes, write one "
        "specific, recent thing about them or their company that I could mention.\n"
        "Skip these people, I already have them: " + ("; ".join(known) or "(none)")
    )
    person = schema_object({
        "name": {"type": "string"}, "title": {"type": "string"}, "company": {"type": "string"},
        "category": {"type": "string", "enum": ["HR", "Recruiter", "Founder", "Hiring Manager"]},
        "linkedin_url": {"type": "string"}, "source_url": {"type": "string"}, "notes": {"type": "string"},
    })
    result = llm.search_and_save(prompt, schema_object({"people": {"type": "array", "items": person}}))
    added = sum(upsert_contact(conn, p) for p in result["people"])
    conn.commit()
    return added


def _drop_stale_drafts(conn) -> None:
    cutoff = (datetime.now() - timedelta(days=STALE_DRAFT_DAYS)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM outreach_messages WHERE sent_at IS NULL AND created_at < ?", (cutoff,))


def select_for_today(cfg, conn, limit: int | None = None) -> list[tuple[dict, str]]:
    """Pick who to contact today and how: first messages to new connections,
    then follow-ups, then fresh connection requests."""
    limit = min(limit or int(cfg.outreach.get("daily_limit", 12)), MAX_DAILY)
    follow_up_days = int(cfg.outreach.get("follow_up_after_days", 4))
    close_loop_days = int(cfg.outreach.get("close_loop_after_days", 6))
    _drop_stale_drafts(conn)
    # Unsent drafts from earlier days still count toward today's limit.
    limit -= conn.execute("SELECT COUNT(*) FROM outreach_messages WHERE sent_at IS NULL").fetchone()[0]

    def ago(days):
        return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    no_pending = "NOT EXISTS (SELECT 1 FROM outreach_messages m WHERE m.contact_id=c.id AND m.sent_at IS NULL)"
    sent = "(SELECT COUNT(*) FROM outreach_messages m WHERE m.contact_id=c.id AND m.sent_at IS NOT NULL AND m.kind='{}')"
    queries = [
        ("dm", "status='connected'", ()),
        ("follow_up", f"status='messaged' AND last_action_at < ? AND {sent.format('follow_up')} = 0",
         (ago(follow_up_days),)),
        ("close_loop", f"status='messaged' AND last_action_at < ? AND {sent.format('follow_up')} = 1 "
                       f"AND {sent.format('close_loop')} = 0", (ago(close_loop_days),)),
        ("connect", "status='new'", ()),
    ]
    picked: list[tuple[dict, str]] = []
    for kind, where, params in queries:
        sql = f"SELECT * FROM contacts c WHERE {where} AND {no_pending} ORDER BY coalesce(last_action_at, ''), id"
        for row in conn.execute(sql, params):
            if len(picked) >= limit:
                return picked
            picked.append((dict(row), kind))
    return picked


def draft(cfg, conn, llm, picked: list[tuple[dict, str]]) -> list[dict]:
    if not picked:
        return []
    note_limit = 300 if cfg.outreach.get("is_premium") else 200
    limits = {"connect": note_limit, "dm": 500, "follow_up": 350, "close_loop": 300}
    lines = []
    for contact, kind in picked:
        previous = [r["text"] for r in conn.execute(
            "SELECT text FROM outreach_messages WHERE contact_id=? AND sent_at IS NOT NULL ORDER BY id", (contact["id"],))]
        lines.append(
            f"- contact_id={contact['id']} | type={kind} | max {limits[kind]} characters | "
            f"{contact['name']}, {contact.get('title') or ''} at {contact.get('company') or ''} "
            f"({contact.get('category') or ''}). Notes: {contact.get('notes') or 'none'}. "
            f"Already sent: {' / '.join(previous) or 'nothing'}"
        )
    prompt = (
        "Write one LinkedIn message per contact below.\n"
        + "".join(f"- {kind}: {rule}\n" for kind, rule in KIND_RULES.items())
        + f"My goal: {cfg.outreach.get('goal', 'get referred or considered for roles in my niche')}.\n"
        "Rules: first name only; never fabricate a mutual connection, a shared school, a meeting, or having "
        "read something that is not in the notes; never open with 'I hope this message finds you well'; "
        "stay within the character limit. Set specific=true only if the notes give a real, current reason "
        "to contact this person (a post, a launch, an opening, a mutual interest); otherwise false.\n\n"
        + "\n".join(lines)
    )
    msg = schema_object({"contact_id": {"type": "integer"}, "text": {"type": "string"},
                         "specific": {"type": "boolean"}})
    result = llm.json(prompt, schema_object({"messages": {"type": "array", "items": msg}}))
    kinds = {c["id"]: (c, k) for c, k in picked}
    keep = cfg.content.get("keep_words") or []
    drafts = []
    for m in result["messages"]:
        if m["contact_id"] not in kinds:
            continue
        contact, kind = kinds.pop(m["contact_id"])
        text = _fit(clean(m["text"], keep)[0], limits[kind])
        conn.execute(
            "INSERT INTO outreach_messages (contact_id, kind, text, created_at, specific) VALUES (?, ?, ?, ?, ?)",
            (contact["id"], kind, text, db.now(), int(m["specific"])))
        drafts.append({"contact": contact, "kind": kind, "text": text, "specific": m["specific"]})
    conn.commit()
    return drafts


def _fit(text: str, limit: int) -> str:
    """Trim to LinkedIn's limit at a sentence (or word) boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: end + 1] if end > limit // 2 else cut[: cut.rfind(" ")].rstrip(",;:")


def mark(conn, contact_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if conn.execute("SELECT 1 FROM contacts WHERE id=?", (contact_id,)).fetchone() is None:
        raise ValueError(f"No contact with id {contact_id}")
    ts = db.now()
    conn.execute("UPDATE contacts SET status=?, last_action_at=? WHERE id=?", (status, ts, contact_id))
    kinds = _SENT_KIND.get(status)
    if kinds:
        conn.execute(
            f"""UPDATE outreach_messages SET sent_at=? WHERE id = (
                SELECT id FROM outreach_messages WHERE contact_id=? AND sent_at IS NULL
                AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1)""",
            (ts, contact_id, *kinds))
    conn.commit()


def pending_drafts(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT m.id, m.kind, m.text, m.specific, c.id AS contact_id, c.name, c.title, c.company, c.linkedin_url
           FROM outreach_messages m JOIN contacts c ON c.id = m.contact_id
           WHERE m.sent_at IS NULL ORDER BY m.id""")
    return [dict(r) for r in rows]
