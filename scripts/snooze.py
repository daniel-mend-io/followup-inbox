#!/usr/bin/env python3
"""Snooze an item: hide it until a day you choose, then it comes back by itself.

Usage:
  ./snooze.py <id> 3              # 3 days        (also 3d, 2w, 4h)
  ./snooze.py <id> tomorrow       # also: mon … sun (the next one), next-week (Monday)
  ./snooze.py <id> 2026-10-14     # a date        (also 14.10 or 14.10.2026)
  ./snooze.py <id> --wake         # bring it back now

A day or date wakes the item at the start of working hours (config
`working_hours`, else 09:00) in config `timezone`; hours count from now. The
item keeps its status. While snoozed it is left out of list.py, the digest and
the TUI's open list, so routines do not draft on it or nag about it; it stays
in `list.py --all --ids`, so it is never raised again as new. Snoozing is the
user's: routines never snooze or wake an item. Closing it ends the snooze.
"""

import argparse
import datetime as dt
import os
import re

from _inbox_common import (CLOSED_STATUSES, InboxError, cfg_get, die, find_path, iso_utc, load_config,
                           local_zone, now_utc, read_item, rel, write_item)

WEEKDAYS = {name: n for n, names in enumerate((
    ("mon", "monday"), ("tue", "tues", "tuesday"), ("wed", "wednesday"), ("thu", "thur", "thurs", "thursday"),
    ("fri", "friday"), ("sat", "saturday"), ("sun", "sunday"))) for name in names}


def _start_of_day(day, config):
    hours = str(cfg_get(config, "working_hours", "09:00-18:00") or "09:00-18:00")
    m = re.match(r"\s*(\d{1,2}):(\d{2})", hours)
    h, mi = (int(m.group(1)), int(m.group(2))) if m else (9, 0)
    return dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=local_zone(config))


def parse_when(text, config=None, now=None):
    """'3', '3d', '2w', '4h', 'tomorrow', 'fri', 'next-week', '2026-10-14', '14.10', '14.10.2026' -> aware datetime."""
    config = config if config is not None else load_config()
    now = (now or now_utc()).astimezone(local_zone(config))
    s = str(text).strip().lower().replace(" ", "-")
    today = now.date()
    m = re.fullmatch(r"\+?(\d{1,4})-?([hdw]?)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2) or "d"
        if n <= 0:
            raise InboxError("snooze for at least 1")
        if n * {"h": 1, "d": 24, "w": 168}[unit] > 24 * 366:
            raise InboxError("snooze for at most a year")
        if unit == "h":
            return now + dt.timedelta(hours=n)
        day = today + dt.timedelta(days=n * (7 if unit == "w" else 1))
    elif s in ("tomorrow", "tmr", "jutro"):
        day = today + dt.timedelta(days=1)
    elif s in ("next-week", "nextweek", "nw"):
        day = today + dt.timedelta(days=7 - today.weekday())
    elif s in WEEKDAYS:
        ahead = (WEEKDAYS[s] - today.weekday()) % 7 or 7          # the next one, never today
        day = today + dt.timedelta(days=ahead)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        day = dt.date.fromisoformat(s)
    elif re.fullmatch(r"\d{1,2}[./]\d{1,2}([./]\d{4})?", s):
        parts = [int(p) for p in re.split(r"[./]", s)]
        year = parts[2] if len(parts) == 3 else today.year
        try:
            day = dt.date(year, parts[1], parts[0])
        except ValueError:
            raise InboxError(f"no such date: {text!r}")
        if len(parts) == 2 and day <= today:
            day = day.replace(year=year + 1)  # 14.10 in November means next year
    else:
        raise InboxError(f"cannot read {text!r}; try 3, 2w, 4h, tomorrow, fri, next-week, 2026-10-14 or 14.10")
    when = _start_of_day(day, config)
    if when <= now:
        raise InboxError(f"{when:%Y-%m-%d %H:%M} is not in the future")
    return when


def snooze(item_id, when=None, wake=False):
    path = find_path(item_id)
    if not path:
        raise InboxError(f"no item with id {item_id!r}")
    item = read_item(path)
    if item.get("status") in CLOSED_STATUSES:
        raise InboxError(f"{item_id} is {item['status']}; only open items can be snoozed")
    stamp = iso_utc()
    if wake:
        if not item.get("snoozed_until"):
            raise InboxError(f"{item_id} is not snoozed")
        item["snoozed_until"] = None
        line = f"> {stamp} woken by hand"
    else:
        item["snoozed_until"] = iso_utc(when)
        line = f"> {stamp} snoozed until {item['snoozed_until']}"
    body = (item.get("body") or "").rstrip("\n")
    item["body"] = (body + "\n\n" if body else "") + line
    write_item(path, item)
    return path, item


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id")
    ap.add_argument("when", nargs="?", help="3, 3d, 2w, 4h, tomorrow, fri, next-week, 2026-10-14, 14.10")
    ap.add_argument("--wake", action="store_true", help="bring it back now")
    args = ap.parse_args()
    try:
        if args.wake == bool(args.when):
            raise InboxError("give either a time to snooze until, or --wake")
        config = load_config()
        when = None if args.wake else parse_when(args.when, config)
        path, item = snooze(args.id, when, args.wake)
        if args.wake:
            print(f"woken\t{rel(path)}")
        else:
            local = when.astimezone(local_zone(config))
            print(f"snoozed until {local:%a %d %b %H:%M}\t{rel(path)}")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
