# Career Agent

A personal AI agent for an active job search. It posts on your LinkedIn, prepares
messages and comments for HRs and founders in your niche, finds and scores jobs,
prepares a ready-to-submit kit for each application, and tracks every
application's status from your inbox in one report.

> **Hinglish summary:** Ye agent roz aapki LinkedIn post likhta hai (aur approve
> karne par khud publish karta hai), 10-15 HR/Founders ke liye personalised
> messages aur unke posts par opinion wale comments draft karta hai, jobs dhoond kar
> match score deta hai, har job ke liye cover letter + form answers tayaar karta hai,
> aur aapke email se pata karta hai ki kis application ka kya hua (interview,
> rejection, offer). Sab ek daily to-do list aur ek HTML report mein milta hai.
> Har post, comment aur message ek "humanizer" se guzarta hai jo AI wale shabd
> aur nishaan hata kar 0-100 human score deta hai, aur `profile` command aapki
> LinkedIn profile ko recruiter ki nazar se 100 mein se score karke rewrite karta hai.

## What runs automatically and what stays with you

| Task | What the agent does | What you do |
|---|---|---|
| Daily LinkedIn post | Writes it from your real stories on the best of 21 hook formulas, humanizes and scores it, and publishes through LinkedIn's official API | Approve it (`post approve <id>`), or set `require_approval: false` for full auto |
| 10-15 HR / founder messages | Finds people (web search) or imports your list, picks who to contact today, writes the invite note, first message and exactly two follow-ups, and flags messages with no specific reason behind them | Paste and send (~5 min), then `contacts mark <id> requested` |
| Comments on their posts | Writes 2 comment options of different types (out of 9) per post, recommends one, warns if you comment on the same person too often | Paste the posts you want to engage with into `inbox/posts_to_comment.md`, then post a comment |
| Profile | Scores your profile out of 100 the way a recruiter reads it, rewrites the sections that lose points, and re-scores the rewrite | Paste your profile into `inbox/profile.md`, then paste the rewrites into LinkedIn |
| Job applications | Finds jobs (Greenhouse, Lever, Ashby boards, any careers page via Firecrawl), scores the fit, writes a cover letter, tailored resume bullets, form answers and a referral message | Open the apply link and submit (~2 min per job) |
| Progress report | Reads job emails, updates each application (applied, assessment, interview, rejected, offer), flags follow-ups | Read `output/report.html` |

### Why messages, comments and the final "Submit" are not fully automatic

- **LinkedIn bans bots.** Its User Agreement prohibits automated messaging,
  commenting and scraping, and accounts that do it get restricted. LinkedIn only
  offers APIs for posting to your own profile, which this agent uses. A
  restricted account would hurt your job search far more than 10 minutes a day
  of copy-paste.
- **Firecrawl cannot scrape LinkedIn** (LinkedIn blocks it, and doing so breaks
  their terms). The agent uses Firecrawl for company careers pages instead.
- **Application forms have CAPTCHAs and custom questions**, and recruiters spot
  mass auto-applies. The agent does all the writing; you click Submit.

## How the writing stays human

Every post, comment, message and profile rewrite goes through `career_agent/humanize.py`:

1. **Prevention:** Claude is told up front which stock words ("leverage",
   "delve", "seamless"...), punctuation and sentence shapes to avoid.
2. **Cleaning:** invisible characters, em dashes, curly quotes and 113 stock
   words and phrases are replaced. Hindi/Devanagari text and emoji keep the
   zero-width joiners they need, and real terms like "customer journey" or
   "financial leverage" are never touched. Add your own words to
   `content.keep_words`.
3. **Scoring:** posts get a 0-100 human score from five checks (sentence-length
   variety, specificity, stock-word density, typographic fingerprint, voice).
   These are local heuristics, not GPTZero or any other detector.
