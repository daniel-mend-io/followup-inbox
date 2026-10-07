#!/usr/bin/env python3
"""List items. The query the DB indexes used to serve: what is open, newest first.

Usage:
  ./list.py                          # open items (new, waiting, ready), newest first
  ./list.py --status ready,waiting   # by status (comma-separated or repeated)
  ./list.py --source slack --kind commitment
  ./list.py --max-age 7              # created in the last 7 days
  ./list.py --min-age 7              # created more than 7 days ago (stale)
  ./list.py --snoozed                # only snoozed items, with when they come back
  ./list.py --all                    # include archive/ (closed) and snoozed items
  ./list.py --all --ids              # every id ever recorded — the dedupe set for a scan
  ./list.py --json                   # full records, for programs
"""

import argparse
import json

from _inbox_common import (KINDS, OPEN_STATUSES, SOURCES, STATUSES, InboxError, die, is_snoozed,
                           load_all, load_config, local_zone, now_utc, parse_ts, rel,
                           sort_newest_first)


def _csv(values):
    out = []
    for v in values or []:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def select(items, statuses=None, sources=None, kinds=None, max_age=None, min_age=None, snoozed="hide"):
    """snoozed: "hide" (default), "only", or "show"."""
    now = now_utc()
    for it in items:
        if snoozed != "show" and is_snoozed(it, now) != (snoozed == "only"):
            continue
        if statuses and it.get("status") not in statuses:
            continue
        if sources and it.get("source") not in sources:
            continue
        if kinds and it.get("kind") not in kinds:
            continue
        age = (now - parse_ts(it["created_at"])).total_seconds() / 86400
        if max_age is not None and age > max_age:
            continue
        if min_age is not None and age < min_age:
            continue
        yield it


def fmt_age(created_at):
    secs = (now_utc() - parse_ts(created_at)).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="append", help="comma-separated; default: open statuses")
    ap.add_argument("--source", action="append")
    ap.add_argument("--kind", action="append")
    ap.add_argument("--max-age", type=float, metavar="DAYS")
    ap.add_argument("--min-age", type=float, metavar="DAYS")
    ap.add_argument("--all", action="store_true", help="include archived (closed) and snoozed items")
    ap.add_argument("--snoozed", action="store_true", help="only snoozed items")
    ap.add_argument("--ids", action="store_true", help="print ids only")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    try:
        statuses = _csv(args.status)
        for s in statuses:
            if s not in STATUSES:
                raise InboxError(f"bad status {s!r}; want one of {', '.join(STATUSES)}")
        sources = _csv(args.source)
        for s in sources:
            if s not in SOURCES:
                raise InboxError(f"bad source {s!r}")
        kinds = _csv(args.kind)
        for k in kinds:
            if k not in KINDS:
                raise InboxError(f"bad kind {k!r}")
        if not statuses and not args.all:
            statuses = list(OPEN_STATUSES)

        items = load_all(include_archive=args.all or any(s in ("done", "dismissed") for s in statuses))
        snoozed = "only" if args.snoozed else ("show" if args.all else "hide")
        rows = sort_newest_first(select(items, statuses, sources, kinds, args.max_age, args.min_age, snoozed))
        if args.limit:
            rows = rows[: args.limit]
    except InboxError as e:
        die(str(e))

    if args.ids:
        for it in rows:
            print(it["id"])
        return
    if args.json:
        out = []
        for it in rows:
            rec = {k: v for k, v in it.items() if not k.startswith("_")}
            rec["path"] = rel(it["_path"])
            out.append(rec)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    tz = local_zone(load_config())
    if not rows:
        print("(no items)")
        return
    for it in rows:
        created = parse_ts(it["created_at"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
        draft = "*" if it.get("proposed_reply") else " "
        if is_snoozed(it):
            draft += f" (snoozed until {parse_ts(it['snoozed_until']).astimezone(tz):%a %d %b})"
        print(f"{it['status']:<9} {it['source']:<8} {it['kind']:<12} {created}  {fmt_age(it['created_at']):>4} {draft} "
              f"{it['title']}  [{it['id']}]")


if __name__ == "__main__":
    main()
