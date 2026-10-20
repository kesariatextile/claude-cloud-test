"""`daily`: run every step once, write the to-do digest and the report."""

from __future__ import annotations

import logging
import smtplib
from datetime import date
from email.message import EmailMessage

from . import comments, db, jobs, outreach, posts, tracker
from .config import ConfigError
from .linkedin import LinkedInClient, LinkedInError
from .llm import LLMError
from .report import digest_markdown, write_report

log = logging.getLogger("career_agent")


def _linkedin(cfg) -> LinkedInClient:
    return LinkedInClient(cfg.linkedin_token_path, version=cfg.env("LINKEDIN_API_VERSION"))


def run(cfg, conn, llm, send_email: bool | None = None):
    errors: list[str] = []

    def step(name, fn, default):
        try:
            return fn()
        except (ConfigError, LinkedInError, LLMError) as e:
            log.warning("%s: %s", name, e)
            errors.append(f"{name}: {e}")
            return default
        except Exception as e:  # one failing step must not stop the others
            log.exception("%s failed", name)
            errors.append(f"{name}: {e}")
            return default

    post_lines = step("LinkedIn post", lambda: posts.run_daily(cfg, conn, llm, lambda: _linkedin(cfg)), [])
    if not post_lines:
        post_lines = ["Post step failed, see problems below."]
    step("Outreach", lambda: outreach.draft(cfg, conn, llm, outreach.select_for_today(cfg, conn)), [])
    step("Comments", lambda: comments.run_inbox(cfg, conn, llm), [])

    scan_summary = step("Job scan", lambda: jobs.scan(cfg, conn, llm), None)
    if scan_summary:
        errors += scan_summary["errors"]
    step("Apply kits", lambda: jobs.prepare_kits(cfg, conn, llm), [])

    if cfg.env("EMAIL_ADDRESS") and cfg.env("EMAIL_APP_PASSWORD"):
        updates = step("Email tracking", lambda: tracker.sync(cfg, conn, llm), [])
    else:
        updates = ["Email tracking is off (set EMAIL_ADDRESS and EMAIL_APP_PASSWORD in .env)."]

    try:
        token_days = _linkedin(cfg).days_left()
    except LinkedInError:
        token_days = None
    report_path, data = write_report(cfg, conn, token_days)

    kits = [f"{r['company']} — {r['title']} (match {r['score']}) · apply: {r['apply_url'] or r['url']} · kit: {r['kit_path']}"
            for r in conn.execute("SELECT * FROM jobs WHERE status='kit_ready' ORDER BY score DESC")]
    comment_drafts = comments.todays_drafts(conn)
    digest = digest_markdown(date.today(), post_lines, outreach.pending_drafts(conn), comment_drafts, kits,
                             updates, scan_summary, data["attention"], errors)
    folder = cfg.output_dir / "digests"
    folder.mkdir(exist_ok=True)
    digest_path = folder / f"{db.today()}.md"
    digest_path.write_text(digest, encoding="utf-8")

    if send_email is None:
        send_email = bool(cfg.tracking.get("email_digest"))
    if send_email:
        step("Email digest", lambda: email_digest(cfg, digest, report_path), None)
    return digest_path, report_path, errors


def email_digest(cfg, digest: str, report_path) -> None:
    address = cfg.env("EMAIL_ADDRESS", required=True)
    msg = EmailMessage()
    msg["Subject"] = f"Career agent: today's plan ({date.today():%d %b})"
    msg["From"] = address
    msg["To"] = cfg.env("DIGEST_TO", address)
    msg.set_content(digest)
    msg.add_attachment(report_path.read_bytes(), maintype="text", subtype="html", filename="report.html")
    with smtplib.SMTP_SSL(cfg.tracking.get("smtp_host", "smtp.gmail.com"), 465) as smtp:
        smtp.login(address, cfg.env("EMAIL_APP_PASSWORD", required=True))
        smtp.send_message(msg)
