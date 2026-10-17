"""Command line: `python -m career_agent <command>`."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import comments, db, jobs, outreach, posts, tracker
from .config import Config, ConfigError
from .linkedin import LinkedInClient, LinkedInError, authorize, post_url
from .llm import LLM, LLMError

TEMPLATES = Path(__file__).resolve().parent.parent


def cmd_init(args) -> None:
    root = Path(args.home or Path.cwd())
    copies = [("config/profile.example.yaml", "config/profile.yaml"), (".env.example", ".env"),
              ("inbox/posts_to_comment.example.md", "inbox/posts_to_comment.md")]
    for src, dst in copies:
        target = root / dst
        if target.exists():
            print(f"exists, left alone: {dst}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TEMPLATES / src, target)
        print(f"created {dst}")
    for folder in ("data", "output"):
        (root / folder).mkdir(exist_ok=True)
    print("\nNext: fill config/profile.yaml and .env, then run `python -m career_agent linkedin-auth`.")


def _setup(args):
    cfg = Config(args.home)
    return cfg, db.connect(cfg.db_path)


def _llm(cfg) -> LLM:
    return LLM(cfg)


def cmd_linkedin_auth(args) -> None:
    cfg, _ = _setup(args)
    saved = authorize(
        cfg.linkedin_token_path,
        cfg.env("LINKEDIN_CLIENT_ID", required=True),
        cfg.env("LINKEDIN_CLIENT_SECRET", required=True),
        cfg.env("LINKEDIN_REDIRECT_URI", "http://localhost:8765/callback"),
    )
    print(f"Connected as {saved['name']}. Token valid for ~60 days.")


def cmd_daily(args) -> None:
    from .daily import run

    cfg, conn = _setup(args)
    digest_path, report_path, errors = run(cfg, conn, _llm(cfg), send_email=args.email or None)
    print(digest_path.read_text(encoding="utf-8"))
    print(f"\nDigest: {digest_path}\nReport: {report_path}")
    if errors:
        sys.exit(1)


def cmd_report(args) -> None:
    from .report import write_report

    cfg, conn = _setup(args)
    try:
        days = LinkedInClient(cfg.linkedin_token_path).days_left()
    except LinkedInError:
        days = None
    path, _ = write_report(cfg, conn, days)
    print(f"Report written to {path}")


# ---- posts --------------------------------------------------------------------

def cmd_post(args) -> None:
    cfg, conn = _setup(args)
    if args.action == "generate":
        post_id = posts.generate(cfg, conn, _llm(cfg), args.theme)
        _show_post(conn, post_id)
        print(f"\nApprove with: python -m career_agent post approve {post_id}")
    elif args.action == "list":
        for r in conn.execute("SELECT id, status, created_at, text FROM posts ORDER BY id DESC LIMIT 20"):
            print(f"#{r['id']:<4} {r['status']:<10} {r['created_at'][:10]}  {r['text'].splitlines()[0][:70]}")
    elif args.action == "show":
        _show_post(conn, _need_id(args))
    elif args.action == "edit":
        if not args.file:
            sys.exit("post edit needs --file with the new text")
        conn.execute("UPDATE posts SET text=? WHERE id=?", (Path(args.file).read_text(encoding="utf-8").strip(), _need_id(args)))
        conn.commit()
        print("Updated.")
    elif args.action in ("approve", "reject"):
        posts.set_status(conn, _need_id(args), "approved" if args.action == "approve" else "rejected")
        print(f"Post #{args.id} {args.action}d.")
    elif args.action == "publish":
        client = LinkedInClient(cfg.linkedin_token_path, version=cfg.env("LINKEDIN_API_VERSION"))
        urn = posts.publish(conn, _need_id(args), client)
        print(f"Published: {post_url(urn)}")


def _show_post(conn, post_id: int) -> None:
    r = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if r is None:
        sys.exit(f"No post #{post_id}")
    print(f"Post #{r['id']} [{r['status']}] theme: {r['theme']}\n{'-' * 60}\n{r['text']}")


def _need_id(args) -> int:
    if args.id is None:
        sys.exit("This action needs an id")
    return args.id


# ---- contacts / outreach / comments ----------------------------------------------

def cmd_contacts(args) -> None:
    cfg, conn = _setup(args)
    if args.action == "import":
        print(f"Added {outreach.import_csv(conn, Path(args.arg))} new contacts.")
    elif args.action == "discover":
        print(f"Added {outreach.discover(cfg, conn, _llm(cfg), args.count)} new contacts.")
    elif args.action == "list":
        sql, params = "SELECT * FROM contacts", ()
        if args.status:
            sql, params = sql + " WHERE status=?", (args.status,)
        for r in conn.execute(sql + " ORDER BY id", params):
            print(f"#{r['id']:<4} {r['status']:<14} {r['name']} — {r['title'] or ''} @ {r['company'] or ''}  {r['linkedin_url'] or ''}")
    elif args.action == "mark":
        if args.arg is None or args.status_value is None:
            sys.exit(f"Usage: contacts mark <id> <status>  (status: {', '.join(outreach.STATUSES)})")
        outreach.mark(conn, int(args.arg), args.status_value)
        print("Saved.")


def cmd_outreach(args) -> None:
    cfg, conn = _setup(args)
    drafts = outreach.draft(cfg, conn, _llm(cfg), outreach.select_for_today(cfg, conn, args.limit))
    for d in drafts:
        c = d["contact"]
        print(f"\n[{d['kind']}] #{c['id']} {c['name']} ({c.get('company') or ''}) {c.get('linkedin_url') or ''}\n{d['text']}")
    print(f"\n{len(drafts)} drafts. After sending, mark each: contacts mark <id> requested|messaged")


def cmd_comment(args) -> None:
    cfg, conn = _setup(args)
    if args.text:
        drafted = comments.draft(conn, _llm(cfg), [{"text": args.text, "author": args.author, "url": args.url}])
    else:
        drafted = comments.run_inbox(cfg, conn, _llm(cfg))
    if not drafted:
        print("Nothing to comment on. Paste posts into inbox/posts_to_comment.md or use --text.")
    for d in drafted:
        print(f"\n{d.get('author') or 'Post'} {d.get('url') or ''}")
        for i, option in enumerate(d["options"], 1):
            print(f"  {i}. {option}")


# ---- jobs / tracking ----------------------------------------------------------------

def cmd_jobs(args) -> None:
    cfg, conn = _setup(args)
    if args.action == "scan":
        summary = jobs.scan(cfg, conn, _llm(cfg))
        print(f"Found {summary['found']} openings · {summary['new']} new matches · {summary['shortlisted']} shortlisted")
        for e in summary["errors"]:
            print("  problem:", e)
    elif args.action == "add":
        jd = Path(args.jd_file).read_text(encoding="utf-8") if args.jd_file else None
        job_id = jobs.add_manual(cfg, conn, _llm(cfg), args.arg, jd)
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        print(f"Job #{job_id}: {r['title']} @ {r['company']} — match {r['score']} ({r['status']})")
    elif args.action == "list":
        sql = "SELECT * FROM jobs WHERE status NOT IN ('new','skipped')" if not args.status else "SELECT * FROM jobs WHERE status=?"
        params = (args.status,) if args.status else ()
        for r in conn.execute(sql + " ORDER BY coalesce(score, 0) DESC LIMIT 50", params):
            print(f"#{r['id']:<4} {r['status']:<11} {r['score'] if r['score'] is not None else '-':>3}  {r['title']} @ {r['company']}  {r['apply_url'] or r['url'] or ''}")
    elif args.action == "kit":
        print(f"Kit written to {jobs.make_kit(cfg, conn, _llm(cfg), int(args.arg))}")
    elif args.action == "mark":
        if args.arg is None or args.status_value is None:
            sys.exit(f"Usage: jobs mark <id> <status> [--note ...]  (status: {', '.join(jobs.STATUSES)})")
        jobs.update_status(conn, int(args.arg), args.status_value, args.note or "", origin="manual")
        conn.commit()
        print("Saved.")


def cmd_track(args) -> None:
    cfg, conn = _setup(args)
    updates = tracker.sync(cfg, conn, _llm(cfg))
    print("\n".join(updates) or "No new application updates.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="career_agent", description="Daily LinkedIn + job search assistant")
    p.add_argument("--home", help="project folder (default: current directory)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config files from the examples").set_defaults(func=cmd_init)
    sub.add_parser("linkedin-auth", help="connect your LinkedIn account (once every ~60 days)").set_defaults(func=cmd_linkedin_auth)
    d = sub.add_parser("daily", help="run everything once and write today's digest")
    d.add_argument("--email", action="store_true", help="email the digest to yourself")
    d.set_defaults(func=cmd_daily)
    sub.add_parser("report", help="write output/report.html").set_defaults(func=cmd_report)

    s = sub.add_parser("post", help="LinkedIn posts")
    s.add_argument("action", choices=["generate", "list", "show", "edit", "approve", "reject", "publish"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--theme")
    s.add_argument("--file", help="for edit: file with the new post text")
    s.set_defaults(func=cmd_post)

    c = sub.add_parser("contacts", help="HR / founder contact list")
    c.add_argument("action", choices=["import", "discover", "list", "mark"])
    c.add_argument("arg", nargs="?", help="CSV path (import) or contact id (mark)")
    c.add_argument("status_value", nargs="?", help="new status (mark)")
    c.add_argument("--count", type=int, default=15)
    c.add_argument("--status", help="filter for list")
    c.set_defaults(func=cmd_contacts)

    o = sub.add_parser("outreach", help="draft today's messages")
    o.add_argument("--limit", type=int)
    o.set_defaults(func=cmd_outreach)

    m = sub.add_parser("comment", help="draft comments for posts")
    m.add_argument("--text", help="post text (otherwise uses inbox/posts_to_comment.md)")
    m.add_argument("--author")
    m.add_argument("--url")
    m.set_defaults(func=cmd_comment)

    j = sub.add_parser("jobs", help="find, score and track jobs")
    j.add_argument("action", choices=["scan", "add", "list", "kit", "mark"])
    j.add_argument("arg", nargs="?", help="URL (add) or job id (kit, mark)")
    j.add_argument("status_value", nargs="?", help="new status (mark)")
    j.add_argument("--jd-file", help="for add: job description text file (needed for LinkedIn jobs)")
    j.add_argument("--status", help="filter for list")
    j.add_argument("--note")
    j.set_defaults(func=cmd_jobs)

    sub.add_parser("track", help="read job emails and update application status").set_defaults(func=cmd_track)
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ConfigError, LinkedInError, LLMError, ValueError) as e:
        sys.exit(f"Error: {e}")
