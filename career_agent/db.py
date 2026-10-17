"""SQLite storage: posts, contacts, outreach drafts, comment drafts, jobs and their history."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    theme TEXT,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',      -- draft | approved | published | rejected | failed
    linkedin_urn TEXT,
    published_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT,
    company TEXT,
    linkedin_url TEXT,
    category TEXT,                             -- HR | Recruiter | Founder | Hiring Manager | ...
    notes TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',        -- new | requested | connected | messaged | replied | not_interested
    last_action_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_url ON contacts(linkedin_url) WHERE linkedin_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS outreach_messages (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    kind TEXT NOT NULL,                        -- connect | dm | follow_up
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS comment_drafts (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    author TEXT,
    post_url TEXT,
    post_text TEXT NOT NULL,
    options TEXT NOT NULL                      -- JSON list of comment options
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,                      -- greenhouse | lever | ashby | careers_page | manual | email
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    apply_url TEXT,
    description TEXT,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    score INTEGER,
    score_reasons TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    kit_path TEXT,
    applied_at TEXT,
    last_update_at TEXT,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    origin TEXT NOT NULL                       -- agent | manual | email
);

CREATE TABLE IF NOT EXISTS emails_seen (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    category TEXT,
    job_id INTEGER
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()
