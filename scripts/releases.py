#!/usr/bin/env python3
"""Release dates, so an item can be snoozed until just before the next release.

Usage:
  ./releases.py set --json < releases.json   # routine: replace the list read from the release calendar
  ./releases.py list                         # upcoming releases and the Friday each one wakes snoozed items
  ./releases.py next                         # the next one whose Friday is still ahead

The list lives in state/releases.json. A routine with calendar access refreshes
it from the release calendar (config `releases`), because snooze runs where
there is no calendar. Each record is {"version", "date", "title"}: `date` is the
day the deployment starts (YYYY-MM-DD, in the release calendar's own timezone).
A release deploys over a weekend (Sunday, or Sunday into Monday); snoozing to
"next-release" wakes the item on the Friday before it, at the start of working
hours, so there is a working day left to act.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

from _inbox_common import REPO_ROOT, InboxError, die, iso_utc, now_utc, parse_ts, rel

RELEASES_PATH = os.path.join(REPO_ROOT, "state", "releases.json")
STALE_DAYS = 14
REWRITE_DAYS = 7   # unchanged dates are re-stamped at most weekly, so a daily refresh is not a daily commit
_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")


def friday_before(day):
    """The Friday strictly before `day` (a Sunday gives the Friday two days earlier, a Monday three)."""
    return day - dt.timedelta(days=(day.weekday() - 4) % 7 or 7)


def load():
    if not os.path.exists(RELEASES_PATH):
        return {"updated_at": None, "releases": []}
    with open(RELEASES_PATH, encoding="utf-8") as f:
        return json.load(f)


def save(records):
    clean = []
    for r in records:
        version, day = str(r.get("version") or "").strip(), str(r.get("date") or "").strip()
        if not _VERSION.match(version):
            raise InboxError(f"bad version {version!r}")
        dt.date.fromisoformat(day)
        clean.append({"version": version, "date": day, "title": r.get("title") or f"{version} Deployments"})
    clean.sort(key=lambda r: r["date"])
    current = load()
    fresh = parse_ts(current.get("updated_at"))
    if current.get("releases") == clean and fresh and now_utc() - fresh < dt.timedelta(days=REWRITE_DAYS):
        return clean, False  # same dates, recently confirmed: leave the file (and git) alone
    os.makedirs(os.path.dirname(RELEASES_PATH), exist_ok=True)
    tmp = RELEASES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": iso_utc(), "releases": clean}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, RELEASES_PATH)
    return clean, True


def stale_warning(data=None, now=None):
    data = data or load()
    updated = parse_ts(data.get("updated_at"))
    if not updated:
        return "no release dates yet: state/releases.json is filled by the efforts routine"
    if (now or now_utc()) - updated > dt.timedelta(days=STALE_DAYS):
        return f"release dates last refreshed {updated:%Y-%m-%d}; they may be out of date"
    return None


def upcoming(wake_at, now=None, data=None):
    """Releases whose wake time (from `wake_at(friday)`) is still ahead, soonest first."""
    now = now or now_utc()
    out = []
    for r in (data or load()).get("releases", []):
        day = dt.date.fromisoformat(r["date"])
        wake = wake_at(friday_before(day))
        if wake > now:
            out.append({**r, "friday": friday_before(day).isoformat(), "wake": wake})
    return out


def main():
    from snooze import _start_of_day  # wake at the start of working hours, like any other day
    from _inbox_common import load_config, local_zone
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set"); s.add_argument("--json", action="store_true", required=True)
    sub.add_parser("list"); sub.add_parser("next")
    args = ap.parse_args()
    try:
        if args.cmd == "set":
            payload = json.load(sys.stdin)
            saved, written = save(payload if isinstance(payload, list) else [payload])
            print(f"{'saved' if written else 'unchanged'} {len(saved)} releases\t{rel(RELEASES_PATH)}")
            return
        config = load_config()
        tz = local_zone(config)
        rows = upcoming(lambda d: _start_of_day(d, config))
        warn = stale_warning()
        if warn:
            print(f"warning: {warn}", file=sys.stderr)
        if args.cmd == "next":
            rows = rows[:1]
        if not rows:
            print("(no upcoming release known)")
        for r in rows:
            day = dt.date.fromisoformat(r["date"])
            print(f"{r['version']:<9} deploys {day:%a %d %b %Y}   snoozed items wake {r['wake'].astimezone(tz):%a %d %b %H:%M}")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
