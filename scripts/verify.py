#!/usr/bin/env python3
"""Prove the write path end to end. Writes an item, reads it back, changes its
status (which archives it), regenerates the digest, deletes it, reports ok.

Usage:
  ./verify.py            # local store only; touches no git history
  ./verify.py --commit   # also round-trips through sync.py (commit, rebase, push, then
                         # a second commit removing the item). Run this once from the
                         # environment the routines will use: it proves push access.
"""

import argparse
import os
import subprocess
import sys
import time

from _inbox_common import (DIGEST_PATH, ITEMS_DIR, REPO_ROOT, InboxError, die, find_path,
                           id_to_filename, is_git_repo, iso_utc, read_item, rel)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))


def run(script, *args, stdin=None):
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                          input=stdin, text=True, capture_output=True, cwd=REPO_ROOT,
                          env=dict(os.environ, INBOX_ROOT=REPO_ROOT))
    if proc.returncode != 0:
        raise InboxError(f"{script} {' '.join(args)} failed:\n{proc.stdout}{proc.stderr}")
    return proc.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="also commit+push each step through sync.py")
    args = ap.parse_args()

    item_id = f"slack:verify/{int(time.time())}.{os.getpid()}"
    fname = id_to_filename(item_id)
    try:
        print(f"--- store: {REPO_ROOT}")
        if is_git_repo():
            print("git: ok")
        else:
            print("git: NOT a repository (fine for a local check; sync.py will refuse)")
            if args.commit:
                raise InboxError("--commit needs a git repository")

        print(f"--- add {item_id}")
        out = run("add.py", "--id", item_id, "--source", "slack", "--kind", "fyi",
                  "--created-at", iso_utc(), "--title", "verify write path",
                  "--body", "Written by verify.py. Safe to delete.")
        print(out.strip())
        assert out.startswith("created"), out

        print("--- add again (idempotent)")
        out = run("add.py", "--id", item_id, "--source", "slack", "--kind", "fyi",
                  "--created-at", iso_utc(), "--title", "DIFFERENT TITLE MUST NOT WIN")
        print(out.strip())
        assert out.startswith("exists"), out
        assert read_item(find_path(item_id))["title"] == "verify write path"

        print("--- read back")
        out = run("list.py", "--ids")
        assert item_id in out.split(), "item missing from list"
        print(f"listed: {item_id}")

        if args.commit:
            print("--- sync (commit + rebase + push)")
            print(run("sync.py", "-m", "verify: write", rel(os.path.join(ITEMS_DIR, fname)), "INBOX.md").strip())

        print("--- status done (archives)")
        print(run("status.py", item_id, "done").strip())
        path = find_path(item_id)
        assert path and "/archive/" in path.replace(os.sep, "/"), f"not archived: {path}"

        print("--- add after close (must not re-raise)")
        out = run("add.py", "--id", item_id, "--source", "slack", "--kind", "fyi",
                  "--created-at", iso_utc(), "--title", "verify write path")
        print(out.strip())
        assert out.startswith("exists") and "archive/" in out, out
        assert item_id not in run("list.py", "--ids").split(), "closed item still open"

        print("--- digest")
        print(run("digest.py").strip())
        assert os.path.exists(DIGEST_PATH)

        print("--- cleanup")
        os.remove(path)
        run("digest.py")
        if args.commit:
            print(run("sync.py", "-m", "verify: cleanup", "items", "archive", "INBOX.md").strip())
        print("ok")
    except (InboxError, AssertionError) as e:
        p = find_path(item_id)
        if p:
            os.remove(p)
        die(f"verify failed: {e}")


if __name__ == "__main__":
    main()
