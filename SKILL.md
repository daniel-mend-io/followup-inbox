---
name: followup-inbox
description: Operate the follow-up inbox store — add items found in Slack/Jira/email/calendar, list what is open, draft replies, change status, regenerate INBOX.md and commit safely. Use when a routine or a person needs to read or write inbox items.
---

# followup-inbox — agent contract

The store is the instance git repository: `config.yml`, `items/` (open),
`archive/YYYY-MM/` (closed) and a generated `INBOX.md`. This module is usually
vendored into it as the `tool/` submodule, so from the instance root every
command is `python3 tool/scripts/<name>.py`; the examples below omit the
`tool/` prefix. If `tool/scripts` is empty, run `git submodule update --init`
first. Never edit item files or `INBOX.md` by hand; every write goes through
a script. Stdlib only; nothing to install.

The scripts find the store by walking up from the current directory to the
first one holding `config.yml` or `items/`; set `INBOX_ROOT=/path` to override.

## Read what is known

```bash
python3 scripts/list.py --all --ids                       # every id ever recorded, open or closed — the dedupe set
python3 scripts/list.py                                   # open items, newest first (new, waiting, ready)
python3 scripts/list.py --status waiting,ready --source slack --kind commitment --json
python3 scripts/list.py --min-age 7 --status waiting      # stale
python3 scripts/list.py --max-age 1                       # created in the last day
```

Statuses: `new | waiting | ready | done | dismissed`. Sources: `slack | jira |
email | calendar`. Kinds: `commitment` (the user promised), `ask` (someone
asked the user), `needs_reply` (unanswered question to the user),
`loose_thread` (conversation stopped without landing), `fyi`.
`--json` returns full records including `body`, `proposed_reply` and `path`.

## Add items (idempotent)

```bash
python3 scripts/add.py --json <<'JSON'
[{"id": "slack:C0123456789/1789739475.264789",
  "source": "slack",
  "kind": "commitment",
  "created_at": "2026-09-28T13:51:15Z",
  "title": "Send Alex the toggle behaviour write-up",
  "url": "https://example.slack.com/archives/C0123456789/p1789739475264789",
  "actor": "Alex",
  "trigger": "after the 8.5 release",
  "status": "waiting",
  "body": "> I'll write this up after the release\n\nAlex asked in #product how the toggle behaves.",
  "proposed_reply": null}]
JSON
```

One object or an array. Prints one line per item: `created`, `enriched` or
`exists`, then the path. Exit 0 in all three cases; exit 1 on a bad record
(the others still land). Flag form for a single item:
`add.py --id … --source … --kind … --created-at … --title … [--url --actor
--trigger --status --body|--body-file --proposed-reply|--proposed-reply-file --meta JSON]`.

Ids are `<source>:<source-specific key>` with no whitespace, and the prefix
must equal `source`:

| source | id | note |
|---|---|---|
| slack | `slack:<channel id>/<message ts>` | from the permalink `…/archives/<CHANNEL>/p1789739475264789` → ts `1789739475.264789` (dot before the last six digits). Thread items use the root ts. |
| calendar | `calendar:<event id>` | instance id for recurring events |
| jira | `jira:<comment id>` or `jira:<issue key>` | |
| email | `email:<message-id>` | |

`created_at` is when it happened at the source, UTC. `seen_at` is filled in.
`status` defaults to `new`; use `waiting` when a trigger is stated and not
met, `ready` when it is actionable now.

## Draft a reply

```bash
python3 scripts/draft.py "<id>" --file - <<'TEXT'
the reply, in the user's voice
TEXT
python3 scripts/draft.py "<id>" --text "one-liner"
python3 scripts/draft.py "<id>" --clear
```

Sets or replaces the `## Proposed reply` section. Refuses on closed items.

## Change status

```bash
python3 scripts/status.py "<id>" ready --note "trigger met: release 8.5 shipped 2026-10-02"
python3 scripts/status.py "<id>" done --note "answered in thread; owner is Jakub, not me"   # the resolution
python3 scripts/status.py "<id>" dismissed --note "bot alert, on-call owns these"
python3 scripts/status.py "<id>" new --reopen        # humans only
```

**Snoozing is the user's.** `snooze.py "<id>" 3|2w|4h|tomorrow|fri|next-week|next-release|2026-10-14|14.10`
hides an open item until then; `--wake` brings it back. Snoozed items are left
out of `list.py` (see them with `--snoozed`; `--all` includes them), so they
drop out of a routine's working set on their own. Routines never snooze or wake.
`next-release` reads `state/releases.json`; the efforts routine refreshes it
with `releases.py set --json` from the release calendar (`releases.calendar`).

`done` / `dismissed` move the file to `archive/YYYY-MM/`; a `--note` there is
stored as `resolution`, the user's account of how it was settled, which the
learn routine relies on. **Routines may only
move `new`/`waiting` → `ready`, with `--note`.** Everything else is the human's.

## Digest and commit

```bash
python3 scripts/digest.py                                   # rewrite INBOX.md
python3 scripts/sync.py -m "commitments: +2 new, 1 ready" items/slack__C01__1.2.md INBOX.md
python3 scripts/sync.py -m "triage"                         # default paths: items archive efforts knowledge INBOX.md
```

`sync.py` commits, pulls with rebase, resolves the store's conflicts by rule
(remote wins for items; digest regenerated), pushes, retries five times, never
forces. Non-zero exit means the commit is safe locally and the push did not
happen: report it, do not retry in a loop, do not force. With no remote it
commits locally and says so.

