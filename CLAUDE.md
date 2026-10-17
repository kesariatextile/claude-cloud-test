# Instagram automation for @riteshmodiofficial

The `ig-*` skills in `.claude/skills/` come from
https://github.com/Jakeschincariol/instagram-agent-skill (MIT, commit d03c56b).

## Where account data lives

The skills read and write `~/.claude/instagram/`. Cloud sessions wipe the home
directory, so in this repo that folder is `instagram/` at the repo root:

| skills say | use instead |
| --- | --- |
| `~/.claude/instagram/voice.md` | `instagram/voice.md` |
| `~/.claude/instagram/swipe.md` | `instagram/swipe.md` |
| `~/.claude/instagram/plan.md` | `instagram/plan.md` |
| `~/.claude/instagram/log.md` | `instagram/log.md` |

Commit changes to these files so the next session has them.

## Rules

- Nothing is posted, liked, followed or messaged automatically. Every output
  ends as a copy-ready block for the account owner to post.
- Never ask for or store the Instagram password.
- For reel scripts, also follow the `riteshmodiofficial-reels` skill, which has
  this account's own viral formula and past performance data.
