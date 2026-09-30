# Prompts to copy-paste

Everything below is meant to be pasted into Claude Code, opened in an empty
folder on your own machine. Each prompt is self-contained. Replace the two
`<...>` placeholders before pasting. The tool repository is
`https://github.com/daniel-mend-io/followup-inbox`; if you forked it, use your
fork's address instead.

You need before you start: a GitHub account with `gh` logged in on your
machine (`gh auth login`), a Claude account with Claude Code, and Slack (plus
Jira and Gmail if you want those scanners) connected at
https://claude.ai/customize/connectors.

## 1. Set up my private inbox

> Set up a private follow-up inbox for me using the tool at
> https://github.com/daniel-mend-io/followup-inbox. Do all of this without
> asking me questions unless something fails:
>
> 1. Create a private GitHub repository called `my-inbox` under my account with
>    `gh repo create my-inbox --private`, clone it into this folder, and add the
>    tool as a git submodule at `tool/`. Create `items/`, `archive/`, `efforts/`
>    and `state/` with `.gitkeep` files, and a `.gitignore` containing
>    `rendered/`, `.env`, `__pycache__/`, `*.tmp`.
> 2. Copy `tool/config.example.yml` to `config.yml` and fill it in: my name is
>    `<YOUR NAME>`, my email is `<YOUR EMAIL>`, my Slack member id is
>    `<YOUR SLACK MEMBER ID>`, my timezone is `<YOUR IANA TIMEZONE>`, my
>    company's email domains are `<YOUR DOMAINS>`, and `git.repo_url` is the
>    repository you just created. Keep `module_path: tool`. Leave
>    `customer-calls` disabled. Leave the Gmail routine enabled only if I told
>    you Gmail is connected; otherwise set it to `enabled: false`.
> 3. Run `python3 tool/scripts/verify.py` and show me the last line.
> 4. Commit everything with the message "inbox: initial" and push to `main`.
> 5. Run `python3 tool/scripts/verify.py --commit` to prove the push path works
>    end to end, and show me the last line.
> 6. Tell me the one thing I must do myself next: install the Claude GitHub App
>    on `my-inbox` at https://github.com/apps/claude/installations/new with
>    "Only select repositories", and keep `main` unprotected.

## 2. Install the routines

Run this after step 6 above is done, in the same folder.

> Install my follow-up routines. Run `python3 tool/scripts/render.py`, then read
> `rendered/manifest.json` and create one scheduled cloud routine per entry,
> using the schedule tool: the entry's `name`, `cron_expression` and `model`,
> the `prompt` verbatim, this repository (`git.repo_url` in `config.yml`) as the
> source, the `allowed_tools` listed, and the listed connectors attached. Use my
> default cloud environment. When done, write each routine's id into
> `state/routine-ids.md` in the format that file describes, commit it with the
> message "state: routine ids" and push. Then run the commitments routine once
> and tell me the link to its run.

## 3. Use it every day

> Open my inbox: `git pull` and then `python3 tool/scripts/tui.py`.

Or, without the TUI:

> Pull my inbox repository and show me what is open, newest first, with the
> proposed reply for each ready item. For anything I tell you I have handled,
> mark it done with `python3 tool/scripts/status.py "<id>" done`, then run
> `python3 tool/scripts/sync.py -m "triage"`.

## 4. Update the tool

> Update the follow-up inbox tool: run `git submodule update --remote tool`,
> re-render the routine prompts with `python3 tool/scripts/render.py`, run
> `python3 -m unittest discover -s tool/tests`, and if the tests pass commit
> with the message "tool: update" and push. Then, for every routine whose
> rendered prompt changed, update the routine's prompt with the schedule tool
> using the ids in `state/routine-ids.md`.

## 5. Add a colleague heads-up (optional)

> Enable the customer-calls routine in my inbox: fill in the `colleague` block
> in `config.yml` with `<NAME>`, `<EMAIL>`, `<SLACK MEMBER ID>` and
> `<TIMEZONE>`, set `enabled: true` on the `customer-calls` routine, render,
> install that routine with the schedule tool, record its id in
> `state/routine-ids.md`, commit and push.

## What each prompt assumes

- The tool is public, so the cloud can fetch the `tool/` submodule without a
  credential. Your inbox repository is private and must stay private.
- Routines commit as you, using `user.name` / `user.email` from `config.yml`.
  The cloud refuses pushes of branches with commits by anyone else.
- Nothing is ever sent to another person. Every routine writes drafts and DMs
  only you.
