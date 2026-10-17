"""HR / founder outreach: keep a contact list, pick 10-15 people a day and draft
personalised connection notes, first messages and follow-ups for you to send."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .llm import schema_object

STATUSES = ["new", "requested", "connected", "messaged", "replied", "not_interested"]
# Marking a contact with one of these statuses also marks the matching draft as sent.
_SENT_KIND = {"requested": ("connect",), "messaged": ("dm", "follow_up")}
STALE_DRAFT_DAYS = 3


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
    """Pick who to contact today and how: first DMs to new connections, then
    follow-ups, then fresh connection requests."""
    limit = limit or int(cfg.outreach.get("daily_limit", 12))
    follow_up_days = int(cfg.outreach.get("follow_up_after_days", 5))
    _drop_stale_drafts(conn)
    # Unsent drafts from earlier days still count toward today's limit.
    limit -= conn.execute("SELECT COUNT(*) FROM outreach_messages WHERE sent_at IS NULL").fetchone()[0]
    cutoff = (datetime.now() - timedelta(days=follow_up_days)).isoformat(timespec="seconds")
    no_pending = "AND NOT EXISTS (SELECT 1 FROM outreach_messages m WHERE m.contact_id=c.id AND m.sent_at IS NULL)"
    queries = [
        ("dm", f"SELECT * FROM contacts c WHERE status='connected' {no_pending} ORDER BY last_action_at"),
        ("follow_up", f"""SELECT * FROM contacts c WHERE status='messaged' AND last_action_at < ? {no_pending}
             AND (SELECT COUNT(*) FROM outreach_messages m WHERE m.contact_id=c.id AND m.kind='follow_up') = 0
             ORDER BY last_action_at""", ),
        ("connect", f"SELECT * FROM contacts c WHERE status='new' {no_pending} ORDER BY id"),
    ]
    picked: list[tuple[dict, str]] = []
    for kind, sql in queries:
        params = (cutoff,) if kind == "follow_up" else ()
        for row in conn.execute(sql, params):
            if len(picked) >= limit:
                return picked
            picked.append((dict(row), kind))
    return picked


def draft(cfg, conn, llm, picked: list[tuple[dict, str]]) -> list[dict]:
    if not picked:
        return []
    note_limit = 300 if cfg.outreach.get("is_premium") else 200
    limits = {"connect": note_limit, "dm": 600, "follow_up": 350}
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
        "Types: connect = connection request note; dm = first message after they accepted; "
        "follow_up = polite nudge after no reply (add something new, don't guilt-trip).\n"
        f"My goal: {cfg.outreach.get('goal', 'get referred or considered for roles in my niche')}.\n"
        "Rules: address them by first name; mention something specific about them or their "
        "company when the notes give you something; one clear, low-effort ask (e.g. 'open to a "
        "10-minute chat?' or 'could you point me to the right person?'); never paste a whole "
        "resume; never claim we met; stay within the character limit.\n\n" + "\n".join(lines)
    )
    msg = schema_object({"contact_id": {"type": "integer"}, "text": {"type": "string"}})
    result = llm.json(prompt, schema_object({"messages": {"type": "array", "items": msg}}))
    kinds = {c["id"]: (c, k) for c, k in picked}
    drafts = []
    for m in result["messages"]:
        if m["contact_id"] not in kinds:
            continue
        contact, kind = kinds[m["contact_id"]]
        text = _fit(m["text"].strip(), limits[kind])
        conn.execute(
            "INSERT INTO outreach_messages (contact_id, kind, text, created_at) VALUES (?, ?, ?, ?)",
            (contact["id"], kind, text, db.now()))
        drafts.append({"contact": contact, "kind": kind, "text": text})
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
        """SELECT m.id, m.kind, m.text, c.id AS contact_id, c.name, c.title, c.company, c.linkedin_url
           FROM outreach_messages m JOIN contacts c ON c.id = m.contact_id
           WHERE m.sent_at IS NULL ORDER BY m.id""")
    return [dict(r) for r in rows]
