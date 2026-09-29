# Getting started, in plain words

## What this is

You promise people things in Slack all day. "I'll check and get back to you."
"I'll open a ticket after the release." Some of those slip. This tool runs a
few small robots (Claude "routines") on a schedule. They read your Slack, spot
the promises and unanswered threads, and write each one down as a little note.
You look at the list of notes, deal with them, and tick them off.

The robots never message anyone. They only write notes, draft a reply for you
to send yourself, and ping you. That is a rule, not a setting.

## Two boxes

Think of it as a game and a save file.

- **The tool** (this repository, `followup-inbox`) is the game. Anyone can
  download it. It has no personal data in it, ever.
- **Your inbox** (a repository you create, say `my-inbox`) is your save file.
  It holds your settings and your notes, which quote your real messages. It is
  **private** and it is yours.

Your inbox contains a copy of the tool in a folder called `tool/`. Git calls
that a "submodule": a pinned link to a specific version of the tool. When the
tool gets an update, you choose when to pull it in.

## What you need before you start

- A GitHub account. Both boxes live there.
- A Claude account with Claude Code, and Slack connected to it
  (https://claude.ai/customize/connectors). Jira and Google Calendar too if
  you want the robots to check tickets and meetings.
- A computer with `git` and `python3`. Macs have both. Nothing else to install.

## New user, step by step

**1. Make your private inbox on GitHub.** Go to GitHub, New repository, name it
`my-inbox`, tick **Private**, create it empty. Private matters: the notes will
quote your messages and name your colleagues.

**2. Set it up on your computer.** In a terminal:

```
git clone https://github.com/<you>/my-inbox
cd my-inbox
git submodule add https://github.com/<tool-owner>/followup-inbox tool
mkdir items archive state && touch items/.gitkeep archive/.gitkeep
printf 'rendered/\n.env\n__pycache__/\n*.tmp\n' > .gitignore
```

`items/` is where open notes go, `archive/` is where finished ones go,
`tool/` is the tool.

**3. Fill in your settings.**

```
cp tool/config.example.yml config.yml
```

Open `config.yml` in any editor. Change these five things and leave the rest:

- `user.name` and `user.email`: you.
- `user.slack_id`: in Slack, click your profile, the three dots, "Copy member
  ID". It looks like `U0123ABCDEF`.
- `timezone`: yours, like `Europe/Warsaw` or `America/New_York`.
- `git.repo_url`: your inbox's address, `https://github.com/<you>/my-inbox`.

The three Slack robots are switched on already. The fourth one, "customer
calls", is off; it needs a named colleague and most people do not want it.

**4. Check it works.**

```
python3 tool/scripts/verify.py
```

It should end with `ok`. It writes a test note, reads it back, closes it and
deletes it.

**5. Save and upload.**

```
git add -A
git commit -m "my inbox"
git push -u origin main
python3 tool/scripts/verify.py --commit
```

The second check does the same test but through git, so you know uploading
works too.

**6. Let Claude's cloud write to your inbox.** The robots run on Anthropic's
computers, not yours, so GitHub has to let them push to `my-inbox`. Open
https://github.com/apps/claude/installations/new, pick your account, choose
**Only select repositories**, tick `my-inbox`, click **Install & Authorize**.
Give it only that one repository; this is the one permission in the whole
setup that matters. (Later, to change it: https://github.com/settings/installations,
Claude, Configure.)

Two rules to know. Do not put branch protection on `main` in `my-inbox`; the
cloud refuses to push to protected branches. And the cloud refuses to push a
branch that has commits by someone other than you, so the robots commit under
your name and the email from `config.yml`. Make sure that email is one your
GitHub account knows (GitHub, Settings, Emails).

**7. Create the robots.**

```
python3 tool/scripts/render.py
```

This turns your settings into three ready-made instructions in a folder called
`rendered/`. Then open Claude Code inside `my-inbox` and say:

> install the routines from rendered/manifest.json

It creates three scheduled routines for you: two that look for promises you
made (morning and midday, then evening) and one that looks for things people
asked of you and threads that went quiet. You can also do it by hand at https://claude.ai/code/routines: new
routine, paste the text of one rendered file, set its schedule from
`rendered/manifest.json`, attach Slack, add `my-inbox` as the repository.

**8. Wait, then triage.** On the first run the robots look back 30 days. If
they find something, you get one Slack DM from yourself with a short list.
Then, in your inbox folder:

```
git pull
python3 tool/scripts/list.py                  # what is open, newest first
python3 tool/scripts/status.py "<id>" done    # I did it
python3 tool/scripts/status.py "<id>" dismissed   # not a real thing, drop it
python3 tool/scripts/sync.py -m "triage"      # save and upload your decisions
```

`INBOX.md` in the folder is the same list as a page you can read on GitHub.
Each note is a small text file you can open. A note you mark done or
dismissed is never brought back, even if the robots see the same message
again.

If a robot found nothing, it stays quiet. No DM means nothing new.

**9. Twice a year.** The schedules are in universal time. When the clocks
change, the robots run an hour earlier or later on your clock. If you care,
`python3 tool/scripts/render.py --schedule` shows the current local times and
you can adjust the schedules in `config.yml` and in the routines page.

## Sharing the tool with others

If you own the tool repository:

- Push it to GitHub as a **public** repository. It contains no personal data
  by design, and the tool's own `.gitignore` refuses `config.yml`, `items/`,
  `archive/` and `INBOX.md`, so nothing personal can slip in later.
- Tag releases (`git tag v0.2.0 && git push --tags`) so people pin a version.
- Send people the link and this file. They follow the steps above with your
  repository as `<tool-owner>/followup-inbox`. They never see your inbox; it is
  a different, private repository.
- To ship an update, commit to the tool. Users take it when they want:

```
cd my-inbox
git submodule update --remote tool
git commit -am "tool: update"
git push
```

If you are a user and you want to change the tool itself, fork it, or send a
pull request. Your inbox keeps working on the version it is pinned to.

## Where to look next

- `SETUP.md`: the same steps as a terse checklist.
- `README.md`: how the notes are stored and how several robots can write at
  once without breaking anything.
- `SKILL.md`: what the robots read before they touch your notes.
