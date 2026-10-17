import json
from datetime import date, timedelta

import httpx2 as httpx
import pytest

from career_agent import jobs
from career_agent.jobs import Job
from tests.conftest import FakeLLM

RECENT = (date.today() - timedelta(days=2)).isoformat()


def _http(routes):
    def handler(request):
        for prefix, response in routes.items():
            if str(request.url).startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


GREENHOUSE = {"jobs": [{
    "id": 101, "title": "Senior Data Analyst", "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
    "location": {"name": "Bengaluru"}, "updated_at": RECENT + "T10:00:00-04:00",
    "content": "&lt;p&gt;Own &lt;strong&gt;SQL&lt;/strong&gt; dashboards&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
}, {
    "id": 102, "title": "Backend Engineer", "absolute_url": "https://x", "location": {"name": "Bengaluru"},
    "updated_at": RECENT + "T10:00:00Z", "content": "",
}]}
LEVER = [{
    "id": "lev-1", "text": "Product Analyst", "hostedUrl": "https://jobs.lever.co/beta/lev-1",
    "applyUrl": "https://jobs.lever.co/beta/lev-1/apply", "categories": {"location": "Remote - India"},
    "descriptionPlain": "Analyse funnels.", "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
    "additionalPlain": "", "createdAt": 1_760_000_000_000,
}]
ASHBY = {"jobs": [
    {"id": "ash-1", "title": "Data Analyst Intern", "location": "Remote", "isListed": True,
     "descriptionPlain": "x", "jobUrl": "https://jobs.ashbyhq.com/gamma/ash-1", "publishedAt": RECENT},
    {"id": "ash-2", "title": "Data Analyst", "location": "Bengaluru", "isListed": False,
     "descriptionPlain": "x", "jobUrl": "u", "publishedAt": RECENT},
]}


def test_fetchers_parse_public_boards():
    http = _http({
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": httpx.Response(200, json=GREENHOUSE),
        "https://api.lever.co/v0/postings/beta": httpx.Response(200, json=LEVER),
        "https://api.ashbyhq.com/posting-api/job-board/gamma": httpx.Response(200, json=ASHBY),
    })
    gh = jobs.fetch_greenhouse(http, "acme", "Acme")
    assert gh[0].company == "Acme" and gh[0].location == "Bengaluru" and gh[0].posted_at == RECENT
    assert "SQL dashboards" in gh[0].description and "- Python" in gh[0].description

    lv = jobs.fetch_lever(http, "beta", "Beta")[0]
    assert lv.apply_url.endswith("/apply") and "Requirements:" in lv.description and "- SQL" in lv.description
    assert lv.posted_at == "2025-10-09"

    ash = jobs.fetch_ashby(http, "gamma", "Gamma")
    assert [j.external_id for j in ash] == ["ash-1"]  # unlisted job dropped


def test_prefilter(cfg):
    niche = cfg.niche
    ok = Job("x", "1", "Acme", "Data Analyst", "Bengaluru, India", posted_at=RECENT)
    assert jobs.passes_prefilter(ok, niche)
    assert not jobs.passes_prefilter(Job("x", "2", "Acme", "Backend Engineer", "Bengaluru"), niche)
    assert not jobs.passes_prefilter(Job("x", "3", "Acme", "Data Analyst Intern", "Remote"), niche)
    assert not jobs.passes_prefilter(Job("x", "4", "Acme", "Data Analyst", "Mumbai"), niche)
    old = (date.today() - timedelta(days=60)).isoformat()
    assert not jobs.passes_prefilter(Job("x", "5", "Acme", "Data Analyst", "Remote", posted_at=old), niche)
    assert jobs.passes_prefilter(Job("x", "6", "Acme", "Data Analyst", ""), niche)  # unknown location kept


def _scores(prompt, schema):
    ids = [int(part.split('"')[0]) for part in prompt.split('<job id="')[1:]]
    return {"scores": [{"id": i, "score": 85 if i % 2 else 40, "verdict": "apply", "reasons": ["SQL match"],
                        "missing_skills": ["dbt"]} for i in ids]}


def test_scan_stores_only_matches_and_scores_them(cfg, conn):
    cfg.data["jobs"].update(greenhouse=["acme"], lever=[{"slug": "beta", "name": "Beta Corp"}], ashby=["gamma"])
    http = _http({
        "https://boards-api.greenhouse.io": httpx.Response(200, json=GREENHOUSE),
        "https://api.lever.co": httpx.Response(200, json=LEVER),
        "https://api.ashbyhq.com": httpx.Response(500),
    })
    summary = jobs.scan(cfg, conn, FakeLLM(_scores), http=http)
    assert summary["found"] == 3 and summary["new"] == 1  # lever job is too old (createdAt 2025)
    assert any("ashby/gamma" in e for e in summary["errors"])
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["title"] == "Senior Data Analyst" and row["score"] in (85, 40)
    assert row["status"] == ("shortlisted" if row["score"] == 85 else "skipped")
    # Second scan finds nothing new and calls Claude for nothing.
    llm = FakeLLM(_scores)
    assert jobs.scan(cfg, conn, llm, http=http)["new"] == 0 and llm.prompts == []


