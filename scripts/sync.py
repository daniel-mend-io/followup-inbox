#!/usr/bin/env python3
"""Commit and push inbox changes safely when other writers may be pushing too.

Usage:
  ./sync.py -m "commitments: +2"                 # stage items/ archive/ INBOX.md, commit, rebase, push
  ./sync.py -m "..." items/slack__C1__2.md       # stage only the given paths (what a routine should do)
  ./sync.py --retries 8

The concurrency model (see README, "Concurrency"):
  1. commit locally first — nothing is ever lost to a failed push
  2. `git pull --rebase` onto the remote branch
  3. resolve the only conflicts the store can produce, mechanically:
       - INBOX.md is derived: regenerate it from the merged tree
       - items/ and archive/: the version already on the remote wins
         (same rule as the store's "if the file exists, do not rewrite it";
         a human's status change is always on the remote before a routine's
         enrichment, so the human wins)
  4. regenerate the digest on top of the merged tree; commit it if it changed
  5. push; if the remote moved in between, go back to 2 — bounded retries, jittered
No force pushes, ever. If retries run out the local commits remain intact and
the script exits 1 so the caller can report it instead of retrying forever.
"""

import argparse
import os
import random
import sys
import time

from _inbox_common import DIGEST_PATH, REPO_ROOT, InboxError, die, git, is_git_repo, load_config, cfg_get, rel

DEFAULT_PATHS = ("items", "archive", "INBOX.md")


def _identity_args(config):
    """Commit as the user. Claude's cloud refuses to push a branch that carries
    commits authored by someone other than the account owner, so a bot identity
    would make every routine push fail."""
    name = (cfg_get(config, "git.author_name") or cfg_get(config, "user.name")
            or git("config", "user.name", check=False).stdout.strip() or "followup-inbox")
    email = (cfg_get(config, "git.author_email") or cfg_get(config, "user.email")
             or git("config", "user.email", check=False).stdout.strip() or "inbox@localhost")
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def _has_commits():
    return git("rev-parse", "--verify", "HEAD", check=False).returncode == 0


def _branch():
    b = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return b if b != "HEAD" else "main"


def _remote():
    out = git("remote", check=False).stdout.split()
    return "origin" if "origin" in out else (out[0] if out else None)


def _conflicted():
    return [p for p in git("diff", "--name-only", "--diff-filter=U").stdout.split("\n") if p]


def regenerate_digest():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from digest import write_digest
    write_digest()


def _resolve_rebase_conflicts(ident):
    """Apply the mechanical resolution rules. Returns True if the rebase finished."""
    for _ in range(50):  # each replayed commit may conflict in turn
        paths = _conflicted()
        if not paths:
            return True
        for p in paths:
            if p == "INBOX.md":
                git("checkout", "--ours", "--", p, check=False)  # any content; regenerated below
                regenerate_digest()
                git("add", "--", p)
            elif p.startswith(("items/", "archive/")):
                # upstream ("ours" during a rebase) wins; if upstream removed/moved it, drop ours
                if git("cat-file", "-e", f"HEAD:{p}", check=False).returncode == 0:
                    git("checkout", "HEAD", "--", p)
                    git("add", "--", p)
                else:
                    git("rm", "-q", "--cached", "--", p, check=False)
                    try:
                        os.remove(os.path.join(REPO_ROOT, p))
                    except FileNotFoundError:
                        pass
            else:
                git("rebase", "--abort", check=False)
                raise InboxError(f"conflict outside the store in {p}; rebase aborted, resolve by hand")
        cont = git(*ident, "rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
        if cont.returncode != 0:
            if "No changes" in (cont.stderr + cont.stdout) or "nothing to commit" in (cont.stderr + cont.stdout):
                git("rebase", "--skip", check=False, env={"GIT_EDITOR": "true"})
            elif _conflicted():
                continue
            else:
                git("rebase", "--abort", check=False)
                raise InboxError(f"rebase --continue failed:\n{cont.stderr.strip()}")
    git("rebase", "--abort", check=False)
    raise InboxError("too many conflicting commits; rebase aborted")


def commit_local(message, paths, ident):
    for p in paths:
        if os.path.exists(os.path.join(REPO_ROOT, p)):
            git("add", "-A", "--", p)
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(*ident, "commit", "-q", "-m", message)
    return True


def sync(message, paths, retries=5):
    if not is_git_repo():
        raise InboxError("not a git repository; run `git init` first (see SETUP.md)")
    config = load_config()
    ident = _identity_args(config)
    committed = commit_local(message, paths, ident)
    print(("committed: " if committed else "nothing new to commit: ") + message)

    remote = _remote()
    if not remote or not _has_commits():
        print("no remote configured; local commit only")
        return
    branch = _branch()

    for attempt in range(1, retries + 1):
        pull = git("pull", "--rebase", "--no-autostash", remote, branch, check=False)
        if pull.returncode != 0:
            if _conflicted() or os.path.isdir(os.path.join(REPO_ROOT, ".git", "rebase-merge")):
                _resolve_rebase_conflicts(ident)
            elif "couldn't find remote ref" in pull.stderr:
                pass  # remote branch does not exist yet; first push creates it
            else:
                raise InboxError(f"git pull --rebase failed:\n{pull.stderr.strip()}")
        # digest reflects the merged tree, always
        regenerate_digest()
        if commit_local("inbox: regenerate digest", [rel(DIGEST_PATH)], ident):
            print("committed: inbox: regenerate digest")
        push = git("push", remote, f"HEAD:{branch}", check=False)
        if push.returncode == 0:
            print(f"pushed to {remote}/{branch} (attempt {attempt})")
            return
        if "rejected" in push.stderr or "fetch first" in push.stderr or "non-fast-forward" in push.stderr:
            time.sleep(random.uniform(0.5, 2.0) * attempt)
            continue
        raise InboxError(f"git push failed:\n{push.stderr.strip()}")
    raise InboxError(f"push still rejected after {retries} attempts; local commits are intact, try again later")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-m", "--message", required=True)
    ap.add_argument("paths", nargs="*", help="paths to stage (default: items archive INBOX.md)")
    ap.add_argument("--retries", type=int, default=5)
    args = ap.parse_args()
    try:
        sync(args.message, args.paths or list(DEFAULT_PATHS), args.retries)
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
