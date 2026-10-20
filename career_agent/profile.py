"""Score your LinkedIn profile out of 100 the way a recruiter reads it, rewrite
the sections that lose points (fix-first), and re-score the rewrite.

Rubric adapted for job seekers from linkedin-agent-skill's li-profile (MIT).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from . import db
from .humanize import clean
from .llm import schema_object

RUBRIC = json.loads((Path(__file__).parent / "data" / "profile_rubric.json").read_text(encoding="utf-8"))
MAX_POINTS = {i["id"]: i["points"] for i in RUBRIC["items"]}
REWRITTEN = ["headline", "about_open", "about_body", "current_role", "skills"]
INBOX_NAME = "profile.md"


def score(llm, profile_text: str, target: str, only: list[str] | None = None) -> dict[str, dict]:
    items = [i for i in RUBRIC["items"] if only is None or i["id"] in only]
    rubric = "\n".join(f"- {i['id']} (max {i['points']}): {i['full_marks']}" for i in items)
    item = schema_object({
        "id": {"type": "string", "enum": [i["id"] for i in items]},
        "points": {"type": "integer"},
        "reason": {"type": "string"},
    })
    result = llm.json(
        f"Score this LinkedIn profile as a strict recruiter hiring for: {target}.\n"
        "Most first-pass profiles score 30-45 out of 100; a generous score helps nobody. For each rubric "
        "item give integer points from 0 to its max and one short reason naming what is missing "
        "(or 'full marks').\n\n"
        f"Rubric:\n{rubric}\n\n<profile>\n{profile_text}\n</profile>",
        schema_object({"items": {"type": "array", "items": item}}))
    scores = {i["id"]: {"points": 0, "reason": "not scored"} for i in items}
    for r in result["items"]:
        if r["id"] in scores:
            scores[r["id"]] = {"points": max(0, min(r["points"], MAX_POINTS[r["id"]])), "reason": r["reason"]}
    return scores


def rewrite(llm, profile_text: str, scores: dict, target: str, keep_words=()) -> dict:
    lost = "\n".join(f"- {k}: {v['points']}/{MAX_POINTS[k]}, {v['reason']}" for k, v in scores.items())
    result = llm.json(
        f"Rewrite the parts of my LinkedIn profile that lost points. I am targeting: {target}.\n"
        "Use only facts from my profile below and my career profile; where a number would help and "
        "I have not given one, write {{your number}}.\n"
        "- headline_options: 3 different headlines, each at most 220 characters: target role and specialty "
        "in the words recruiters search for, one proof point, what I am looking for or what I am known for.\n"
        "- about_open: the first two lines, at most 300 characters, no 'passionate', not opening with my name.\n"
        "- about_body: the rest of the About section, first person, at most 1,400 characters, ending with "
        "the roles I want and how to reach me.\n"
        "- current_role: one scope line, then 2-3 outcome bullets starting with '- '.\n"
        "- top_skills: the 3 skills to pin first.\n"
        "- settings_to_fix: short to-dos for what lost points but is not text (featured, Open to Work, "
        "photo, banner, recommendations, activity, contact info); empty list if none.\n\n"
        f"Where points were lost:\n{lost}\n\n<profile>\n{profile_text}\n</profile>",
        schema_object({
            "headline_options": {"type": "array", "items": {"type": "string"}},
            "about_open": {"type": "string"},
            "about_body": {"type": "string"},
            "current_role": {"type": "string"},
            "top_skills": {"type": "array", "items": {"type": "string"}},
            "settings_to_fix": {"type": "array", "items": {"type": "string"}},
        }))
    for key in ("about_open", "about_body", "current_role"):
        result[key] = clean(result[key], keep_words)[0]
    result["headline_options"] = [_cap(clean(h, keep_words)[0], 220) for h in result["headline_options"]]
    return result


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(" |,;-")


def run(cfg, conn, llm, profile_text: str) -> tuple[Path, int, int]:
    target = ", ".join(cfg.niche.get("target_roles") or ["my target roles"])
    keep = cfg.content.get("keep_words") or []
    before = score(llm, profile_text, target)
    new = rewrite(llm, profile_text, before, target, keep)
    rewritten_text = (
        f"HEADLINE:\n{new['headline_options'][0] if new['headline_options'] else ''}\n\n"
        f"ABOUT:\n{new['about_open']}\n\n{new['about_body']}\n\n"
        f"CURRENT ROLE:\n{new['current_role']}\n\nTOP 3 SKILLS:\n{', '.join(new['top_skills'])}"
    )
    # Only the rewritten sections can change; everything else keeps its score.
    after = {**before, **score(llm, rewritten_text, target, only=REWRITTEN)}
    total_before = sum(v["points"] for v in before.values())
    total_after = sum(v["points"] for v in after.values())

    sections = {
        "headline": ("Headline (pick one)", "\n".join(f"{i}. {h}" for i, h in enumerate(new["headline_options"], 1))),
        "about_open": ("About: the first two lines", new["about_open"]),
        "about_body": ("About: the rest", new["about_body"]),
        "current_role": ("Current role", new["current_role"]),
        "skills": ("Top 3 skills to pin", ", ".join(new["top_skills"])),
    }
    order = sorted(REWRITTEN, key=lambda k: before[k]["points"] - MAX_POINTS[k])  # most points lost first
    lines = [
        f"# LinkedIn profile review, {date.today():%d %b %Y}", "",
        f"**Score: {total_before}/100 now, {total_after}/100 with the rewrites below "
        f"({total_after - total_before:+d}).**", "",
        "| Item | Now | After rewrite | Why it lost points |", "|---|---|---|---|",
        *[f"| {k} | {before[k]['points']}/{MAX_POINTS[k]} | {after[k]['points']}/{MAX_POINTS[k]} | "
          f"{before[k]['reason']} |" for k in MAX_POINTS],
        "", "Paste these in, most valuable first. Fill any {{your number}} before you do.", "",
    ]
    for n, key in enumerate(order, 1):
        title, body = sections[key]
        lines += [f"## {n}. {title}", "", body, ""]
    if new["settings_to_fix"]:
        lines += ["## Things only you can fix in LinkedIn", "", *[f"- {t}" for t in new["settings_to_fix"]], ""]
    path = cfg.output_dir / "profile-review.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    conn.execute("INSERT INTO profile_scores (at, score, rewritten_score, items) VALUES (?, ?, ?, ?)",
                 (db.now(), total_before, total_after, json.dumps({"before": before, "after": after})))
    conn.commit()
    return path, total_before, total_after