def test_status_rules(conn):
    job_id = jobs.upsert(conn, Job("manual", "u1", "Acme", "Data Analyst"))
    assert jobs.upsert(conn, Job("manual", "u1", "Acme", "Data Analyst")) is None
    assert jobs.update_status(conn, job_id, "applied", origin="email")
    assert conn.execute("SELECT applied_at FROM jobs").fetchone()[0]
    assert jobs.update_status(conn, job_id, "interview", origin="email")
    # A late confirmation email must not move the job back.
    assert not jobs.update_status(conn, job_id, "applied", origin="email")
    assert jobs.update_status(conn, job_id, "rejected", origin="email")
    assert not jobs.update_status(conn, job_id, "interview", origin="email")
    # Manual changes always win.
    assert jobs.update_status(conn, job_id, "interview", origin="manual")
    assert conn.execute("SELECT status FROM jobs").fetchone()[0] == "interview"
    assert conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 6
    with pytest.raises(ValueError):
        jobs.update_status(conn, job_id, "dreaming")


def _kit(prompt, schema):
    return {"cover_letter": "Dear team", "resume_bullets": ["Did SQL"], "why_company": "Because",
            "why_fit": "Fit", "referral_message": "Hi!", "interview_prep": ["Window functions"]}


def test_prepare_kits_picks_best_shortlisted(cfg, conn):
    for i, score in enumerate([75, 95, 90]):
        job_id = jobs.upsert(conn, Job("manual", f"u{i}", f"Co{i}", "Data Analyst", apply_url=f"https://apply/{i}"))
        conn.execute("UPDATE jobs SET score=?, status='shortlisted', score_reasons=? WHERE id=?",
                     (score, json.dumps({"reasons": ["good"], "missing_skills": ["dbt"]}), job_id))
    paths = jobs.prepare_kits(cfg, conn, FakeLLM(_kit))
    assert len(paths) == 2
    text = paths[0].read_text()
    assert "Co1" in text and "https://apply/1" in text and "18 LPA" in text and "dbt" in text
    statuses = [r[0] for r in conn.execute("SELECT status FROM jobs ORDER BY id")]
    assert statuses == ["shortlisted", "kit_ready", "kit_ready"]


def test_careers_page_via_firecrawl(cfg, conn, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    cfg.data["jobs"]["career_pages"] = [{"url": "https://delta.example/careers", "name": "Delta"}]
    scraped = []

    def firecrawl(request):
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer fc-test" and body["formats"] == ["markdown"]
        scraped.append(body["url"])
        return httpx.Response(200, json={"success": True, "data": {"markdown": f"page {body['url']}"}})

    def handler(prompt, schema):
        if "careers page" in prompt:
            return {"jobs": [{"title": "Data Analyst", "location": "Bengaluru", "url": "/jobs/7"},
                             {"title": "Designer", "location": "Bengaluru", "url": "/jobs/8"}]}
        return _scores(prompt, schema)

    summary = jobs.scan(cfg, conn, FakeLLM(handler), http=_http({jobs.FIRECRAWL_URL: firecrawl}))
    assert summary["new"] == 1 and summary["errors"] == []
    # Only the matching job's detail page is scraped.
    assert scraped == ["https://delta.example/careers", "https://delta.example/jobs/7"]
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["url"] == "https://delta.example/jobs/7" and row["description"].startswith("page ")


def test_add_manual_refuses_to_scrape_linkedin(cfg, conn):
    with pytest.raises(ValueError, match="jd-file"):
        jobs.add_manual(cfg, conn, FakeLLM(_scores), "https://www.linkedin.com/jobs/view/123")


def test_add_manual_with_pasted_description(cfg, conn):
    def handler(prompt, schema):
        if "extract the company" in prompt:
            return {"company": "Zeta", "title": "Data Analyst", "location": "Remote", "apply_url": ""}
        return _scores(prompt, schema)

    job_id = jobs.add_manual(cfg, conn, FakeLLM(handler), "https://www.linkedin.com/jobs/view/123", "JD text")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["company"] == "Zeta" and row["apply_url"] == "https://www.linkedin.com/jobs/view/123"
    assert row["score"] is not None
