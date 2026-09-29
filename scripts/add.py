#!/usr/bin/env python3
"""Create an item. Idempotent on `id`.

Usage:
  ./add.py --id slack:C123/1700000000.000100 --source slack --kind commitment \
           --created-at 2026-09-28T13:51:15Z --title "Reply to X about Y" \
           [--url URL] [--actor NAME] [--trigger TEXT] [--status new] \
           [--body TEXT | --body-file PATH] [--proposed-reply TEXT | --proposed-reply-file PATH] \
           [--meta '{"k":"v"}']

  ./add.py --json < items.json      # one object or an array of objects (agent-friendly)

Behaviour when the file already exists (in items/ OR archive/):
  - never rewrite it; never touch `status`; never overwrite a non-empty body
  - fill frontmatter fields that are currently empty (url, actor, trigger, meta keys)
  - add a body / proposed reply only if the file has none
Prints one line per item:  created | enriched | exists  <path>
Exit code 0 in all three cases; 1 on a validation error.
"""

import argparse
import json
import os
import sys

from _inbox_common import (ITEMS_DIR, InboxError, die, find_path, id_to_filename,
                           iso_utc, read_item, rel, validate_item, write_item)

ENRICHABLE = ("url", "actor", "trigger")


def _read(path_or_none):
    if not path_or_none:
        return None
    if path_or_none == "-":
        return sys.stdin.read()
    with open(path_or_none, encoding="utf-8") as f:
        return f.read()


def add_one(data):
    item = {k: data.get(k) for k in ("id", "source", "kind", "created_at", "seen_at", "title",
                                    "url", "actor", "trigger", "status", "meta", "body",
                                    "proposed_reply")}
    item["status"] = item.get("status") or "new"
    item["seen_at"] = item.get("seen_at") or iso_utc()
    if isinstance(item.get("meta"), str):
        try:
            item["meta"] = json.loads(item["meta"])
        except json.JSONDecodeError as e:
            raise InboxError(f"meta is not JSON: {e}")
    validate_item(item)

    existing_path = find_path(item["id"])
    if existing_path is None:
        path = os.path.join(ITEMS_DIR, id_to_filename(item["id"]))
        write_item(path, item)
        return "created", path

    existing = read_item(existing_path)
    changed = False
    for key in ENRICHABLE:
        if not existing.get(key) and item.get(key):
            existing[key] = item[key]
            changed = True
    if item.get("meta"):
        merged = dict(existing.get("meta") or {})
        for k, v in item["meta"].items():
            if k not in merged:
                merged[k] = v
                changed = True
        existing["meta"] = merged
    if not existing.get("body") and item.get("body"):
        existing["body"] = item["body"]
        changed = True
    if not existing.get("proposed_reply") and item.get("proposed_reply"):
        existing["proposed_reply"] = item["proposed_reply"]
        changed = True
    if changed:
        write_item(existing_path, existing)
        return "enriched", existing_path
    return "exists", existing_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="read one object or an array of objects from stdin")
    ap.add_argument("--id")
    ap.add_argument("--source")
    ap.add_argument("--kind")
    ap.add_argument("--created-at", dest="created_at", help="ISO-8601, when it happened at the source")
    ap.add_argument("--seen-at", dest="seen_at", help="ISO-8601, defaults to now")
    ap.add_argument("--title")
    ap.add_argument("--url")
    ap.add_argument("--actor", help="who is waiting on you")
    ap.add_argument("--trigger", help="what makes it actionable; omit if none stated")
    ap.add_argument("--status", default=None)
    ap.add_argument("--body")
    ap.add_argument("--body-file", dest="body_file", help="path, or - for stdin")
    ap.add_argument("--proposed-reply", dest="proposed_reply")
    ap.add_argument("--proposed-reply-file", dest="proposed_reply_file")
    ap.add_argument("--meta", help="JSON object of extra fields")
    args = ap.parse_args()

    try:
        if args.json:
            payload = json.load(sys.stdin)
            records = payload if isinstance(payload, list) else [payload]
        else:
            rec = {k: getattr(args, k) for k in ("id", "source", "kind", "created_at", "seen_at", "title",
                                                 "url", "actor", "trigger", "status", "meta",
                                                 "proposed_reply")}
            rec["body"] = args.body if args.body is not None else _read(args.body_file)
            if args.proposed_reply_file:
                rec["proposed_reply"] = _read(args.proposed_reply_file)
            records = [rec]
        rc = 0
        for rec in records:
            try:
                outcome, path = add_one(rec)
                print(f"{outcome}\t{rel(path)}")
            except InboxError as e:
                rc = 1
                print(f"error\t{rec.get('id', '?')}\t{e}", file=sys.stderr)
        sys.exit(rc)
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
