import json
import re
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from career_agent import daily, jobs, report, tracker
from career_agent.jobs import Job
from career_agent.llm import LLM, LLMError
from tests.conftest import FakeLLM


def _raw(subject, body, sender="no-reply@greenhouse.io", html=False, msg_id="<1@x>"):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "me@example.com", subject
    msg["Date"], msg["Message-ID"] = "Fri, 16 Oct 2026 10:00:00 +0530", msg_id
    if html:
        msg.set_content("fallback")
        msg.add_alternative(f"<html><body><p>{body}</p></body></html>", subtype="html")
        msg.get_payload().pop(0)  # html only
    else:
        msg.set_content(body)
    return msg.as_bytes()


def test_parse_message_reads_plain_and_html():
    m = tracker.parse_message(_raw("Thanks for applying", "We received your application"))
    assert m.subject == "Thanks for applying" and "received your application" in m.body and m.date == "2026-10-16"
    h = tracker.parse_message(_raw("Interview", "Let's schedule a call", html=True))
    assert h.body == "Let's schedule a call"


def test_job_related_filter():
    assert tracker.looks_job_related(tracker.parse_message(_raw("Your application to Acme", "x")))
    assert not tracker.looks_job_related(tracker.parse_message(_raw("50% off shoes", "Sale!", sender="deals@shop.com")))


def test_sync_updates_known_and_creates_unknown_applications(cfg, conn):
    known = jobs.upsert(conn, Job("greenhouse", "1", "Acme", "Data Analyst"), status="kit_ready")
    conn.commit()
    mails = [tracker.parse_message(_raw(s, b, msg_id=f"<{i}@x>")) for i, (s, b) in enumerate([
        ("Interview invitation - Data Analyst", "Please pick a slot"),
        ("Application received - Product Analyst", "Thanks for applying to Beta"),
        ("Big sale today", "nothing about work", ),
    ])]
    mails[2].sender = "deals@shop.com"

    def handler(prompt, schema):
        assert "1: Data Analyst @ Acme [kit_ready]" in prompt and "Big sale" not in prompt
        return {"emails": [
            {"index": 0, "category": "interview_invite", "company": "Acme", "role": "Data Analyst",
             "job_id": known, "summary": "Pick a slot this week"},
            {"index": 1, "category": "application_received", "company": "Beta", "role": "Product Analyst",
             "job_id": 0, "summary": "Applied"},
        ]}

    updates = tracker.sync(cfg, conn, FakeLLM(handler), mails=mails)
    assert len(updates) == 2
    rows = {r["company"]: r["status"] for r in conn.execute("SELECT company, status FROM jobs")}
    assert rows == {"Acme": "interview", "Beta": "applied"}
    assert conn.execute("SELECT COUNT(*) FROM emails_seen").fetchone()[0] == 3
    # Already-processed emails are not sent to Claude again.
    llm = FakeLLM(handler)
    assert tracker.sync(cfg, conn, llm, mails=mails) == [] and llm.prompts == []


def _seed(conn):
    a = jobs.upsert(conn, Job("lever", "a", "Acme", "Data Analyst", apply_url="https://apply/a"), status="shortlisted")
    b = jobs.upsert(conn, Job("lever", "b", "Beta", "BI Analyst"), status="shortlisted")
    c = jobs.upsert(conn, Job("lever", "c", "Gamma", "Analyst <script>", apply_url="javascript:alert(1)"),
                    status="shortlisted")
    jobs.update_status(conn, a, "applied")
    jobs.update_status(conn, a, "interview", "Round 1 on Monday", origin="email")
    jobs.update_status(conn, b, "applied")
    conn.execute("UPDATE jobs SET applied_at='2026-01-01T10:00:00', last_update_at='2026-01-01T10:00:00' WHERE id=?", (b,))
    jobs.update_status(conn, c, "kit_ready", origin="agent")
    conn.commit()


def test_report_numbers_and_escaping(cfg, conn):
    _seed(conn)
    path, data = report.write_report(cfg, conn, token_days_left=3)
    assert len(data["applications"]) == 2 and data["interviews"] == 1 and data["replied"] == 1
    assert dict(data["pipeline"])["Interview"] == 1
    html = path.read_text()
    assert "Analyst &lt;script&gt;" in html and "<script>" not in html.split("<main>")[1].split("</main>")[0]
    assert "no reply for" in html and "LinkedIn connection expires in 3 days" in html
    assert 'href="https://apply/a"' in html and "javascript:" not in html


def test_daily_run_survives_failures(cfg, conn):
    _seed(conn)
    (cfg.inbox_dir / "posts_to_comment.md").write_text("Some post")

    def handler(prompt, schema):
        if "2 alternative comments" in prompt:
            return {"comments": [{"index": 0, "options": ["Opinion"]}]}
        if "today's LinkedIn post" in prompt:
            return {"text": "Draft post"}
        raise AssertionError(prompt[:80])

    digest_path, report_path, errors = daily.run(cfg, conn, FakeLLM(handler), send_email=False)
    text = digest_path.read_text()
    assert "waiting for your approval" in text and "1. Opinion" in text
    assert "Gamma — Analyst <script>" in text and "Email tracking is off" in text
    assert errors == [] and report_path.exists()


class _Client:
    def __init__(self, response):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.response = response

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_llm_request_shape(cfg):
    client = _Client(SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="thinking", text=None), SimpleNamespace(type="text", text='{"a": 1}')]))
    llm = LLM(cfg, client=client)
    assert llm.json("hi", {"type": "object"}, effort="low") == {"a": 1}
    kw = client.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and "Asha Rao" in kw["system"][0]["text"]


def test_llm_refusal_raises(cfg):
    llm = LLM(cfg, client=_Client(SimpleNamespace(stop_reason="refusal", content=[])))
    with pytest.raises(LLMError):
        llm.json("hi", {"type": "object"})
