"""Thin wrapper around the Claude API used by every step of the agent."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic
import yaml

from .humanize import style_rules

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side fallback: if the model declines a request for policy reasons, the
# API retries it on Anthropic's recommended fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def profile_system_prompt(cfg) -> str:
    """Stable system prompt describing the user. Kept byte-identical between
    calls so prompt caching can reuse it."""
    profile = yaml.safe_dump(
        {"me": cfg.me, "niche": cfg.niche}, sort_keys=True, allow_unicode=True
    )
    return (
        "You are a career assistant working for the job seeker described below. "
        "You write LinkedIn content, outreach messages and job applications in their voice.\n\n"
        "Hard rules:\n"
        "- Never invent experience, employers, numbers, metrics, degrees or stories. Use only "
        "facts present in the profile. If a fact is missing, write around it.\n"
        "- Sound like a real, thoughtful professional, not a marketer. No buzzword salad, no "
        "fake enthusiasm, no 'I hope this message finds you well'.\n"
        "- LinkedIn does not render Markdown, so write plain text (no **bold**, no # headings).\n\n"
        f"{style_rules()}\n\n"
        f"<profile>\n{profile}</profile>"
    )


class LLM:
    def __init__(self, cfg=None, client: anthropic.Anthropic | None = None, model: str | None = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or os.getenv("CLAUDE_MODEL") or DEFAULT_MODEL
        self.system = profile_system_prompt(cfg) if cfg is not None else ""

    def _create(self, messages: list, effort: str, max_tokens: int, output_config: dict | None = None, **extra):
        if self.system:
            extra["system"] = [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}]
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=messages,
                output_config={"effort": effort, **(output_config or {})},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **extra,
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API key is invalid. Check ANTHROPIC_API_KEY in .env") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API (network error)") from e
        except TypeError as e:
            if "authentication" not in str(e):
                raise
            raise LLMError("No Claude credentials. Set ANTHROPIC_API_KEY in .env") from e
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off (max_tokens reached)")
        return response

    def json(self, prompt: str, schema: dict, effort: str = "medium", max_tokens: int = 16000) -> Any:
        """Ask for an answer that matches `schema` (JSON Schema) and return it parsed."""
        response = self._create(
            [{"role": "user", "content": prompt}],
            effort,
            max_tokens,
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def search_and_save(self, prompt: str, schema: dict, max_searches: int = 8, effort: str = "medium") -> Any:
        """Let Claude research with web search, then return what it passes to the
        `save_results` tool (validated against `schema`)."""
        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches},
            {
                "name": "save_results",
                "description": "Save the final research results. Call exactly once, when done searching.",
                "strict": True,
                "input_schema": schema,
            },
        ]
        messages: list = [{"role": "user", "content": prompt + "\n\nWhen finished, call save_results."}]
        for _ in range(6):
            response = self._create(messages, effort, 16000, tools=tools)
            for block in response.content:
                if block.type == "tool_use" and block.name == "save_results":
                    return block.input
            if response.stop_reason != "pause_turn":
                break
            # Long server-side tool runs pause; resend to let Claude continue.
            messages.append({"role": "assistant", "content": response.content})
        raise LLMError("Claude finished researching without saving results")


def schema_object(properties: dict, required: list[str] | None = None) -> dict:
    """JSON Schema object with all fields required and no extras (needed for strict output)."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }
