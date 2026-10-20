"""Progress report (output/report.html) and the daily to-do digest."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from html import escape

from .linkedin import post_url

APPLIED = ("applied", "assessment", "interview", "offer", "rejected", "withdrawn")
PIPELINE = [("shortlisted", "Shortlisted"), ("kit_ready", "Kit ready"), ("lead", "Recruiter leads"),
            ("applied", "Applied, waiting"), ("assessment", "Assessment"), ("interview", "Interview"),
            ("offer", "Offer"), ("rejected", "Rejected")]
LABELS = {"new": "New", "skipped": "Skipped", "shortlisted": "Shortlisted", "kit_ready": "Kit ready",
          "lead": "Recruiter lead", "applied": "Applied", "assessment": "Assessment", "interview": "Interview",
          "offer": "Offer", "rejected": "Rejected", "withdrawn": "Withdrawn"}
# Status markers: an icon + label always accompany the color.
MARKERS = {"offer": ("good", "✓"), "rejected": ("critical", "✕"), "interview": ("accent", "●"),
           "assessment": ("accent", "●"), "lead": ("accent", "●")}


def _days_since(ts: str | None, today: date) -> int | None:
    if not ts:
        return None
    return (today - datetime.fromisoformat(ts).date()).days


def collect(conn, today: date | None = None, follow_up_days: int = 10) -> dict:
    today = today or date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('new','skipped') ORDER BY coalesce(last_update_at, discovered_at) DESC")]
    applications = [j for j in jobs if j["applied_at"] or j["status"] in APPLIED]
    reached = {r["job_id"]: r["kind"] for r in conn.execute(
        "SELECT job_id, kind FROM job_events WHERE kind IN ('assessment','interview','offer')")}
    replied = [j for j in applications if j["id"] in reached or j["status"] in ("rejected", "assessment", "interview", "offer")]
    interviews = [j for j in applications if j["status"] in ("interview", "offer") or reached.get(j["id"]) in ("interview", "offer")]

    for j in jobs:
        j["days_waiting"] = _days_since(j["last_update_at"] or j["applied_at"], today)
        j["reasons"] = json.loads(j["score_reasons"] or "{}").get("reasons", [])

    attention = []
    for j in jobs:
        name = f"{j['title']} at {j['company']}"
        if j["status"] == "applied" and (j["days_waiting"] or 0) >= follow_up_days:
            attention.append(f"{name}: no reply for {j['days_waiting']} days. Follow up or ask someone there for a referral.")
        elif j["status"] == "interview":
            attention.append(f"{name}: interview stage. Prep with the apply kit.")
        elif j["status"] == "assessment":
            attention.append(f"{name}: assessment pending.")
        elif j["status"] == "lead":
            attention.append(f"{name}: a recruiter reached out. Reply.")
        elif j["status"] == "kit_ready":
            attention.append(f"{name}: application kit ready. Apply.")

    profile = conn.execute("SELECT at, score, rewritten_score FROM profile_scores ORDER BY id DESC LIMIT 1").fetchone()
    if profile is None:
        attention.append("Recruiters read your profile before your messages: paste it into inbox/profile.md "
                         "and run `python -m career_agent profile`.")
    elif profile["at"] < (today - timedelta(days=30)).isoformat():
        attention.append(f"Your profile was last checked on {profile['at'][:10]}. Re-run `profile` after your updates.")

    contacts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM contacts GROUP BY status")}
    return {
        "today": today,
        "jobs": jobs,
        "applications": applications,
        "applied_week": sum(1 for j in applications if (j["applied_at"] or "") >= week_ago),
        "replied": len(replied),
        "interviews": len(interviews),
        "offers": sum(1 for j in applications if j["status"] == "offer"),
        "pipeline": [(label, sum(1 for j in jobs if j["status"] == key)) for key, label in PIPELINE],
        "attention": attention,
        "events": [dict(r) for r in conn.execute(
            """SELECT e.at, e.kind, e.detail, e.origin, j.company, j.title FROM job_events e
               JOIN jobs j ON j.id = e.job_id ORDER BY e.at DESC, e.id DESC LIMIT 25""")],
        "posts": [dict(r) for r in conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT 10")],
        "posts_month": conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status='published' AND published_at >= ?", (month_ago,)).fetchone()[0],
        "outreach_week": conn.execute(
            "SELECT COUNT(*) FROM outreach_messages WHERE sent_at >= ?", (week_ago,)).fetchone()[0],
        "contacts": contacts,
        "profile": profile,
    }


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "–"


def _tile(label: str, value, note: str = "") -> str:
    note_html = f'<div class="note">{escape(note)}</div>' if note else ""
    return f'<div class="tile"><div class="label">{escape(label)}</div><div class="value">{escape(str(value))}</div>{note_html}</div>'


def _profile_tile(row) -> str:
    if row is None:
        return _tile("Profile score", "–", "run `python -m career_agent profile`")
    return _tile("Profile score", f"{row['score']}/100",
                 f"{row['rewritten_score']}/100 with the suggested rewrites · {row['at'][:10]}")


def _status(status: str) -> str:
    tone, icon = MARKERS.get(status, ("muted", "○"))
    return f'<span class="status"><span class="dot {tone}">{icon}</span>{escape(LABELS.get(status, status))}</span>'


def render_html(data: dict, token_days_left: int | None = None) -> str:
    apps = data["applications"]
    total = len(apps)
    tiles = "".join([
        _tile("Applications", total, f"{data['applied_week']} in the last 7 days"),
        _tile("Response rate", _pct(data["replied"], total), f"{data['replied']} of {total} replied"),
        _tile("Interviews", data["interviews"], f"{_pct(data['interviews'], total)} of applications"),
        _tile("Offers", data["offers"]),
        _tile("Ready to apply", sum(1 for j in data["jobs"] if j["status"] == "kit_ready"), "application kits prepared"),
        _tile("Posts published", data["posts_month"], "last 30 days"),
        _tile("Messages sent", data["outreach_week"], f"last 7 days · {data['contacts'].get('replied', 0)} replied so far"),
        _profile_tile(data["profile"]),
    ])

    peak = max((n for _, n in data["pipeline"]), default=0) or 1
    bars = "".join(
        f'<div class="bar-row" tabindex="0" data-tip="{escape(label)}: {n} job{"s" if n != 1 else ""}">'
        f'<span class="bar-label">{escape(label)}</span>'
        f'<span class="bar-track"><span class="bar" style="width:{max(n / peak * 100, 0.5 if n else 0):.1f}%"></span>'
        f'<span class="bar-value">{n}</span></span></div>'
        for label, n in data["pipeline"])
    pipeline_table = "".join(f"<tr><td>{escape(label)}</td><td class='num'>{n}</td></tr>" for label, n in data["pipeline"])

    attention = data["attention"][:]
    if token_days_left is not None and token_days_left < 7:
        attention.insert(0, f"LinkedIn connection expires in {max(token_days_left, 0)} days. Run `python -m career_agent linkedin-auth`.")
    attention_html = "".join(f"<li>{escape(a)}</li>" for a in attention) or "<li class='muted'>Nothing urgent.</li>"

    rows = "".join(_job_row(j) for j in data["jobs"])
    events = "".join(
        f"<li><span class='when'>{escape(e['at'][:10])}</span> <strong>{escape(e['company'])}</strong> · "
        f"{escape(e['title'])} → {escape(LABELS.get(e['kind'], e['kind']))}"
        f"{' · ' + escape(e['detail']) if e['detail'] else ''} <span class='muted'>({escape(e['origin'])})</span></li>"
        for e in data["events"]) or "<li class='muted'>No activity yet.</li>"
    posts = "".join(_post_item(p) for p in data["posts"]) or "<li class='muted'>No posts yet.</li>"
    contact_rows = "".join(
        f"<tr><td>{escape(s.replace('_', ' ').capitalize())}</td><td class='num'>{data['contacts'].get(s, 0)}</td></tr>"
        for s in ("new", "requested", "connected", "messaged", "replied", "not_interested"))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Search Report</title>
<style>
:root {{
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --good: #0ca30c; --critical: #d03b3b;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --series-1: #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10); --series-1: #3987e5;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1080px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 0 0 12px; }}
.sub, .muted, .note, .when {{ color: var(--muted); }}
.card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-top: 16px; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }}
@media (max-width: 760px) {{ .kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
.tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }}
.tile .label {{ color: var(--ink-2); font-size: 13px; }}
.tile .value {{ font-size: 28px; font-weight: 600; }}
.tile .note {{ font-size: 12px; }}
.grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.grid2 > .card {{ margin-top: 16px; }}
@media (max-width: 760px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}
.bar-row {{ display: grid; grid-template-columns: 130px 1fr; align-items: center; gap: 8px; padding: 3px 0; border-radius: 6px; outline: none; }}
.bar-row:hover .bar, .bar-row:focus .bar {{ filter: brightness(1.12); }}
.bar-label {{ color: var(--ink-2); font-size: 13px; }}
.bar-track {{ display: flex; align-items: center; gap: 6px; border-left: 1px solid var(--axis); min-height: 22px; }}
.bar {{ display: block; height: 16px; background: var(--series-1); border-radius: 0 4px 4px 0; }}
.bar-value {{ font-size: 13px; font-variant-numeric: tabular-nums; }}
#tip {{ position: fixed; pointer-events: none; background: var(--surface); color: var(--ink); border: 1px solid var(--border);
       border-radius: 8px; padding: 6px 10px; font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; }}
details {{ margin-top: 8px; font-size: 13px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
th {{ color: var(--ink-2); font-weight: 600; font-size: 13px; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
a {{ color: inherit; text-decoration-color: var(--axis); }}
.status {{ display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }}
.dot {{ font-size: 11px; line-height: 1; }}
.dot.good {{ color: var(--good); }} .dot.critical {{ color: var(--critical); }}
.dot.accent {{ color: var(--series-1); }} .dot.muted {{ color: var(--muted); }}
ul.list {{ margin: 0; padding-left: 18px; }} ul.list li {{ margin: 4px 0; }}
</style></head>
<body><main>
<h1>Job search report</h1>
<div class="sub">Generated {data['today'].strftime('%d %b %Y')} · {total} applications tracked</div>
<section class="kpis">{tiles}</section>
<div class="grid2">
  <section class="card"><h2>Where every job stands</h2>{bars}
    <details><summary>Show as table</summary><table><tr><th>Stage</th><th class="num">Jobs</th></tr>{pipeline_table}</table></details>
  </section>
  <section class="card"><h2>Needs your attention</h2><ul class="list">{attention_html}</ul></section>
</div>
<section class="card"><h2>All applications and shortlisted jobs</h2><div class="table-wrap"><table>
<tr><th>Company</th><th>Role</th><th>Status</th><th class="num">Applied</th><th class="num">Days since update</th><th class="num">Match</th></tr>
{rows or "<tr><td colspan='6' class='muted'>No jobs yet. Run <code>python -m career_agent jobs scan</code>.</td></tr>"}
</table></div></section>
<div class="grid2">
  <section class="card"><h2>Recent updates</h2><ul class="list">{events}</ul></section>
  <section class="card"><h2>LinkedIn activity</h2><ul class="list">{posts}</ul>
    <h2 style="margin-top:16px">Outreach contacts</h2><table>{contact_rows}</table></section>
</div>
</main>
<div id="tip" role="status"></div>
<script>
const tip = document.getElementById("tip");
function show(e) {{
  const row = e.currentTarget, r = row.getBoundingClientRect();
  tip.textContent = row.dataset.tip;
  tip.style.display = "block";
  const x = e.clientX ?? r.left + r.width / 2, y = e.clientY ?? r.top;
  tip.style.left = Math.min(x + 12, innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = (y - tip.offsetHeight - 10) + "px";
}}
document.querySelectorAll(".bar-row").forEach(row => {{
  row.addEventListener("pointermove", show);
  row.addEventListener("focus", show);
  row.addEventListener("pointerleave", () => tip.style.display = "none");
  row.addEventListener("blur", () => tip.style.display = "none");
}});
</script>
</body></html>"""


