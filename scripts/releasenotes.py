#!/usr/bin/env python3
"""Release notes: a routine drafts them, the user approves, the routine writes them to the tracker.

Usage (the user):
  ./releasenotes.py list                               every release with notes, and its state
  ./releasenotes.py show [<release>]                   one release, ticket by ticket (default: the open one due first)
  ./releasenotes.py request [<release>]                ask the routine for a draft now (default: the next release)
  ./releasenotes.py set <release> <KEY> [--include yes|no] [--note TEXT | --note-file F|-]
  ./releasenotes.py approve <release>                  hand it to the routine to apply; closes the inbox item
  ./releasenotes.py reopen <release>                   back to draft (tickets already applied stay applied)

Usage (the release-notes routine):
  ./releasenotes.py pending --json                     what to draft and what to apply
  ./releasenotes.py draft --json < draft.json          write or refresh a draft (keeps the user's edits)
  ./releasenotes.py applied <release> <KEY> [--error MESSAGE]

One file per release, releasenotes/<release>.json. A release is the calendar
version (26.9.3); it maps to one tracker version, rarely two
("26.9.3 (19-Oct-26)"). Notes are due on the Friday before the deployment
(state/releases.json, the same Friday as snooze `next-release`); the routine
drafts `release_notes.draft_days_before_due` days before that, or at once when
the user requests it.

States: requested -> draft -> approved -> applied. The routine writes
`requested`/`draft` files and marks tickets applied; only the user edits,
approves or reopens. A ticket the user edited keeps their include and note
when the routine refreshes the draft.
"""

import argparse
import datetime as dt
import json
import os
import sys

from _inbox_common import (REPO_ROOT, InboxError, cfg_get, die, find_path, iso_utc, load_config, local_zone,
                           now_utc, read_item, rel)

NOTES_DIR = os.path.join(REPO_ROOT, "releasenotes")
STATES = ("requested", "draft", "approved", "applied")
TICKET_FIELDS = ("key", "summary", "type", "status", "url", "include", "reason", "note")
DEFAULTS = {"draft_days_before_due": 1, "project": "WSAP"}


def _cfg(name):
    return cfg_get(load_config(), f"release_notes.{name}", DEFAULTS[name])


def item_id(release):
    return f"jira:release-notes/{release}"


def path_for(release):
    if not release or "/" in release or release.startswith("."):
        raise InboxError(f"bad release {release!r}")
    return os.path.join(NOTES_DIR, f"{release}.json")


def load(release):
    p = path_for(release)
    if not os.path.exists(p):
        raise InboxError(f"no release notes for {release} (request a draft with: releasenotes.py request {release})")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(doc):
    p = path_for(doc["release"])
    os.makedirs(NOTES_DIR, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)
    return p


