"""Make drafts read like a person wrote them, and score how machine-written they look.

Adapted from linkedin-agent-skill by Jake Schincariol (MIT, see
data/THIRD_PARTY_NOTICE.txt). Changes from the original:
- zero-width joiners that Hindi/Devanagari and emoji sequences need are kept
- domain terms that contain a lexicon word ("customer journey", "financial
  leverage") and words listed in `content.keep_words` are never rewritten
- `polish()` closes the loop: structural tells and a weak score go back to
  Claude for one targeted rewrite instead of being left to the user
"""

from __future__ import annotations

import json
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

LEXICON = json.loads((Path(__file__).parent / "data" / "slop.json").read_text(encoding="utf-8"))
# Phrases that contain a lexicon word but are real terms in analytics, finance and tech.
PROTECTED = ["customer journey", "user journey", "buyer journey", "candidate journey", "financial leverage",
             "operating leverage", "leverage ratio", "robust regression", "robust standard errors"]
MIN_WORDS_TO_SCORE = 60  # below this the detection checks are noise

_URL = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+")
_SENT = re.compile(r"[^.!?\n]+[.!?]*")
_WORD = re.compile(r"[A-Za-z']+")
_CONTRACTIONS = re.compile(r"\b\w+'(?:s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
_PRONOUNS = re.compile(r"\b(i|me|my|mine|we|us|our|you|your)\b", re.IGNORECASE)
_NUMBERS = re.compile(r"\b\d[\d,.]*%?\b|\$\d|₹\d")
_PROPER = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]{2,}\b", re.MULTILINE)
CHECKS = ["BURSTINESS", "SPECIFICITY", "SLOP DENSITY", "FINGERPRINT", "VOICE"]


# ---- cleaning -------------------------------------------------------------------

def _invisible_table():
    single, ranges = {}, []
    for e in LEXICON["invisible"]:
        if "-" in e["cp"]:
            a, b = e["cp"].split("-")
            ranges.append((int(a[2:], 16), int(b[2:], 16), e))
        else:
            single[int(e["cp"][2:], 16)] = e
    return single, ranges


_SINGLE, _RANGES = _invisible_table()


def _joinable(ch: str) -> bool:
    cp = ord(ch)
    return (0x0900 <= cp <= 0x0DFF                      # Devanagari ... Sinhala
            or unicodedata.category(ch) in ("So", "Sk")  # emoji and symbols
            or 0x1F3FB <= cp <= 0x1F3FF or cp == 0xFE0F)  # skin tones, emoji presentation


def _needed(text: str, i: int) -> bool:
    """Is the invisible character at i part of a script or emoji sequence?"""
    cp = ord(text[i])
    if cp in (0x200C, 0x200D):
        return 0 < i < len(text) - 1 and _joinable(text[i - 1]) and _joinable(text[i + 1])
    if 0xE0020 <= cp <= 0xE007F:  # tag characters inside a subdivision flag like 🏴󠁧󠁢󠁳󠁣󠁴󠁿
        j = i
        while j > 0 and 0xE0020 <= ord(text[j - 1]) <= 0xE007F:
            j -= 1
        return j > 0 and ord(text[j - 1]) == 0x1F3F4
    return False


def _strip_invisible(text: str):
    hits: Counter = Counter()
    out = []
    for i, ch in enumerate(text):
        cp = ord(ch)
        entry = _SINGLE.get(cp) or next((e for a, b, e in _RANGES if a <= cp <= b), None)
        if entry is None and unicodedata.category(ch) == "Cf":
            entry = {"cp": f"U+{cp:04X}", "name": "invisible format character", "action": "delete"}
        if entry is None or _needed(text, i):
            out.append(ch)
            continue
        hits[f"{entry['cp']} {entry['name']}"] += 1
        out.append("" if entry["action"] == "delete" else " ")
    return "".join(out), [{"name": k, "count": v} for k, v in hits.items()]


def _typography(text: str):
    hits = []
    for entry in LEXICON["typographic"]:
        ch = entry["from"]
        n = text.count(ch)
        if not n:
            continue
        hits.append({"name": f"{ch} {entry['name']}", "count": n})
        if ch == "—":
            text = re.sub(r"\s*—\s*", ", ", text)
        elif ch == "–":
            text = re.sub(r"\s*–\s*(?=\d)", "-", text)   # 5–10 -> 5-10
            text = re.sub(r"\s+–\s+", ", ", text)         # used as a dash
            text = text.replace("–", "-")
        else:
            text = text.replace(ch, entry["to"])
    text = re.sub(r",\s*([,.;:!?])", r"\1", text)
    text = re.sub(r",[ \t]*\n", "\n", text)
    return text, hits


