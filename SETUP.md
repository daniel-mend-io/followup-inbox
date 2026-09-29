# Setup

The checklist. If any step is unclear, [GETTING-STARTED.md](GETTING-STARTED.md)
explains the same steps in plain words.

The steps only you can do. There is no database, no hosted service and no
secret to provision: the routines reach Slack, Jira and Calendar through the
connectors already attached to your Claude account.

Two repositories: this module (public, never holds your data) and your
instance (private, holds everything else). You create the instance.

## 1. Make the instance repository, and make it private

Item bodies quote your real messages, and `config.yml` names your colleagues.
The instance must be private. Create an empty private repository on GitHub
(or wherever your Claude Code cloud environment can clone from), then:

```
mkdir my-inbox && cd my-inbox
git init -b main
git remote add origin <your private repo url>
git submodule add https://github.com/<owner>/followup-inbox tool
mkdir items archive state && touch items/.gitkeep archive/.gitkeep
printf 'rendered/\n.env\n__pycache__/\n*.tmp\n' > .gitignore
```

`tool/` is the module, pinned to the commit you added. Upgrade later with
`git submodule update --remote tool` and a commit; nothing changes under you
otherwise.

## 2. Fill in the config

```
cp tool/config.example.yml config.yml
```

Edit `config.yml`: your name, email, Slack member id, timezone, working
hours, the private repo url, which channels to skip. Keep `module_path: tool`.
The three Slack routines are enabled by default. The `customer-calls` routine
needs a named colleague; leave it disabled unless you want it.

`config.yml` is committed. The instance is private, so this adds nothing the
item files do not already expose. Nothing secret goes in it; if a future
source needs a token, it goes in `.env`, which is gitignored.

## 3. Check the store, then commit

```
python3 tool/scripts/verify.py
git add -A && git commit -m "inbox: initial" && git push -u origin main
python3 tool/scripts/verify.py --commit
```

The second `verify` proves the whole path: write, commit, rebase, push, and
the cleanup commit. If it passes here it will pass in the cloud, provided the
cloud environment can push to the instance (step 4).

## 4. Give the cloud environment push access

The routines run in Claude Code's cloud, clone the instance, run
`git submodule update --init` (the module is public, so that needs no
credential), and commit directly to `main` of the instance. In claude.ai →
Code → settings, make sure the GitHub integration covers the instance
repository with write access, and that `main` has no branch protection that
blocks direct pushes. That GitHub grant is the one credential that matters: it
can write to every repository it covers, so scope it to the instance if you
can.

## 5. Install the routines

```
python3 tool/scripts/render.py
```

This writes one prompt per enabled routine to `rendered/` and a
`rendered/manifest.json` with each one's name, cron, model, connectors and
repo. Then either:

- tell Claude Code in the instance: *"install the routines from
  rendered/manifest.json"*. `tool/SKILL.md` tells it how: one routine per
  manifest entry, with the instance repository as the source and the listed
  connectors attached. A new account gets the three Slack routines this way; or
- create them by hand at https://claude.ai/code/routines: paste each rendered
  prompt, set the cron from the manifest, attach the connectors, add the
  instance repo as the source.

Record the routine ids in `state/routine-ids.md`. Then run one routine
manually from the routines page and check that a commit arrived. On a quiet
day it finds nothing and stays silent, so seed a test commitment in Slack
first ("I'll get back to you on this tomorrow") if you want to see a full run.

## 6. Twice a year: DST

Crons are UTC. When your clocks change, every routine fires an hour earlier or
later in local terms. `python3 tool/scripts/render.py --schedule` shows today's
local times; edit the crons in `config.yml` and in the routines UI if you care.