def all_docs():
    if not os.path.isdir(NOTES_DIR):
        return []
    out = []
    for name in sorted(os.listdir(NOTES_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(NOTES_DIR, name), encoding="utf-8") as f:
                out.append(json.load(f))
    # open ones first, soonest due first; then applied ones, newest first
    still_open = sorted((d for d in out if d.get("status") != "applied"), key=lambda d: d.get("due") or "9999")
    applied = sorted((d for d in out if d.get("status") == "applied"), key=lambda d: d.get("due") or "", reverse=True)
    return still_open + applied


def release_dates(release):
    """(deploy date, due Friday) for a release from state/releases.json, or (None, None)."""
    from releases import friday_before, load as load_releases
    for r in load_releases().get("releases", []):
        if r["version"] == release:
            day = dt.date.fromisoformat(r["date"])
            return day.isoformat(), friday_before(day).isoformat()
    return None, None


def next_release(today=None):
    """The first release whose deployment has not happened yet."""
    from releases import load as load_releases
    today = today or now_utc().astimezone(local_zone(load_config())).date()
    for r in load_releases().get("releases", []):
        if dt.date.fromisoformat(r["date"]) >= today:
            return r["version"]
    return None


def counts(doc):
    t = doc.get("tickets", [])
    inc = [x for x in t if x.get("include")]
    return {"tickets": len(t), "included": len(inc), "applied": sum(1 for x in inc if x.get("applied")),
            "errors": sum(1 for x in inc if x.get("apply_error"))}


# ----------------------------------------------------------------------------
# The user's verbs
# ----------------------------------------------------------------------------

def request(release=None):
    release = release or next_release()
    if not release:
        raise InboxError("no upcoming release known; give one: releasenotes.py request 26.9.3")
    p = path_for(release)
    if os.path.exists(p):
        doc = load(release)
        return doc, f"already {doc['status']}"
    deploy, due = release_dates(release)
    doc = {"release": release, "status": "requested", "deploy_date": deploy, "due": due,
           "jira_versions": [], "requested_at": iso_utc(), "tickets": []}
    save(doc)
    return doc, "requested"


def set_ticket(release, key, include=None, note=None):
    doc = load(release)
    if doc["status"] not in ("draft",):
        raise InboxError(f"{release} is {doc['status']}; " + ("reopen it first" if doc["status"] in ("approved", "applied")
                                                              else "wait for the draft"))
    for t in doc["tickets"]:
        if t["key"] == key:
            if include is not None:
                t["include"] = include
            if note is not None:
                t["note"] = note.strip()
            t["edited"] = True
            save(doc)
            return t
    raise InboxError(f"{key} is not in the {release} draft")


def approve(release):
    doc = load(release)
    if doc["status"] != "draft":
        raise InboxError(f"{release} is {doc['status']}; only a draft can be approved")
    missing = [t["key"] for t in doc["tickets"] if t.get("include") and not (t.get("note") or "").strip()]
    if missing:
        raise InboxError(f"included without a note: {', '.join(missing)}; write one or exclude them")
    doc["status"], doc["approved_at"] = "approved", iso_utc()
    save(doc)
    if find_path(item_id(release)):
        from status import set_status
        it = read_item(find_path(item_id(release)))
        if it.get("status") not in ("done", "dismissed"):
            c = counts(doc)
            set_status(item_id(release), "done", f"approved {c['included']} release notes; the routine applies them")
    return doc


def reopen(release):
    doc = load(release)
    if doc["status"] not in ("approved", "applied"):
        raise InboxError(f"{release} is {doc['status']}; nothing to reopen")
    doc["status"] = "draft"
    doc.pop("approved_at", None)
    save(doc)
    return doc


# ----------------------------------------------------------------------------
# The routine's verbs
# ----------------------------------------------------------------------------

def pending(today=None):
    config = load_config()
    today = today or now_utc().astimezone(local_zone(config)).date()
    ahead = int(_cfg("draft_days_before_due"))
    have = {d["release"]: d for d in all_docs()}
    to_draft = []
    for d in have.values():
        if d["status"] == "requested":
            to_draft.append({"release": d["release"], "deploy_date": d.get("deploy_date"), "due": d.get("due"),
                             "why": "requested by the user"})
    from releases import friday_before, load as load_releases
    for r in load_releases().get("releases", []):
        day = dt.date.fromisoformat(r["date"])
        due = friday_before(day)
        if r["version"] not in have and due - dt.timedelta(days=ahead) <= today <= day:
            to_draft.append({"release": r["version"], "deploy_date": day.isoformat(), "due": due.isoformat(),
                             "why": f"due {due:%a %d %b}"})
    to_apply = [d for d in have.values() if d["status"] == "approved"
                and any(t.get("include") and not t.get("applied") for t in d["tickets"])]
    return {"project": _cfg("project"), "to_draft": to_draft, "to_apply": to_apply}


def draft(payload):
    release = payload.get("release")
    p = path_for(release)
    old = load(release) if os.path.exists(p) else None
    if old and old["status"] in ("approved", "applied"):
        raise InboxError(f"{release} is {old['status']}; the user reopens it before it can be redrafted")
    kept = {t["key"]: t for t in (old or {}).get("tickets", []) if t.get("edited")}
    tickets = []
    for raw in payload.get("tickets") or []:
        if not raw.get("key"):
            raise InboxError("every ticket needs a key")
        if raw["key"] in kept:
            t = kept.pop(raw["key"])
            t.update({k: raw[k] for k in ("summary", "type", "status", "url") if raw.get(k)})
        else:
            t = {k: raw.get(k) for k in TICKET_FIELDS}
            t["include"] = bool(raw.get("include"))
            t["note"] = (raw.get("note") or "").strip()
            t["edited"] = False
        t.setdefault("applied", False)
        t.setdefault("apply_error", None)
        tickets.append(t)
    tickets.extend(kept.values())  # the user's edits survive even if the ticket left the version
    deploy, due = release_dates(release)
    doc = {"release": release, "status": "draft",
           "deploy_date": payload.get("deploy_date") or deploy or (old or {}).get("deploy_date"),
           "due": payload.get("due") or due or (old or {}).get("due"),
           "jira_versions": payload.get("jira_versions") or [], "drafted_at": iso_utc(),
           "requested_at": (old or {}).get("requested_at"), "tickets": tickets}
    path = save(doc)
    c = counts(doc)
    from add import add_one
    due_txt = dt.date.fromisoformat(doc["due"]).strftime("%a %d %b") if doc.get("due") else "before the deployment"
    outcome, item_path = add_one({
        "id": item_id(release), "source": "jira", "kind": "ask", "created_at": iso_utc(), "status": "ready",
        "title": f"Release notes for {release}: review and approve, due {due_txt}",
        "actor": "Release notes", "trigger": f"due {doc.get('due') or 'before deployment'}",
        "url": "https://mend-io.atlassian.net/projects/" + _cfg("project") +
               "?selectedItem=com.atlassian.jira.jira-projects-plugin%3Arelease-page",
        "body": (f"{c['tickets']} tickets in {', '.join(doc['jira_versions']) or release}; "
                 f"{c['included']} proposed as customer facing.\n\n"
                 f"Review on screen 3 of the TUI, or `releasenotes.py show {release}`. Approving there closes this item "
                 "and the release-notes routine writes the label, Release Note and Customer-Facing? to each ticket.")})
    return doc, path, item_path


def mark_applied(release, key, error=None):
    doc = load(release)
    if doc["status"] != "approved":
        raise InboxError(f"{release} is {doc['status']}; only approved notes are applied")
    for t in doc["tickets"]:
        if t["key"] == key:
            if error:
                t["apply_error"] = error
            else:
                t["applied"], t["apply_error"], t["applied_at"] = True, None, iso_utc()
            break
    else:
        raise InboxError(f"{key} is not in {release}")
    if all(t.get("applied") for t in doc["tickets"] if t.get("include")):
        doc["status"], doc["applied_at"] = "applied", iso_utc()
    save(doc)
    return doc


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _describe(doc):
    c = counts(doc)
    due = f"due {doc['due']}" if doc.get("due") else "due ?"
    extra = f", {c['applied']} applied" if doc["status"] in ("approved", "applied") else ""
    errs = f", {c['errors']} failed" if c["errors"] else ""
    return f"{doc['release']:<9} {doc['status']:<9} {due:<14} {c['included']}/{c['tickets']} customer facing{extra}{errs}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("show"); s.add_argument("release", nargs="?")
    r = sub.add_parser("request"); r.add_argument("release", nargs="?")
    st = sub.add_parser("set"); st.add_argument("release"); st.add_argument("key")
    st.add_argument("--include", choices=("yes", "no")); g = st.add_mutually_exclusive_group()
    g.add_argument("--note"); g.add_argument("--note-file")
    a = sub.add_parser("approve"); a.add_argument("release")
    o = sub.add_parser("reopen"); o.add_argument("release")
    p = sub.add_parser("pending"); p.add_argument("--json", action="store_true", required=True)
    d = sub.add_parser("draft"); d.add_argument("--json", action="store_true", required=True)
    ad = sub.add_parser("applied"); ad.add_argument("release"); ad.add_argument("key"); ad.add_argument("--error")
    args = ap.parse_args()
    try:
        if args.cmd == "list":
            docs = all_docs()
            print("\n".join(_describe(x) for x in docs) if docs else "(no release notes yet)")
        elif args.cmd == "show":
            docs = all_docs()
            doc = load(args.release) if args.release else (docs[0] if docs else None)
            if not doc:
                print("(no release notes yet)")
                return
            print(_describe(doc), "  versions:", ", ".join(doc.get("jira_versions") or ["?"]))
            for t in doc["tickets"]:
                mark = "x" if t.get("include") else " "
                state = " applied" if t.get("applied") else (f" FAILED: {t['apply_error']}" if t.get("apply_error") else "")
                print(f"[{mark}] {t['key']:<11} {t.get('summary') or ''}{state}")
                if t.get("include") and t.get("note"):
                    print(f"      {t['note']}")
                elif t.get("reason"):
                    print(f"      ({t['reason']})")
        elif args.cmd == "request":
            doc, outcome = request(args.release)
            print(f"{outcome}\t{doc['release']}\t{rel(path_for(doc['release']))}")
        elif args.cmd == "set":
            note = args.note if args.note is not None else (
                (sys.stdin.read() if args.note_file == "-" else open(args.note_file, encoding="utf-8").read())
                if args.note_file else None)
            inc = None if args.include is None else args.include == "yes"
            t = set_ticket(args.release, args.key, inc, note)
            print(f"{'included' if t['include'] else 'excluded'}\t{t['key']}")
        elif args.cmd == "approve":
            doc = approve(args.release)
            print(f"approved\t{doc['release']}\t{counts(doc)['included']} notes to apply")
        elif args.cmd == "reopen":
            print(f"reopened\t{reopen(args.release)['release']}")
        elif args.cmd == "pending":
            print(json.dumps(pending(), indent=2, ensure_ascii=False))
        elif args.cmd == "draft":
            doc, path, item_path = draft(json.load(sys.stdin))
            print(f"drafted\t{rel(path)}\t{counts(doc)['included']}/{counts(doc)['tickets']} customer facing\t{rel(item_path)}")
        elif args.cmd == "applied":
            doc = mark_applied(args.release, args.key, args.error)
            print(f"{'failed' if args.error else 'applied'}\t{args.key}\t{doc['release']} is {doc['status']}")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
