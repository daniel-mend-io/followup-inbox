#!/usr/bin/env python3
"""Efforts: everything the user has to keep in their head, one file each under efforts/.

Usage:
  ./efforts.py add --title "Cap One SAST rollout" --kind customer [--id effort:cap-one-rollout]
                   [--url ...] [--next "send Toni the rollout plan"] [--due 2026-10-03] [--status active]
  ./efforts.py add --json < efforts.json          # one object or an array; routines use this
  ./efforts.py list [--all] [--status active,suggested] [--kind epic] [--json] [--ids]
  ./efforts.py status <id> active|paused|done|dropped     # accept a suggestion with `active`
  ./efforts.py next <id> "the next concrete step" [--due YYYY-MM-DD]   # sets next_action, clears suggestion
  ./efforts.py note <id> "what happened"                   # appends a dated line to the body
  ./efforts.py suggest <id> --text "..." | --file -        # routine-owned: proposed next action

Ids: `effort:<slug>` (manual; derived from the title when omitted) or `jira:<KEY>`
(suggested from the tracker, so re-suggesting is idempotent). `add` never rewrites
an existing effort; it reports `exists`. Status is human-owned: a routine may only
create efforts as `suggested` and write the suggestion section.
"""

import argparse
import json
import os
import sys

from _inbox_common import (EFFORT_KINDS, EFFORT_OPEN, EFFORT_STATUSES, EFFORTS_DIR, InboxError,
                           die, effort_path, iso_utc, load_efforts, now_utc, read_effort, rel,
                           slugify, validate_effort, write_effort)


def _today():
    return now_utc().strftime("%Y-%m-%d")


def add_one(data):
    e = {k: data.get(k) for k in ("id", "kind", "title", "source", "url", "status", "next_action",
                                  "next_action_due", "last_touched", "created_at", "meta", "body", "suggestion")}
    if not e.get("id"):
        e["id"] = "effort:" + slugify(e.get("title") or "")
    e["kind"] = e.get("kind") or "other"
    e["source"] = e.get("source") or ("jira" if e["id"].startswith("jira:") else "manual")
    e["status"] = e.get("status") or ("suggested" if e["source"] != "manual" else "active")
    e["created_at"] = e.get("created_at") or iso_utc()
    e["last_touched"] = e.get("last_touched") or _today()
    validate_effort(e)
    path = effort_path(e["id"])
    if os.path.exists(path):
        return "exists", path
    write_effort(path, e)
    return "created", path


def _load(effort_id):
    path = effort_path(effort_id)
    if not os.path.exists(path):
        raise InboxError(f"no effort with id {effort_id!r}")
    return path, read_effort(path)


def cmd_add(args):
    if args.json:
        payload = json.load(sys.stdin)
        records = payload if isinstance(payload, list) else [payload]
    else:
        if not args.title:
            raise InboxError("--title is required (or use --json)")
        records = [{"id": args.id, "kind": args.kind, "title": args.title, "url": args.url,
                    "status": args.status or "active", "next_action": args.next, "next_action_due": args.due,
                    "body": args.body}]
    rc = 0
    for rec in records:
        try:
            outcome, path = add_one(rec)
            print(f"{outcome}\t{rel(path)}")
        except InboxError as e:
            rc = 1
            print(f"error\t{rec.get('id') or rec.get('title') or '?'}\t{e}", file=sys.stderr)
    sys.exit(rc)


def cmd_list(args):
    statuses = [s for v in (args.status or []) for s in v.split(",") if s]
    kinds = [k for v in (args.kind or []) for k in v.split(",") if k]
    rows = load_efforts(include_closed=args.all or any(s not in EFFORT_OPEN for s in statuses))
    if statuses:
        rows = [e for e in rows if e.get("status") in statuses]
    if kinds:
        rows = [e for e in rows if e.get("kind") in kinds]
    order = {s: i for i, s in enumerate(EFFORT_STATUSES)}
    rows.sort(key=lambda e: (order.get(e.get("status"), 9), str(e.get("next_action_due") or "9999"),
                             str(e.get("last_touched") or "")))
    if args.ids:
        for e in rows:
            print(e["id"])
        return
    if args.json:
        out = [{**{k: v for k, v in e.items() if not k.startswith("_")}, "path": rel(e["_path"])} for e in rows]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("(no efforts)")
        return
    for e in rows:
        due = f" due {e['next_action_due']}" if e.get("next_action_due") else ""
        nxt = e.get("next_action") or ("* " + e["suggestion"].splitlines()[0] if e.get("suggestion") else "-")
        print(f"{e['status']:<9} {e['kind']:<12} {e.get('last_touched') or '':<10} {e['title']}  ->  {nxt}{due}  [{e['id']}]")


def cmd_status(args):
    path, e = _load(args.id)
    if args.new_status not in EFFORT_STATUSES:
        raise InboxError(f"bad status {args.new_status!r}")
    old = e.get("status")
    e["status"] = args.new_status
    e["last_touched"] = _today()
    if old == "suggested" and args.new_status == "active" and e.get("suggestion") and not e.get("next_action"):
        e["next_action"] = e["suggestion"].splitlines()[0]
        e["suggestion"] = ""
    write_effort(path, e)
    print(f"{old} -> {args.new_status}\t{rel(path)}")


def cmd_next(args):
    path, e = _load(args.id)
    e["next_action"] = args.text
    e["next_action_due"] = args.due
    e["last_touched"] = _today()
    e["suggestion"] = ""
    write_effort(path, e)
    print(f"next\t{rel(path)}")


def cmd_note(args):
    path, e = _load(args.id)
    body = (e.get("body") or "").rstrip("\n")
    e["body"] = (body + "\n\n" if body else "") + f"- {_today()}: {args.text}"
    e["last_touched"] = _today()
    write_effort(path, e)
    print(f"noted\t{rel(path)}")


def cmd_suggest(args):
    path, e = _load(args.id)
    if e.get("status") in ("done", "dropped"):
        raise InboxError(f"{args.id} is {e['status']}; closed efforts are never touched")
    text = args.text if args.text is not None else (sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read())
    e["suggestion"] = text.strip("\n")
    write_effort(path, e)
    print(f"suggested\t{rel(path)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--json", action="store_true"); a.add_argument("--id")
    a.add_argument("--title"); a.add_argument("--kind", default="other", choices=EFFORT_KINDS); a.add_argument("--url")
    a.add_argument("--status", choices=EFFORT_STATUSES); a.add_argument("--next"); a.add_argument("--due"); a.add_argument("--body")
    a.set_defaults(fn=cmd_add)
    l = sub.add_parser("list"); l.add_argument("--all", action="store_true"); l.add_argument("--status", action="append")
    l.add_argument("--kind", action="append"); l.add_argument("--json", action="store_true"); l.add_argument("--ids", action="store_true")
    l.set_defaults(fn=cmd_list)
    st = sub.add_parser("status"); st.add_argument("id"); st.add_argument("new_status", choices=EFFORT_STATUSES); st.set_defaults(fn=cmd_status)
    n = sub.add_parser("next"); n.add_argument("id"); n.add_argument("text"); n.add_argument("--due"); n.set_defaults(fn=cmd_next)
    no = sub.add_parser("note"); no.add_argument("id"); no.add_argument("text"); no.set_defaults(fn=cmd_note)
    sg = sub.add_parser("suggest"); sg.add_argument("id"); g = sg.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--file"); sg.set_defaults(fn=cmd_suggest)
    args = ap.parse_args()
    try:
        args.fn(args)
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