## Efforts

Everything the user has to keep in their head, one file each under
`efforts/`. Ids: `effort:<slug>` (manual) or `jira:<KEY>` (suggested from the
tracker, idempotent). Kinds: `epic | customer | marketing | coordination |
research | other`. Statuses: `suggested | active | paused | done | dropped`.

```bash
python3 scripts/efforts.py list                                  # open efforts
python3 scripts/efforts.py list --all --ids                      # every id ever, the dedupe set
python3 scripts/efforts.py add --title "Cap One rollout" --kind customer --next "send the plan" --due 2026-10-03
python3 scripts/efforts.py add --json <<'JSON'                   # routines: status suggested + a suggestion
[{"id":"jira:PROJ-1","kind":"epic","title":"…","url":"…","status":"suggested","body":"…","suggestion":"next step, one line"}]
JSON
python3 scripts/efforts.py suggest "jira:PROJ-1" --text "ask Maya for the beta date"   # routine-owned section
python3 scripts/efforts.py status "jira:PROJ-1" active           # human: accept (promotes the suggestion to next_action)
python3 scripts/efforts.py next "jira:PROJ-1" "write the one-pager" --due 2026-10-05
python3 scripts/efforts.py note "jira:PROJ-1" "beta slipped a week"
```

**Routines may only `add` with status `suggested` and write `suggest`.**
Status, next action and body are the human's.

## Knowledge

What closed items taught, under `knowledge/`: `voice.md`, `product.md`,
`people.md`, `process.md`, `triage.md`, `examples/` and `ledger.tsv`.

```bash
python3 scripts/knowledge.py context                        # every routine reads this before it drafts
python3 scripts/knowledge.py pending --json                 # learn routine: closed items not learned from yet
python3 scripts/knowledge.py add --json <<'JSON'
[{"topic": "voice", "key": "lead-with-answer", "text": "Lead with the answer.", "source": "slack:C01/1.2"}]
JSON
python3 scripts/knowledge.py mark "<id>" --outcome replied --sent-file - --example --note "cut the preamble" <<'TEXT'
what the user actually sent
TEXT
python3 scripts/knowledge.py mark "<id>" --outcome acted|dismissed|unknown
python3 scripts/knowledge.py drop --topic voice --key lead-with-answer
python3 scripts/knowledge.py stats                          # outcomes and draft survival per week
```

`add` prints `added`, `confirmed` (seen +1), `updated` (with the old text) or
`pinned` (the user's; unchanged). Keys are lowercase slugs; reuse a key to
confirm or refine. `mark` is once per item (`exists` after that) and only on
closed items. **Only the learn routine writes here; every other routine only
reads `context`.** The user edits the files by hand: untagged lines and
`## Pinned` entries are never changed by the script.

## TUI

```bash
python3 scripts/tui.py            # 0 routines, 1 inbox, 2 efforts; ? for keys
python3 scripts/tui.py --check    # print each screen once without curses
```

Stdlib curses. Actions go through the scripts above, so the TUI never
bypasses the store rules. `g` runs `sync.py -m triage`.

## Verify

```bash
python3 scripts/verify.py            # write, read back, close, re-add (must not re-raise), digest, delete
python3 scripts/verify.py --commit   # the same, through sync.py, proving push access
```

Ends with `ok`.

## Routines

```bash
python3 scripts/render.py            # config.yml + the module's routines/*.md → rendered/*.md + rendered/manifest.json
python3 scripts/render.py --schedule # crons with today's local times
python3 scripts/render.py --stdout loose-threads
```

**Installing them (first run on a new account).** For each entry in
`rendered/manifest.json`, create one scheduled cloud routine with:
`name`, `cron_expression`, `model`, the prompt from `prompt` (verbatim), the
repository `git_repository` as the session source, `allowed_tools`, and the
listed `connectors` attached (Slack always; Atlassian and Google-Calendar when
listed and connected). In Claude Code that is the schedule skill / `RemoteTrigger`
create call; ask the user which environment to run in. Disabled routines are
not in the manifest. Record the resulting routine ids in
`state/routine-ids.md` (see `state/INDEX.md` for the file shape).

## Common gotchas

- **Run from inside the instance**, or set `INBOX_ROOT`. The scripts walk up
  from the current directory looking for `config.yml` or `items/`; from
  elsewhere they fall back to the module's own directory and you will write
  into the wrong place.
- **Quote ids.** They contain `:` and `/`; in JSON that is fine, on a shell
  line wrap them in double quotes.
- **Do not check for the file before `add`.** `add` is idempotent and looks in
  the archive too. Use `list.py --all --ids` only to skip re-triage work.
- **`exists` on an archived path means the user closed it.** Leave it. Do not
  create a variant id to get around it.
- **Stage what you created.** Pass the paths `add.py` printed plus `INBOX.md`
  to `sync.py`, so an unrelated half-edited file in the clone is never swept
  into a routine's commit.
- **Slack ts has a dot.** `p1789739475264789` in a permalink is
  `1789739475.264789` as an id. Get it wrong and every re-scan creates a new
  item.
- **`created_at` is the source time, not now.** Sorting, ageing and the digest
  all key off it.
- **Timestamps are ISO-8601 with a zone.** `2026-09-28T13:51:15Z` or
  `…+02:00`. Naive values are read as UTC.
- **A quiet run commits nothing.** No new items, no drafts, no promotions:
  stop without `digest`/`sync`, without a notification.
- **Crons are UTC and do not follow DST.** `render.py --schedule` shows the
  local drift.