def _job_row(j: dict) -> str:
    link = j["apply_url"] or j["url"]
    if not (link or "").startswith(("https://", "http://")):
        link = ""  # links come from scraped pages; never render javascript: and the like
    role = f'<a href="{escape(link)}">{escape(j["title"])}</a>' if link else escape(j["title"])
    dash = "–"
    return (f"<tr><td>{escape(j['company'])}</td><td>{role}</td><td>{_status(j['status'])}</td>"
            f"<td class='num'>{escape((j['applied_at'] or '')[:10]) or dash}</td>"
            f"<td class='num'>{dash if j['days_waiting'] is None else j['days_waiting']}</td>"
            f"<td class='num'>{dash if j['score'] is None else j['score']}</td></tr>")


def _post_item(p: dict) -> str:
    score = p.get("human_score")
    score_html = f" <span class='muted'>(human score {score:.0f})</span>" if score is not None else ""
    return (f"<li><span class='when'>{escape((p['published_at'] or p['created_at'])[:10])}</span> "
            f"{_status_text(p)} {escape(p['text'].splitlines()[0][:110])}{score_html}</li>")


def _status_text(post: dict) -> str:
    if post["status"] == "published" and post["linkedin_urn"]:
        return f"<a href='{escape(post_url(post['linkedin_urn']))}'>Published</a> ·"
    return escape(post["status"].capitalize()) + " ·"


