#!/usr/bin/env python3
"""Set or replace an item's proposed reply (the `## Proposed reply` section).

Usage:
  ./draft.py <id> --text "short reply in the user's voice"
  ./draft.py <id> --file reply.md
  ./draft.py <id> --file -            # from stdin
  ./draft.py <id> --clear

Routines own the draft and may replace it as the situation changes; nothing
else in the file is touched. `add` only fills a draft when none exists, so use
this when a trigger has since been met and the old draft is stale.
Refuses to draft on a closed (archived) item.
"""

import argparse
import sys

from _inbox_common import CLOSED_STATUSES, InboxError, die, find_path, read_item, rel, write_item


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file", help="path, or - for stdin")
    g.add_argument("--clear", action="store_true")
    args = ap.parse_args()
    try:
        path = find_path(args.id)
        if path is None:
            raise InboxError(f"no item with id {args.id!r}")
        item = read_item(path)
        if item.get("status") in CLOSED_STATUSES:
            raise InboxError(f"{args.id} is {item['status']}; closed items are never touched")
        if args.clear:
            text = ""
        elif args.text is not None:
            text = args.text
        else:
            text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        item["proposed_reply"] = text.strip("\n")
        write_item(path, item)
        print(f"{'cleared' if not text.strip() else 'drafted'}\t{rel(path)}")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
