#!/usr/bin/env python3
"""Regenerate INBOX.md: open items grouped by status then source, newest first.

Usage:
  ./digest.py            # rewrite INBOX.md
  ./digest.py --stdout   # print instead

Run at the end of every routine run. Never hand-edit INBOX.md; it is derived.
"""

import argparse
import datetime as dt
import os
import sys

from _inbox_common import (ARCHIVE_DIR, DIGEST_PATH, OPEN_STATUSES, SOURCES, is_snoozed, load_all,
                           load_config, load_efforts, woke_recently, local_zone, now_utc, parse_ts, rel, sort_newest_first)

STATUS_ORDER = ("ready", "waiting", "new")   # most actionable first
STATUS_BLURB = {
    "ready": "trigger met, needs you",
    "waiting": "trigger not met yet",
    "new": "not yet triaged",
}


def render(items, config):
    tz = local_zone(config)
    tzname = getattr(tz, "key", "UTC")
    now = now_utc().astimezone(tz)
    all_open = [it for it in items if it.get("status") in OPEN_STATUSES]
    snoozed = [it for it in all_open if is_snoozed(it)]
    open_items = [it for it in all_open if not is_snoozed(it)]
    lines = ["# Inbox", "",
             f"_Generated {now.strftime('%Y-%m-%d %H:%M')} {tzname}. Do not edit; run `scripts/digest.py`._",
             ""]
    counts = {s: sum(1 for it in open_items if it["status"] == s) for s in STATUS_ORDER}
    drafts = sum(1 for it in open_items if it.get("proposed_reply"))
    lines.append(f"**Open: {len(open_items)}** — " +
                 ", ".join(f"{counts[s]} {s}" for s in STATUS_ORDER) +
                 (f"; {drafts} with a proposed reply" if drafts else "") +
                 (f". Snoozed: {len(snoozed)}" if snoozed else ""))
    lines.append("")

    for status in STATUS_ORDER:
        group = [it for it in open_items if it["status"] == status]
        if not group:
            continue
        lines.append(f"## {status} ({len(group)}) — {STATUS_BLURB[status]}")
        lines.append("")
        for source in SOURCES:
            sub = sort_newest_first([it for it in group if it["source"] == source])
            if not sub:
                continue
            lines.append(f"### {source}")
            lines.append("")
            for it in sub:
                created = parse_ts(it["created_at"]).astimezone(tz).strftime("%Y-%m-%d")
                bits = [f"- {created}", f"**{it['kind']}**", f"[{it['title']}]({rel(it['_path'])})"]
                tail = ["**back from snooze**"] if woke_recently(it) else []
                if it.get("actor"):
                    tail.append(f"waiting: {it['actor']}")
                if it.get("trigger"):
                    tail.append(f"trigger: {it['trigger']}")
                if it.get("proposed_reply"):
                    tail.append("draft ready")
                if it.get("url"):
                    tail.append(f"[source]({it['url']})")
                lines.append(" ".join(bits) + (" — " + "; ".join(tail) if tail else ""))
            lines.append("")

    if snoozed:
        lines.append(f"## snoozed ({len(snoozed)}) — hidden until the date shown")
        lines.append("")
        for it in sorted(snoozed, key=lambda i: parse_ts(i["snoozed_until"])):
            back = parse_ts(it["snoozed_until"]).astimezone(tz).strftime("%a %Y-%m-%d")
            lines.append(f"- back {back}: **{it['kind']}** [{it['title']}]({rel(it['_path'])})")
        lines.append("")

    efforts = load_efforts(include_closed=False)
    if efforts:
        lines.append(f"## efforts ({len(efforts)} open)")
        lines.append("")
        for e in sorted(efforts, key=lambda x: (x.get("status") != "suggested", str(x.get("next_action_due") or "9999"))):
            nxt = e.get("next_action") or (("suggested: " + e["suggestion"].splitlines()[0]) if e.get("suggestion") else "no next action")
            due = f" (due {e['next_action_due']})" if e.get("next_action_due") else ""
            lines.append(f"- **{e.get('status')}** {e.get('kind')}: [{e['title']}]({rel(e['_path'])}) → {nxt}{due}")
        lines.append("")

    # closed this month, for a sense of throughput; the archive holds the rest
    month = now_utc().strftime("%Y-%m")
    mdir = os.path.join(ARCHIVE_DIR, month)
    closed = len([n for n in os.listdir(mdir) if n.endswith(".md")]) if os.path.isdir(mdir) else 0
    lines.append(f"_Closed this month: {closed} (see `archive/{month}/`)._")
    lines.append("")
    return "\n".join(lines)


def write_digest():
    text = render(load_all(include_archive=False), load_config())
    tmp = DIGEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, DIGEST_PATH)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()
    if args.stdout:
        sys.stdout.write(render(load_all(include_archive=False), load_config()))
    else:
        write_digest()
        print(f"wrote {rel(DIGEST_PATH)}")


if __name__ == "__main__":
    main()