def _match_case(src: str, repl: str) -> str:
    if not repl:
        return repl
    if src.isupper() and len(src) > 1:
        return repl.upper()
    return repl[0].upper() + repl[1:] if src[0].isupper() else repl


def _phrase(phrase: str) -> str:
    # Lookarounds instead of \b so phrases ending in "?" still match at the end of a line.
    return r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"


def _pattern(phrase: str) -> re.Pattern:
    return re.compile(_phrase(phrase), re.IGNORECASE)


def _delete(m: re.Match) -> str:
    """Drop a stock phrase; if it opened a sentence, capitalise what now opens it."""
    before = m.string[:m.start()].rstrip(" \t")
    nxt = m.group(1) or ""
    return nxt.upper() if before == "" or before[-1] in ".!?\n" else nxt


def _lexical(text: str, keep: set[str]):
    hits = []
    entries = sorted(LEXICON["phrases"] + LEXICON["words"], key=lambda e: len(e["find"]), reverse=True)
    for entry in entries:
        if entry["find"].lower() in keep:
            continue
        if entry["replace"]:
            pattern = _pattern(entry["find"])
            repl = lambda m, r=entry["replace"]: _match_case(m.group(0), r)  # noqa: E731
        else:
            pattern = re.compile(_phrase(entry["find"]) + r"[,:!]?[ \t]*([a-z])?", re.IGNORECASE)
            repl = _delete
        text, n = pattern.subn(repl, text)
        if n:
            hits.append({"find": entry["find"], "replace": entry["replace"] or "(deleted)", "count": n})
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?m)^[ \t]*([,.;:])\s*", "", text)
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    text = re.sub(r"(?m)^[ \t]+$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r",\s*(also|so|still|basically|in the end)\s*,\s*",
                  lambda m: ". " + m.group(1)[0].upper() + m.group(1)[1:] + ", ", text)
    return text, hits


def structural_tells(text: str) -> list[dict]:
    flags = []
    for s in LEXICON["structures"]:
        found = re.findall(s["regex"], text, re.MULTILINE)
        if found:
            flags.append({"name": s["name"], "count": len(found), "fix": s["fix"]})
    lens = [len(s.split()) for s in _SENT.findall(text) if len(s.split()) > 2]
    if len(lens) >= 4:
        mean = statistics.mean(lens)
        cv = statistics.pstdev(lens) / mean if mean else 0
        if cv < 0.35:
            flags.append({"name": f"Uniform sentence length (variation {cv:.2f})", "count": len(lens),
                          "fix": "Break one sentence in half. Let another run long."})
    return flags


def clean(text: str, keep_words=()) -> tuple[str, dict]:
    """Remove invisible characters, AI typography and stock phrases. Returns (text, report)."""
    keep = {w.lower() for w in keep_words}
    stash: list[str] = []

    def hide(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    text = _URL.sub(hide, text)
    for phrase in PROTECTED + sorted(keep, key=len, reverse=True):
        text = _pattern(phrase).sub(hide, text)
    text, invisible = _strip_invisible(text)
    text, typography = _typography(text)
    text, lexical = _lexical(text, keep)
    text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text).strip()
    report = {"invisible": invisible, "typography": typography, "lexical": lexical,
              "structures": structural_tells(text)}
    report["removed"] = sum(h["count"] for k in ("invisible", "typography", "lexical") for h in report[k])
    return text, report


# ---- detection panel ---------------------------------------------------------------

def _scale(value: float, human: float, machine: float) -> float:
    return max(0.0, min(100.0, (value - machine) / (human - machine) * 100))


