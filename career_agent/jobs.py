"""Job discovery, matching and application kits.

Sources (no LinkedIn scraping):
- Greenhouse / Lever / Ashby public job-board APIs (used by many startups)
- Any careers page, via Firecrawl (page -> markdown) + Claude (markdown -> job list)
- Single job links you add by hand (`jobs add <url>`)
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx2 as httpx

from . import db
from .llm import schema_object

FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
STATUS_RANK = {"new": 0, "skipped": 0, "shortlisted": 1, "kit_ready": 2, "lead": 2,
               "applied": 3, "assessment": 4, "interview": 5, "offer": 6}
FINAL = {"rejected", "withdrawn"}
STATUSES = list(STATUS_RANK) + sorted(FINAL)


@dataclass
class Job:
    source: str
    external_id: str
    company: str
    title: str
    location: str = ""
    url: str = ""
    apply_url: str = ""
    description: str = ""
    posted_at: str = ""  # YYYY-MM-DD when known


# ---- helpers ----------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "tr", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n- " if tag == "li" else "\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup or "")
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _day(value) -> str:
    """Normalise ISO strings or epoch milliseconds to YYYY-MM-DD."""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OSError):
        return ""


def _board(entry) -> tuple[str, str]:
    """Config entries are either "slug" or {slug: ..., name: ...}."""
    if isinstance(entry, dict):
        return entry["slug"], entry.get("name") or entry["slug"]
    return entry, entry


def _client() -> httpx.Client:
    return httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": "career-agent/1.0"})


# ---- sources ----------------------------------------------------------------------

def fetch_greenhouse(http, slug: str, name: str) -> list[Job]:
    resp = http.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    resp.raise_for_status()
    return [Job(
        source="greenhouse", external_id=str(j["id"]), company=j.get("company_name") or name,
        title=j["title"], location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""), apply_url=j.get("absolute_url", ""),
        description=html_to_text(html.unescape(j.get("content") or "")),
        posted_at=_day(j.get("first_published") or j.get("updated_at")),
    ) for j in resp.json().get("jobs", [])]


def fetch_lever(http, slug: str, name: str) -> list[Job]:
    resp = http.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    resp.raise_for_status()
    jobs = []
    for p in resp.json():
        sections = [p.get("descriptionPlain") or ""]
        for lst in p.get("lists") or []:
            sections.append(f"{lst.get('text', '')}:\n{html_to_text(lst.get('content', ''))}")
        sections.append(p.get("additionalPlain") or "")
        jobs.append(Job(
            source="lever", external_id=p["id"], company=name, title=p["text"],
            location=(p.get("categories") or {}).get("location", ""),
            url=p.get("hostedUrl", ""), apply_url=p.get("applyUrl") or p.get("hostedUrl", ""),
            description="\n\n".join(s for s in sections if s).strip(), posted_at=_day(p.get("createdAt")),
        ))
    return jobs


def fetch_ashby(http, slug: str, name: str) -> list[Job]:
    resp = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    resp.raise_for_status()
    jobs = []
    for j in resp.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        location = j.get("location") or ""
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (Remote)".strip()
        jobs.append(Job(
            source="ashby", external_id=j["id"], company=name, title=j["title"], location=location,
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            posted_at=_day(j.get("publishedAt")),
        ))
    return jobs


def firecrawl_markdown(http, api_key: str, url: str) -> str:
    resp = http.post(FIRECRAWL_URL, headers={"Authorization": f"Bearer {api_key}"},
                     json={"url": url, "formats": ["markdown"], "onlyMainContent": True})
    if resp.status_code != 200:
        raise RuntimeError(f"Firecrawl could not scrape {url} ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"Firecrawl could not scrape {url}: {body}")
    return body["data"].get("markdown", "")


def fetch_careers_page(http, llm, api_key: str, page: dict, niche: dict, max_details: int = 8) -> list[Job]:
    """Scrape a careers page, let Claude list the openings, then scrape the
    detail page of the ones that pass the pre-filter."""
    url, company = page["url"], page.get("name") or urlparse(page["url"]).netloc
    markdown = firecrawl_markdown(http, api_key, url)
    listing = schema_object({
        "title": {"type": "string"}, "location": {"type": "string"}, "url": {"type": "string"},
    })
    result = llm.json(
        f"Below is the careers page of {company} ({url}) as markdown. List every open job "
        "posting on it: title, location (empty string if not shown) and the link to the job "
        "(absolute or relative, empty string if none). Do not invent jobs.\n\n" + markdown,
        schema_object({"jobs": {"type": "array", "items": listing}}), effort="low")
    jobs = []
    for item in result["jobs"]:
        link = urljoin(url, item["url"]) if item["url"] else ""
        job = Job(source="careers_page", external_id=link or f"{company}:{item['title']}", company=company,
                  title=item["title"], location=item["location"], url=link or url, apply_url=link or url)
        if passes_prefilter(job, niche) and link and max_details > 0:
            max_details -= 1
            try:
                job.description = firecrawl_markdown(http, api_key, link)
            except RuntimeError:
                pass  # keep the job; it will be scored on title/location only
        jobs.append(job)
    return jobs


# ---- filtering and storage ---------------------------------------------------------

def passes_prefilter(job: Job, niche: dict) -> bool:
    """Cheap keyword filter so only plausible jobs reach Claude."""
    title = job.title.lower()
    keywords = niche.get("title_keywords") or niche.get("target_roles") or []
    if keywords and not any(k.lower() in title for k in keywords):
        return False
    if any(a.lower() in title for a in niche.get("avoid_keywords") or []):
        return False
    locations = niche.get("locations") or []
    if locations and job.location and not any(l.lower() in job.location.lower() for l in locations):
        return False
    max_age = niche.get("max_job_age_days", 30)
    if job.posted_at and max_age and job.posted_at < (date.today() - timedelta(days=max_age)).isoformat():
        return False
    return True


def upsert(conn, job: Job, status: str = "new") -> int | None:
    """Insert a job if unseen. Returns the new row id, or None if it already existed."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO jobs (source, external_id, company, title, location, url, apply_url,
           description, posted_at, discovered_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (job.source, job.external_id, job.company, job.title, job.location, job.url, job.apply_url,
         job.description, job.posted_at, db.now(), status))
    return cur.lastrowid if cur.rowcount else None


