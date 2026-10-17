import copy

import pytest

from career_agent import db
from career_agent.config import Config

PROFILE = {
    "me": {"name": "Asha Rao", "skills": ["SQL", "Python"], "resume_text": "Analyst at Acme, 3 years",
           "notice_period": "30 days", "expected_ctc": "18 LPA"},
    "niche": {"target_roles": ["Data Analyst"], "title_keywords": ["analyst"], "avoid_keywords": ["intern"],
              "locations": ["Bengaluru", "Remote"], "min_match_score": 70, "max_job_age_days": 30},
    "content": {"post_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "require_approval": True,
                "themes": ["Theme A", "Theme B"], "story_bank": ["A true story"]},
    "outreach": {"daily_limit": 3, "follow_up_after_days": 5},
    "jobs": {"daily_apply_kits": 2},
    "tracking": {},
}


class FakeLLM:
    """Stands in for career_agent.llm.LLM; `handler(prompt, schema)` returns the parsed JSON."""

    def __init__(self, handler):
        self.handler = handler
        self.prompts = []

    def json(self, prompt, schema, effort="medium", max_tokens=16000):
        self.prompts.append(prompt)
        return self.handler(prompt, schema)

    def search_and_save(self, prompt, schema, max_searches=8, effort="medium"):
        self.prompts.append(prompt)
        return self.handler(prompt, schema)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for key in ("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "FIRECRAWL_API_KEY", "LINKEDIN_API_VERSION"):
        monkeypatch.delenv(key, raising=False)
    return Config(tmp_path, data=copy.deepcopy(PROFILE))


@pytest.fixture
def conn(cfg):
    return db.connect(cfg.db_path)
