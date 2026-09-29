#!/usr/bin/env python3
"""Change an item's status. This is the triage action, and status is human-owned.

Usage:
  ./status.py <id> <new-status>          new | waiting | ready | done | dismissed
  ./status.py <id> done --note "sent the reply in thread"

Closing (done/dismissed) moves the file to archive/YYYY-MM/. A closed item is
never re-raised by a scan, because `add` finds it in the archive and leaves it
alone. Reopening a closed item needs --reopen and moves it back to items/;
that flag is for humans, a routine never passes it.

Who may call this: humans, for any transition. Routines, only `waiting -> ready`
(with --note saying which trigger was met) — never to done/dismissed, never on
an archived item.
"""

import argparse
import os

from _inbox_common import (CLOSED_STATUSES, ITEMS_DIR, STATUSES, InboxError, archive_dir_for,
                           die, find_path, iso_utc, read_item, rel, write_item)


def set_status(item_id, new_status, note=None, reopen=False):
    if new_status not in STATUSES:
        raise InboxError(f"bad status {new_status!r}; want one of {', '.join(STATUSES)}")
    path = find_path(item_id)
    if path is None:
        raise InboxError(f"no item with id {item_id!r}")
    item = read_item(path)
    old = item.get("status")
    if old in CLOSED_STATUSES and new_status not in CLOSED_STATUSES and not reopen:
        raise InboxError(f"{item_id} is {old}; pass --reopen to bring it back (humans only)")
    item["status"] = new_status
    if note:
        stamp = iso_utc()
        item["body"] = (item.get("body") or "").rstrip("\n")
        item["body"] = (item["body"] + "\n\n" if item["body"] else "") + f"> {stamp} {old} → {new_status}: {note}"

    if new_status in CLOSED_STATUSES:
        target_dir = archive_dir_for()
    else:
        target_dir = ITEMS_DIR
    target = os.path.join(target_dir, os.path.basename(path))
    write_item(target, item)
    if os.path.abspath(target) != os.path.abspath(path):
        os.remove(path)
    return old, target


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id")
    ap.add_argument("new_status", choices=STATUSES)
    ap.add_argument("--note", help="appended to the body as an audit line")
    ap.add_argument("--reopen", action="store_true", help="allow moving a closed item back to open")
    args = ap.parse_args()
    try:
        old, target = set_status(args.id, args.new_status, args.note, args.reopen)
        print(f"{old} -> {args.new_status}\t{rel(target)}")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
