from career_agent import humanize
from career_agent.humanize import clean, polish, score, structural_tells
from tests.conftest import FakeLLM

HUMAN = (
    "Last March our weekly revenue dashboard said 4.2 crore. Finance said 3.9. Nobody trusted either.\n\n"
    "I spent two days on it. The bug was dumb: refunds were joined on order date, not refund date, so "
    "every refund in the last week of a month landed in the next one.\n\n"
    "We fixed one join. The numbers matched to the rupee. My manager stopped exporting to Excel.\n\n"
    "I've learned that a dashboard nobody trusts is worse than no dashboard, because people still make "
    "decisions with it. They just make them quietly. Where do your numbers disagree?"
)
SLOP = (
    "In today's fast-paced world, data is crucial. We leverage robust, seamless and innovative tools "
    "to unlock transformative insights. Moreover, it is worth noting that a holistic approach is vital. "
    "Furthermore, our comprehensive ecosystem fosters synergy. Additionally, cutting-edge analytics "
    "elevate outcomes. Ultimately, the journey is a testament to meticulous work. It's not just data, "
    "it's a story. Let that sink in. What are your thoughts?"
)


def test_clean_fixes_typography_vocabulary_and_keeps_urls():
    text, report = clean("Let's face it, we leverage robust tools — seamlessly. See https://x.io/leverage")
    assert text == "We use solid tools, cleanly. See https://x.io/leverage"
    assert report["removed"] == 5


def test_clean_keeps_domain_terms_and_keep_words():
    text, _ = clean("Customer journey drop-offs and financial leverage are robust signals.", keep_words=["robust"])
    assert text == "Customer journey drop-offs and financial leverage are robust signals."


def test_clean_keeps_joiners_that_scripts_and_emoji_need():
    devanagari = "क्‍ष और क्‌ष"
    emoji = "👨‍💻 ❤️‍🔥 🏴\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F"
    text, report = clean(f"{devanagari} {emoji} zero​width space")
    assert text == f"{devanagari} {emoji} zerowidth space"
    assert report["removed"] == 2


def test_closing_bait_is_removed_even_at_the_end():
    assert clean("Shipped it. What are your thoughts?")[0] == "Shipped it."


def test_structural_tells():
    names = [t["name"] for t in structural_tells("It's not just a dashboard, it's a habit.")]
    assert any("not just" in n for n in names)


def test_score_separates_human_from_slop():
    assert score("Too short to judge.") is None
    human, slop = score(HUMAN), score(clean(SLOP)[0] + " " + SLOP)
    assert human["score"] > slop["score"] + 25
    assert slop["verdict"] == "FLAGGED"


def test_polish_rewrites_posts_with_tells_and_keeps_the_better_version():
    llm = FakeLLM(lambda prompt, schema: {"text": HUMAN})
    result = polish(SLOP, llm, "post")
    assert result.rewritten and result.text == HUMAN.strip() and "Revise this LinkedIn post" in llm.prompts[0]

    long_slop = SLOP + " " + SLOP
    worse = FakeLLM(lambda prompt, schema: {"text": long_slop + " Not only fast but also cheap. Here's what we learned."})
    kept = polish(long_slop, worse, "post")
    assert not kept.rewritten and kept.text == clean(long_slop)[0] and len(worse.prompts) == 1


def test_polish_does_not_call_claude_for_clean_posts_or_short_texts():
    llm = FakeLLM(lambda prompt, schema: {"text": "x"})
    assert polish(HUMAN, llm, "post").text == HUMAN.strip()
    assert polish("It's not just X, it's Y.", llm, "comment").rewritten is False
    assert llm.prompts == []


def test_style_rules_list_the_lexicon():
    rules = humanize.style_rules()
    assert "delve" in rules and "em dash" in rules