def update_status(conn, job_id: int, status: str, detail: str = "", origin: str = "manual") -> bool:
    """Record an event and move the job forward. Automatic updates never move a
    job backwards (e.g. a late 'application received' email after an interview)."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"No job with id {job_id}")
    old, ts = row["status"], db.now()
    conn.execute("INSERT INTO job_events (job_id, at, kind, detail, origin) VALUES (?,?,?,?,?)",
                 (job_id, ts, status, detail, origin))
    if origin != "manual":
        if old in FINAL and status != "offer":
            return False
        if status not in FINAL and status != "offer" and STATUS_RANK[status] <= STATUS_RANK.get(old, 0):
            return False
    conn.execute("UPDATE jobs SET status=?, last_update_at=? WHERE id=?", (status, ts, job_id))
    if status == "applied":
        conn.execute("UPDATE jobs SET applied_at=coalesce(applied_at, ?) WHERE id=?", (ts, job_id))
    return True


# ---- Claude: scoring and kits ------------------------------------------------------

def score_new(cfg, conn, llm, batch_size: int = 5) -> int:
    rows = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE status='new' AND score IS NULL ORDER BY id")]
    threshold = int(cfg.niche.get("min_match_score", 70))
    item = schema_object({
        "id": {"type": "integer"},
        "score": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["apply", "maybe", "skip"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
    })
    shortlisted = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        jobs_text = "\n\n".join(
            f"<job id=\"{j['id']}\">\nTitle: {j['title']}\nCompany: {j['company']}\n"
            f"Location: {j['location']}\n{j['description'] or '(no description)'}\n</job>" for j in batch)
        result = llm.json(
            "Score how well each job fits me (0-100): skills overlap, seniority and years of "
            "experience, location/remote, industry. Be strict: 80+ means a recruiter would "
            "likely shortlist me. Give 2-3 short reasons and the key missing skills.\n\n" + jobs_text,
            schema_object({"scores": {"type": "array", "items": item}}), effort="low")
        ids = {j["id"] for j in batch}
        for s in result["scores"]:
            if s["id"] not in ids:
                continue
            status = "shortlisted" if s["score"] >= threshold else "skipped"
            shortlisted += status == "shortlisted"
            reasons = {k: s[k] for k in ("verdict", "reasons", "missing_skills")}
            conn.execute("UPDATE jobs SET score=?, score_reasons=?, status=?, last_update_at=? WHERE id=?",
                         (s["score"], json.dumps(reasons, ensure_ascii=False), status, db.now(), s["id"]))
        conn.commit()
    return shortlisted


def make_kit(cfg, conn, llm, job_id: int) -> Path:
    job = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    me = cfg.me
    result = llm.json(
        "Prepare my application for this job.\n"
        "- cover_letter: under 220 words, specific to this company and role, no clichés.\n"
        "- resume_bullets: 4-6 bullets rewriting my REAL experience in this job's language "
        "(use its keywords for ATS); never add skills or results I don't have.\n"
        "- why_company and why_fit: 60-100 words each, for application form questions.\n"
        "- referral_message: under 300 characters, to someone who works there.\n"
        "- interview_prep: the 3-5 topics I should brush up on for this role.\n\n"
        f"My resume:\n{me.get('resume_text', '(not provided)')}\n\n"
        f"<job>\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
        f"{job['description'] or '(no description)'}\n</job>",
        schema_object({
            "cover_letter": {"type": "string"},
            "resume_bullets": {"type": "array", "items": {"type": "string"}},
            "why_company": {"type": "string"},
            "why_fit": {"type": "string"},
            "referral_message": {"type": "string"},
            "interview_prep": {"type": "array", "items": {"type": "string"}},
        }))
    reasons = json.loads(job["score_reasons"] or "{}")
    lines = [
        f"# {job['title']} — {job['company']}",
        "",
        f"**Apply here:** {job['apply_url'] or job['url']}",
        f"**Location:** {job['location'] or '-'}  ",
        f"**Match score:** {job['score'] if job['score'] is not None else '-'}/100 — "
        + "; ".join(reasons.get("reasons", [])),
        "",
        "When you have submitted, run: `python -m career_agent jobs mark "
        f"{job_id} applied` (or let the email tracker detect the confirmation).",
        "",
        "## Cover letter", "", result["cover_letter"], "",
        "## Resume bullets for this role", "", *[f"- {b}" for b in result["resume_bullets"]], "",
        "## Form answers", "",
        f"**Why this company?** {result['why_company']}", "",
        f"**Why are you a good fit?** {result['why_fit']}", "",
        f"**Notice period:** {me.get('notice_period', '-')}  ",
        f"**Current CTC:** {me.get('current_ctc', '-')}  ",
        f"**Expected CTC:** {me.get('expected_ctc', '-')}", "",
        "## Referral request (send to someone at the company)", "", result["referral_message"], "",
        "## Prepare for the interview", "", *[f"- {t}" for t in result["interview_prep"]], "",
    ]
    if reasons.get("missing_skills"):
        lines += ["## Gaps the recruiter may notice", "", *[f"- {s}" for s in reasons["missing_skills"]], ""]
    folder = cfg.output_dir / "apply_kits"
    folder.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower()).strip("-")[:80]
    path = folder / f"{job_id}-{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    conn.execute("UPDATE jobs SET kit_path=? WHERE id=?", (str(path), job_id))
    if job["status"] in ("new", "shortlisted", "skipped"):
        update_status(conn, job_id, "kit_ready", "Application kit prepared", origin="agent")
    conn.commit()
    return path


def prepare_kits(cfg, conn, llm, limit: int | None = None) -> list[Path]:
    limit = limit if limit is not None else int(cfg.jobs.get("daily_apply_kits", 5))
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status='shortlisted' AND kit_path IS NULL ORDER BY score DESC LIMIT ?", (limit,))
    return [make_kit(cfg, conn, llm, r["id"]) for r in rows.fetchall()]


# ---- orchestration ---------------------------------------------------------------

def scan(cfg, conn, llm, http=None) -> dict:
    """Fetch every configured source, keep plausible new jobs, score them."""
    http = http or _client()
    src, niche = cfg.jobs, cfg.niche
    found, errors = [], []
    fetchers = (("greenhouse", fetch_greenhouse), ("lever", fetch_lever), ("ashby", fetch_ashby))
    for key, fetch in fetchers:
        for entry in src.get(key) or []:
            slug, name = _board(entry)
            try:
                found += fetch(http, slug, name)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                errors.append(f"{key}/{slug}: {e}")
    pages = src.get("career_pages") or []
    if pages:
        api_key = cfg.env("FIRECRAWL_API_KEY")
        if not api_key:
            errors.append("career_pages configured but FIRECRAWL_API_KEY is missing")
        for page in pages if api_key else []:
            page = page if isinstance(page, dict) else {"url": page}
            try:
                found += fetch_careers_page(http, llm, api_key, page, niche)
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as e:
                errors.append(f"careers page {page['url']}: {e}")

    new = sum(upsert(conn, j) is not None for j in found if passes_prefilter(j, niche))
    conn.commit()
    shortlisted = score_new(cfg, conn, llm)
    return {"found": len(found), "new": new, "shortlisted": shortlisted, "errors": errors}


def add_manual(cfg, conn, llm, url: str, jd_text: str | None = None, http=None) -> int:
    """Add one job by link. LinkedIn job pages can't be scraped, so for those
    pass the description text with --jd-file."""
    if jd_text is None:
        if "linkedin.com" in urlparse(url).netloc:
            raise ValueError("LinkedIn pages can't be scraped. Copy the job description into a "
                             "text file and run: jobs add <url> --jd-file job.txt")
        api_key = cfg.env("FIRECRAWL_API_KEY", required=True)
        jd_text = firecrawl_markdown(http or _client(), api_key, url)
    meta = llm.json(
        "From this job posting, extract the company name, job title, location (empty string if "
        f"unknown) and the application link (empty string if none). Page URL: {url}\n\n{jd_text}",
        schema_object({"company": {"type": "string"}, "title": {"type": "string"},
                       "location": {"type": "string"}, "apply_url": {"type": "string"}}), effort="low")
    job = Job(source="manual", external_id=url, company=meta["company"], title=meta["title"],
              location=meta["location"], url=url, apply_url=urljoin(url, meta["apply_url"]) if meta["apply_url"] else url,
              description=jd_text)
    job_id = upsert(conn, job)
    conn.commit()
    if job_id is None:
        return conn.execute("SELECT id FROM jobs WHERE source='manual' AND external_id=?", (url,)).fetchone()["id"]
    score_new(cfg, conn, llm)
    return job_id