def score(text: str) -> dict | None:
    """Five local heuristics (not a detector API), 0-100, higher reads more human.
    Returns None for texts too short to judge."""
    words = _WORD.findall(text)
    if len(words) < MIN_WORDS_TO_SCORE:
        return None
    per100 = 100 / len(words)

    lens = [len(s.split()) for s in _SENT.findall(text) if len(s.split()) > 2]
    cv = statistics.pstdev(lens) / statistics.mean(lens) if len(lens) >= 4 else None
    burst = _scale(cv, 0.70, 0.22) if cv is not None else 50.0

    concrete = (len(_NUMBERS.findall(text)) + len(set(_PROPER.findall(text)))) * per100
    slop_hits = sum(len(_pattern(e["find"]).findall(text)) for e in LEXICON["words"] + LEXICON["phrases"])
    invisible = sum(unicodedata.category(c) == "Cf" and not _needed(text, i) for i, c in enumerate(text))
    marks = (invisible * 4 + text.count("—") * 2
             + sum(text.count(c) for c in "‘’“”…   "))
    tells = sum(len(re.findall(s["regex"], text, re.MULTILINE)) for s in LEXICON["structures"])
    voice = (_scale(len(_CONTRACTIONS.findall(text)) * per100, 3.0, 0.0) * 0.35
             + _scale(len(_PRONOUNS.findall(text)) * per100, 8.0, 1.0) * 0.35
             + max(0.0, 100 - tells * 22) * 0.30)

    checks = {
        "BURSTINESS": burst,
        "SPECIFICITY": _scale(concrete, 6.0, 0.5),
        "SLOP DENSITY": _scale(slop_hits * per100, 0.0, 4.0),
        "FINGERPRINT": _scale(marks * 1000 / max(len(text), 1), 0.0, 12.0),
        "VOICE": voice,
    }
    overall = statistics.mean(checks.values()) * 0.6 + min(checks.values()) * 0.4
    verdict = "PASS" if overall >= 70 and min(checks.values()) >= 55 else "REVIEW" if overall >= 50 else "FLAGGED"
    return {"checks": {k: round(v, 1) for k, v in checks.items()}, "score": round(overall, 1),
            "verdict": verdict, "weakest": min(checks, key=checks.get)}


# ---- closed loop ---------------------------------------------------------------------

WEAKEST_ADVICE = {
    "BURSTINESS": "Vary sentence length a lot: some 3-word sentences, some long ones.",
    "SPECIFICITY": "Add concrete names, tools and numbers that are already in the facts you were given "
                   "(never invent new ones; use {{your number}} where a number is missing).",
    "SLOP DENSITY": "Replace stock words with plain ones.",
    "FINGERPRINT": "Use plain punctuation: no em dashes, curly quotes or ellipsis characters.",
    "VOICE": "Write in first person with contractions, like you talk.",
}


@dataclass
class Polished:
    text: str
    removed: int = 0
    tells: list = field(default_factory=list)
    score: dict | None = None
    rewritten: bool = False


def polish(text: str, llm=None, kind: str = "post", keep_words=()) -> Polished:
    """Clean the draft; for posts with structural tells or a non-PASS score,
    ask Claude for one targeted rewrite and keep whichever version scores better."""
    cleaned, report = clean(text, keep_words)
    best = Polished(cleaned, report["removed"], report["structures"], score(cleaned))
    if llm is None or kind != "post":
        return best
    if not best.tells and (best.score is None or best.score["verdict"] == "PASS"):
        return best

    issues = [f"- {t['name']}: {t['fix']}" for t in best.tells]
    if best.score and best.score["verdict"] != "PASS":
        issues.append(f"- {WEAKEST_ADVICE[best.score['weakest']]}")
    result = llm.json(
        f"Revise this LinkedIn {kind} so it reads like a person wrote it. Fix only these issues:\n"
        + "\n".join(issues)
        + "\nKeep the meaning, every fact, the first line's idea, the hashtags and any {{placeholders}}. "
        "Do not add facts. Keep roughly the same length.\n\n<draft>\n" + cleaned + "\n</draft>",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
         "additionalProperties": False},
        effort="low")
    text2, report2 = clean(result["text"], keep_words)
    candidate = Polished(text2, best.removed + report2["removed"], report2["structures"], score(text2), True)

    if best.score and candidate.score:
        improved = (candidate.score["score"] - 10 * len(candidate.tells)) > (best.score["score"] - 10 * len(best.tells))
    else:
        improved = len(candidate.tells) < len(best.tells)
    return candidate if improved else best


def style_rules() -> str:
    """Writing rules for the system prompt, so drafts avoid the tells up front."""
    banned = sorted({e["find"] for e in LEXICON["words"] + LEXICON["phrases"]})
    shapes = [s["name"] for s in LEXICON["structures"]]
    return (
        "Writing style for everything you write:\n"
        "- No em dashes, en dashes as dashes, curly quotes or the ellipsis character.\n"
        "- Vary sentence length; use contractions and first person.\n"
        f"- Never use these words or phrases: {', '.join(banned)}.\n"
        f"- Avoid these shapes: {'; '.join(shapes)}."
    )