4. **One targeted rewrite:** if a post still has a structural tell ("It's not
   just X, it's Y", rule-of-three lists, uniform sentence length) or doesn't
   pass, Claude rewrites only those issues and the better-scoring version is kept.

Posts never invent numbers. Where a number would help and you haven't given
one, the draft says `{{your number}}`, and such a draft can't be approved or
published until you fill it in.

Try it on any text: `python -m career_agent humanize draft.txt`.

## Setup (about 30 minutes, once)

Needs Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m career_agent init          # creates config/profile.yaml, .env, inbox file
```

1. **Fill `config/profile.yaml`**: your details, target roles, locations, real
   achievements, story bank and resume text. The agent never invents facts, so the
   more real detail you give, the better it writes.
2. **Claude API key**: create one at <https://console.anthropic.com/settings/keys>
   and put it in `.env` as `ANTHROPIC_API_KEY`.
3. **LinkedIn app** (for posting):
   1. Go to <https://www.linkedin.com/developers/apps> and click *Create app*
      (LinkedIn asks you to link a Company Page. You can create a simple page for yourself).
   2. On the *Products* tab, add **Share on LinkedIn** and **Sign In with
      LinkedIn using OpenID Connect**.
   3. On the *Auth* tab, add `http://localhost:8765/callback` under
      *Authorized redirect URLs*, and copy the Client ID and Client Secret into `.env`.
   4. Run `python -m career_agent linkedin-auth` and sign in. The connection
      lasts about 60 days. The report warns you before it expires.
4. **Job sources** in `config/profile.yaml` → `jobs`: add company board slugs
   (from URLs like `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`,
   `jobs.ashbyhq.com/<slug>`) and any careers page URLs. Careers pages need a
   Firecrawl key (<https://firecrawl.dev>) in `.env`.
5. **Email tracking** (Gmail): turn on 2-step verification, create an App
   Password at <https://myaccount.google.com/apppasswords>, and put your address
   and that password in `.env`. The connection is read-only. Only job-related
   emails (by keyword) are sent to Claude, and only their first ~3,000 characters.
6. **Contacts**: `python -m career_agent contacts import my_list.csv` (columns as in
   `data/contacts.example.csv`) and/or `python -m career_agent contacts discover --count 20`.
   Add a note per contact (a post of theirs, a launch, an opening): messages with a
   specific reason get replies, and the digest flags the ones without one.
7. **Profile check**: paste your profile into `inbox/profile.md` (the template
   shows what to copy) and run `python -m career_agent profile`. The review lands
   in `output/profile-review.md`: the score per item, rewrites in the order that
   gains the most points, and the settings only you can change (Open to Work,
   featured, banner). Re-run it after you update the profile.

## Daily use

```bash
python -m career_agent daily
```

This runs every step and prints today's plan (also saved in `output/digests/`):

1. the post (published, or waiting for your approval)
2. messages to send, each with the profile link and the command to mark it sent
3. comment options for the posts you pasted in the inbox
4. jobs with a ready kit, each with its apply link
5. application updates found in your email, plus follow-ups due

Open `output/report.html` for the full picture: applications, response and
interview rates, where every job stands, recent updates, posts and outreach.
Set `email_digest: true` in the profile to get the plan and report in your inbox.

### Run it every morning

macOS / Linux (`crontab -e`), every day at 8:30:

```
30 8 * * * cd /path/to/career-agent && .venv/bin/python -m career_agent daily --email >> data/agent.log 2>&1
```

Windows: Task Scheduler → Create Basic Task → Daily → *Start a program*:
`C:\path\to\career-agent\.venv\Scripts\python.exe` with arguments
`-m career_agent daily --email` and *Start in* `C:\path\to\career-agent`.

The computer must be on at that time. If it isn't, run `daily` when you start work.

## Command reference

| Command | What it does |
|---|---|
| `post generate [--theme "..."]` | Draft a post now |
| `post list` / `post show <id>` | See drafts and published posts (`show` includes the hooks considered and the human score) |
| `post edit <id> --file new.txt` | Replace a draft's text with your edit |
| `post approve <id>` / `post reject <id>` | Approve for the next run / discard |
| `post publish <id>` | Publish right now |
| `contacts import <csv>` / `contacts discover [--count N]` | Add HRs / founders |
| `contacts list [--status S]` | Show contacts |
| `contacts mark <id> <status>` | `requested`, `connected`, `messaged`, `replied`, `not_interested` |
| `outreach [--limit N]` | Draft today's messages |
| `comment` / `comment --text "..." --author "..."` | Draft comments (inbox file or one post) |
| `jobs scan` | Fetch, filter and score new jobs |
| `jobs add <url> [--jd-file jd.txt]` | Add one job (use `--jd-file` for LinkedIn job links) |
| `jobs list [--status S]` | Show tracked jobs |
| `jobs kit <id>` | Write the application kit for a job |
| `jobs mark <id> <status> [--note ...]` | e.g. `applied`, `interview`, `rejected`, `offer`, `withdrawn` |
| `track` | Read job emails and update statuses |
| `profile [--file profile.txt]` | Score your LinkedIn profile out of 100 and rewrite what loses points |
| `humanize [file] [--check]` | Clean any text of AI tells and score it |
| `report` | Rebuild `output/report.html` |

All commands are run as `python -m career_agent <command>`.

## Costs

- Claude API: roughly $0.50-2 a day on the default model (`claude-opus-5-5`),
  mostly from job scoring. The amount grows with the number of job boards. Set
  `CLAUDE_MODEL=claude-sonnet-5-5` in `.env` to roughly halve it.
- Firecrawl: each careers page or job page scrape uses credits. Board APIs
  (Greenhouse, Lever, Ashby) are free.
- LinkedIn API and Gmail: free.

## Your data

Everything stays on your computer in `data/agent.db` (SQLite), `output/` and
`inbox/`, which are git-ignored together with `.env` and `config/profile.yaml`.
Text is sent only to the Claude API (for writing and classifying), Firecrawl (the
careers page URLs you configure) and LinkedIn (your posts).

## Messaging cadence

Per contact: an invite note that asks for nothing, a first message after they
accept that gives something before it asks, a follow-up after 4 days with
something new, a last message 6 days later that closes the loop, then nothing.
The daily limit is capped at 20 because LinkedIn throttles accounts that send
more invites than that.

## Credits

The humanizer lexicon and checks, the 21 hook formulas, the comment types, the
messaging cadence and the profile rubric are adapted from
[linkedin-agent-skill](https://github.com/Jakeschincariol/linkedin-agent-skill)
by Jake Schincariol (MIT, see `career_agent/data/THIRD_PARTY_NOTICE.txt`). The
profile rubric was reworked for job seekers.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests mock every external service (Claude, LinkedIn, the job boards, Firecrawl, email).