def write_report(cfg, conn, token_days_left: int | None = None):
    data = collect(conn, follow_up_days=int(cfg.jobs.get("follow_up_after_days", 10)))
    path = cfg.output_dir / "report.html"
    path.write_text(render_html(data, token_days_left), encoding="utf-8")
    return path, data


def digest_markdown(today: date, post_lines, outreach_drafts, comment_drafts, kits, updates, scan_summary, attention, errors) -> str:
    out = [f"# Today's plan, {today.strftime('%a %d %b %Y')}", ""]
    out += ["## 1. LinkedIn post", *[f"- {line}" for line in post_lines], ""]
    out += [f"## 2. Messages to send ({len(outreach_drafts)})", ""]
    if not outreach_drafts:
        out += ["No drafts. Add contacts with `contacts import` or `contacts discover`.", ""]
    labels = {"connect": "Connection request note", "dm": "First message", "follow_up": "Follow-up",
              "close_loop": "Last follow-up (closes the loop)"}
    for d in outreach_drafts:
        link = d.get("linkedin_url") or "(find their profile on LinkedIn)"
        mark = "requested" if d["kind"] == "connect" else "messaged"
        out += [f"### {d['name']} — {d.get('title') or ''}, {d.get('company') or ''}",
                f"{labels[d['kind']]} · {link}", "", "> " + d["text"].replace("\n", "\n> "), ""]
        if d.get("specific") == 0:
            out += ["No specific reason to contact them yet: add a note (a post of theirs, a launch, an "
                    "opening) to the contact, or skip them today.", ""]
        out += [f"After sending: `python -m career_agent contacts mark {d['contact_id']} {mark}`", ""]
    out += [f"## 3. Comments to post ({len(comment_drafts)})", ""]
    if not comment_drafts:
        out += ["Paste posts you want to comment on into `inbox/posts_to_comment.md` (see the example file).", ""]
    for c in comment_drafts:
        out += [f"### {c.get('author') or 'Post'} · {c.get('url') or ''}", ""]
        for i, opt in enumerate(c["options"], 1):
            out.append(f"{i}. [{opt['type']}] {opt['text']}")
        best = next((f"Post option {i}: {o['recommended']}" for i, o in enumerate(c["options"], 1)
                     if o.get("recommended")), None)
        out += [""] + ([best, ""] if best else []) + ([c["note"], ""] if c.get("note") else [])
    out += [f"## 4. Jobs ready to apply to ({len(kits)})", ""]
    if scan_summary:
        out.append(f"Scanned {scan_summary['found']} openings, {scan_summary['new']} new matches, "
                   f"{scan_summary['shortlisted']} shortlisted.")
    out += [f"- {k}" for k in kits] + [""]
    out += ["## 5. Application updates from email", *([f"- {u}" for u in updates] or ["- No new updates."]), ""]
    if attention:
        out += ["## Follow-ups", *[f"- {a}" for a in attention], ""]
    if errors:
        out += ["## Problems during this run", *[f"- {e}" for e in errors], ""]
    out.append("Full progress report: output/report.html")
    return "\n".join(out)
