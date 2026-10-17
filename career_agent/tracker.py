"""Application tracking from your inbox.

Reads recent emails over IMAP (Gmail: use an App Password), keeps only the ones
that look job-related, and lets Claude classify them (application received,
assessment, interview, rejection, offer, recruiter reaching out) and match them
to the jobs in the tracker. Applications you made elsewhere (Naukri, LinkedIn
Easy Apply, referrals) are picked up from their confirmation emails too.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from . import db
from .jobs import Job, html_to_text, update_status, upsert
from .llm import schema_object

KEYWORDS = re.compile(
    r"applica|applied|apply|candida|interview|assessment|assignment|hackerrank|codility|"
    r"shortlist|offer|recruit|hiring|talent|position|opening|role\b|unfortunately|regret|"
    r"not be moving|next steps|greenhouse|lever\.co|ashby|workday|naukri|instahyre|wellfound|"
    r"jobs-noreply|careers|job",
    re.IGNORECASE,
)
CATEGORY_STATUS = {
    "application_received": "applied",
    "assessment": "assessment",
    "interview_invite": "interview",
    "rejection": "rejected",
    "offer": "offer",
    "recruiter_outreach": "lead",
}
MAX_BODY_CHARS = 3000  # an email's opening is enough to classify it


@dataclass
class Mail:
    message_id: str
    sender: str
    subject: str
    date: str
    body: str


def _decode(value: str | None) -> str:
    return str(make_header(decode_header(value))) if value else ""


def _body(msg: email.message.Message) -> str:
    plain, html_part = None, None
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_subtype() == "plain" and plain is None:
            plain = text
        elif part.get_content_subtype() == "html" and html_part is None:
            html_part = html_to_text(text)
    return (plain or html_part or "").strip()


def parse_message(raw: bytes) -> Mail:
    msg = email.message_from_bytes(raw)
    try:
        sent = parsedate_to_datetime(msg["Date"]).date().isoformat()
    except (TypeError, ValueError):
        sent = ""
    return Mail(
        message_id=(msg["Message-ID"] or "").strip() or f"{msg['Date']}|{msg['Subject']}",
        sender=_decode(msg["From"]),
        subject=_decode(msg["Subject"]),
        date=sent,
        body=_body(msg),
    )


def looks_job_related(mail: Mail) -> bool:
    return bool(KEYWORDS.search(mail.subject) or KEYWORDS.search(parseaddr(mail.sender)[1])
                or KEYWORDS.search(mail.body[:1500]))


def fetch_mail(host: str, user: str, password: str, since_days: int, max_messages: int = 400) -> list[Mail]:
    since = (date.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(host) as imap:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, f'(SINCE "{since}")')
        ids = data[0].split()[-max_messages:]
        mails = []
        for num in ids:
            _, parts = imap.fetch(num, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw:
                mails.append(parse_message(raw))
    return mails


def classify(conn, llm, mails: list[Mail]) -> list[dict]:
    tracked = conn.execute(
        "SELECT id, company, title, status FROM jobs WHERE status NOT IN ('new','skipped') ORDER BY id DESC LIMIT 300")
    jobs_list = "\n".join(f"{r['id']}: {r['title']} @ {r['company']} [{r['status']}]" for r in tracked) or "(none)"
    mails_text = "\n\n".join(
        f"<email index=\"{i}\">\nFrom: {m.sender}\nDate: {m.date}\nSubject: {m.subject}\n\n{m.body[:MAX_BODY_CHARS]}\n</email>"
        for i, m in enumerate(mails))
    item = schema_object({
        "index": {"type": "integer"},
        "category": {"type": "string", "enum": [*CATEGORY_STATUS, "other"]},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "job_id": {"type": "integer"},
        "summary": {"type": "string"},
    })
    result = llm.json(
        "Classify each email about MY job search.\n"
        "- application_received: confirms an application I submitted\n"
        "- assessment: online test / take-home assignment\n"
        "- interview_invite: asks to schedule or confirms an interview / call\n"
        "- rejection: not moving forward\n- offer: job offer\n"
        "- recruiter_outreach: a recruiter/company contacting me about a role I did not apply to\n"
        "- other: job alerts, newsletters, marketing, anything else\n"
        "If an email is about one of my tracked applications below, set job_id to its id, "
        "otherwise 0. summary: one short line with the key detail (date/time, next step).\n\n"
        f"Tracked applications:\n{jobs_list}\n\n{mails_text}",
        schema_object({"emails": {"type": "array", "items": item}}), effort="low")
    return [r for r in result["emails"] if 0 <= r["index"] < len(mails)]


def apply_results(conn, mails: list[Mail], results: list[dict]) -> list[str]:
    valid_ids = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
    updates = []
    for r in results:
        mail = mails[r["index"]]
        status = CATEGORY_STATUS.get(r["category"])
        job_id = r["job_id"] if r["job_id"] in valid_ids else None
        if status and job_id is None and r["company"]:
            # An application (or recruiter lead) the tracker didn't know about yet.
            # Inserted as 'skipped' (never scored) so the update below moves it forward.
            job_id = upsert(conn, Job(source="email", external_id=mail.message_id, company=r["company"],
                                      title=r["role"] or "(role not mentioned)"), status="skipped")
        if status and job_id:
            detail = f"{r['summary']} (email {mail.date}: \"{mail.subject}\")"
            if update_status(conn, job_id, status, detail, origin="email"):
                updates.append(f"{r['company']} — {r['role']}: {status.replace('_', ' ')}. {r['summary']}")
        conn.execute("INSERT OR IGNORE INTO emails_seen (message_id, processed_at, category, job_id) VALUES (?,?,?,?)",
                     (mail.message_id, db.now(), r["category"], job_id))
    conn.commit()
    return updates


def sync(cfg, conn, llm, mails: list[Mail] | None = None, batch_size: int = 10) -> list[str]:
    """Fetch new mail, classify job-related messages, update the tracker."""
    if mails is None:
        t = cfg.tracking
        mails = fetch_mail(
            t.get("imap_host", "imap.gmail.com"),
            cfg.env("EMAIL_ADDRESS", required=True),
            cfg.env("EMAIL_APP_PASSWORD", required=True),
            int(t.get("lookback_days", 14)),
        )
    seen = {r["message_id"] for r in conn.execute("SELECT message_id FROM emails_seen")}
    fresh = [m for m in mails if m.message_id not in seen]
    for m in fresh:
        if not looks_job_related(m):
            conn.execute("INSERT OR IGNORE INTO emails_seen (message_id, processed_at, category) VALUES (?,?,'skipped')",
                         (m.message_id, db.now()))
    conn.commit()
    relevant = [m for m in fresh if looks_job_related(m)]
    updates = []
    for start in range(0, len(relevant), batch_size):
        batch = relevant[start:start + batch_size]
        updates += apply_results(conn, batch, classify(conn, llm, batch))
    return updates
