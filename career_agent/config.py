"""Loads config/profile.yaml and .env, and knows where every file lives."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, root: Path | str | None = None, data: dict | None = None):
        self.root = Path(root or os.getenv("CAREER_AGENT_HOME") or Path.cwd()).resolve()
        load_dotenv(self.root / ".env")
        if data is None:
            path = self.root / "config" / "profile.yaml"
            if not path.exists():
                raise ConfigError(
                    f"{path} not found. Run `python -m career_agent init` and fill it in."
                )
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.data: dict[str, Any] = data

    # ---- paths -------------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self._dir("data")

    @property
    def output_dir(self) -> Path:
        return self._dir("output")

    @property
    def inbox_dir(self) -> Path:
        return self._dir("inbox")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def linkedin_token_path(self) -> Path:
        return self.data_dir / "linkedin_token.json"

    def _dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- sections ------------------------------------------------------------
    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    @property
    def me(self) -> dict:
        return self.section("me")

    @property
    def niche(self) -> dict:
        return self.section("niche")

    @property
    def content(self) -> dict:
        return self.section("content")

    @property
    def outreach(self) -> dict:
        return self.section("outreach")

    @property
    def jobs(self) -> dict:
        return self.section("jobs")

    @property
    def tracking(self) -> dict:
        return self.section("tracking")

    # ---- secrets ---------------------------------------------------------------
    @staticmethod
    def env(key: str, default: str | None = None, required: bool = False) -> str | None:
        value = os.getenv(key) or default
        if required and not value:
            raise ConfigError(f"{key} is not set. Add it to your .env file.")
        return value
