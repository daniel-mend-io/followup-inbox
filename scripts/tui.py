#!/usr/bin/env python3
"""Terminal UI over the store. Stdlib only (curses).

Usage:
  ./tui.py                # opens on the Inbox screen
  ./tui.py --check        # no curses: print what each screen would show, then exit

Screens (switch with the number keys, Tab / Shift-Tab):
  0  Routines   what is scheduled, when it fires locally today, when it last wrote
  1  Inbox      every open item, sortable; expand, triage, copy the draft
  2  Efforts    everything you have to keep in your head; accept suggestions, set next actions

Keys (all screens): j/k or arrows move, Enter expands, s sort, S reverse, f filter,
R reload, g git sync, o open link, e edit file in $EDITOR, ? help, q quit.
Inbox:   d done   x dismiss   r ready   w waiting   n reopen   y copy proposed reply
Efforts: a add    A accept    n next action   N note   p pause/resume   d done   x drop
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap

from _inbox_common import (EFFORT_STATUSES, OPEN_STATUSES, REPO_ROOT, STATUSES, InboxError, git,
                           load_all, load_config, load_efforts, local_zone, now_utc, parse_ts, rel)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
STATE_IDS = os.path.join(REPO_ROOT, "state", "routine-ids.md")

INBOX_SORTS = ("newest", "oldest", "status", "source", "kind", "actor", "age")
INBOX_FILTERS = ("open", "ready", "waiting", "new", "all")
EFFORT_SORTS = ("status", "due", "touched", "kind", "title")
EFFORT_FILTERS = ("open", "suggested", "active", "paused", "all")


# ----------------------------------------------------------------------------
# Pure data shaping (tested without curses)
# ----------------------------------------------------------------------------

def fmt_age(created_at):
    secs = max(0, (now_utc() - parse_ts(created_at)).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


def inbox_rows(items, sort="newest", reverse=False, filt="open"):
    if filt == "open":
        items = [i for i in items if i.get("status") in OPEN_STATUSES]
    elif filt != "all":
        items = [i for i in items if i.get("status") == filt]
    status_rank = {s: n for n, s in enumerate(("ready", "new", "waiting", "done", "dismissed"))}
    keyfn = {
        "newest": lambda i: -parse_ts(i["created_at"]).timestamp(),
        "oldest": lambda i: parse_ts(i["created_at"]).timestamp(),
        "age": lambda i: parse_ts(i["created_at"]).timestamp(),
        "status": lambda i: (status_rank.get(i.get("status"), 9), -parse_ts(i["created_at"]).timestamp()),
        "source": lambda i: (i.get("source") or "", -parse_ts(i["created_at"]).timestamp()),
        "kind": lambda i: (i.get("kind") or "", -parse_ts(i["created_at"]).timestamp()),
        "actor": lambda i: ((i.get("actor") or "~").lower(), -parse_ts(i["created_at"]).timestamp()),
    }[sort]
    return sorted(items, key=keyfn, reverse=reverse)


def inbox_line(it, width):
    draft = "*" if it.get("proposed_reply") else " "
    left = f"{it.get('status', ''):<8} {it.get('source', ''):<8} {it.get('kind', ''):<12} {fmt_age(it['created_at']):>4} {draft} "
    who = (it.get("actor") or "")[:16]
    rest = max(10, width - len(left) - 18)
    return (left + f"{who:<16}  " + (it.get("title") or "")[:rest])[:width]


def effort_rows(efforts, sort="status", reverse=False, filt="open"):
    if filt == "open":
        efforts = [e for e in efforts if e.get("status") in ("suggested", "active", "paused")]
    elif filt != "all":
        efforts = [e for e in efforts if e.get("status") == filt]
    rank = {s: n for n, s in enumerate(EFFORT_STATUSES)}
    keyfn = {
        "status": lambda e: (rank.get(e.get("status"), 9), str(e.get("next_action_due") or "9999"), str(e.get("title")).lower()),
        "due": lambda e: (str(e.get("next_action_due") or "9999"), str(e.get("title")).lower()),
        "touched": lambda e: (str(e.get("last_touched") or ""), str(e.get("title")).lower()),
        "kind": lambda e: (e.get("kind") or "", str(e.get("title")).lower()),
        "title": lambda e: str(e.get("title")).lower(),
    }[sort]
    return sorted(efforts, key=keyfn, reverse=reverse)


def effort_line(e, width):
    due = str(e.get("next_action_due") or "")[:10]
    nxt = e.get("next_action") or ""
    mark = " "
    if not nxt and e.get("suggestion"):
        nxt, mark = e["suggestion"].splitlines()[0], "*"
    left = f"{e.get('status', ''):<9} {e.get('kind', ''):<12} {due:<10} {str(e.get('last_touched') or ''):<10} "
    rest = max(10, width - len(left))
    title = (e.get("title") or "")[: max(8, rest // 2)]
    return (left + f"{title:<{max(8, rest // 2)}} {mark}{nxt}")[:width]


def routine_ids_from_state(path=STATE_IDS):
    ids = {}
    if not os.path.exists(path):
        return ids
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\|\s*([\w-]+)\s*\|\s*`?(trig_[\w]+)`?", line)
            if m:
                ids[m.group(1)] = m.group(2)
    return ids


def last_store_write(prefix):
    """Date of the last commit whose message starts with '<prefix>:' (what routines write)."""
    out = git("log", "-1", "--format=%cs", f"--grep=^{prefix}:", check=False).stdout.strip()
    return out or "-"


def routine_rows(config, ids=None, lastwrite=None):
    from render import cron_local_times
    tz = local_zone(config)
    ids = ids or {}
    rows = []
    for r in config.get("routines") or []:
        name = r.get("name", "?")
        tmpl = r.get("template", "")
        rows.append({
            "name": name,
            "enabled": r.get("enabled", True) is not False,
            "cron": r.get("cron", ""),
            "local": cron_local_times(r.get("cron", ""), tz),
            "connectors": ", ".join(r.get("connectors") or []),
            "id": ids.get(name, "-"),
            "last_write": (lastwrite or {}).get(tmpl) or (lastwrite or {}).get(name, "-"),
        })
    return rows


def routine_line(r, width):
    flag = " " if r["enabled"] else "off"
    return (f"{flag:<3} {r['name']:<28} {r['cron']:<16} today {r['local']:<20} last wrote {r['last_write']:<10} {r['connectors']}")[:width]


def detail_text(rec, section_title, section_key, width):
    lines = []
    for k, v in rec.items():
        if k.startswith("_") or k in ("body", section_key) or v in (None, "", {}):
            continue
        lines.append(f"{k}: {v}")
    lines.append("")
    body = rec.get("body") or ""
    for para in body.split("\n"):
        lines.extend(textwrap.wrap(para, width - 2) or [""])
    if rec.get(section_key):
        lines += ["", section_title, ""]
        for para in rec[section_key].split("\n"):
            lines.extend(textwrap.wrap(para, width - 2) or [""])
    return lines


# ----------------------------------------------------------------------------
# Side effects
# ----------------------------------------------------------------------------

def run_script(name, *args, stdin=None):
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, name), *args], input=stdin, text=True,
                          capture_output=True, cwd=REPO_ROOT, env=dict(os.environ, INBOX_ROOT=REPO_ROOT))
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, (out[-1] if out else "")


def copy_to_clipboard(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text, text=True, check=False)
            return True
    return False


def open_url(url):
    import webbrowser
    return bool(url) and webbrowser.open(url)


# ----------------------------------------------------------------------------
# The curses app
# ----------------------------------------------------------------------------

class App:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.screen = 1
        self.sel = {0: 0, 1: 0, 2: 0}
        self.expanded = False
        self.detail_off = 0
        self.sort = {1: 0, 2: 0}
        self.rev = {1: False, 2: False}
        self.filt = {1: 0, 2: 0}
        self.msg = "?: help"
        self.config = load_config()
        self.reload()

    # data ------------------------------------------------------------------
    def reload(self):
        self.items = load_all(include_archive=True)
        self.efforts = load_efforts(include_closed=True)
        ids = routine_ids_from_state()
        lastwrite = {}
        for r in self.config.get("routines") or []:
            t = r.get("template", "")
            if t and t not in lastwrite:
                lastwrite[t] = last_store_write(t)
        self.routines = routine_rows(self.config, ids, lastwrite)

    def rows(self):
        if self.screen == 1:
            return inbox_rows(self.items, INBOX_SORTS[self.sort[1]], self.rev[1], INBOX_FILTERS[self.filt[1]])
        if self.screen == 2:
            return effort_rows(self.efforts, EFFORT_SORTS[self.sort[2]], self.rev[2], EFFORT_FILTERS[self.filt[2]])
        return self.routines

    def current(self):
        rows = self.rows()
        if not rows:
            return None
        self.sel[self.screen] = max(0, min(self.sel[self.screen], len(rows) - 1))
        return rows[self.sel[self.screen]]

    # drawing ----------------------------------------------------------------
    def draw(self):
        import curses
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        tabs = "  ".join(("[%d %s]" if n == self.screen else " %d %s ") % (n, t)
                         for n, t in enumerate(("Routines", "Inbox", "Efforts")))
        scr.addnstr(0, 0, tabs, w - 1, curses.A_BOLD)
        if self.screen == 1:
            hdr = f"sort:{INBOX_SORTS[self.sort[1]]}{' desc' if self.rev[1] else ''}  filter:{INBOX_FILTERS[self.filt[1]]}"
            cols = f"{'status':<8} {'source':<8} {'kind':<12} {'age':>4} * {'who':<16}  title"
        elif self.screen == 2:
            hdr = f"sort:{EFFORT_SORTS[self.sort[2]]}{' desc' if self.rev[2] else ''}  filter:{EFFORT_FILTERS[self.filt[2]]}"
            cols = f"{'status':<9} {'kind':<12} {'due':<10} {'touched':<10} title / next action (* = suggested)"
        else:
            hdr = "crons are UTC; 'today' is the local firing time; 'last wrote' is the newest store commit from that scanner"
            cols = f"{'':<3} {'routine':<28} {'cron (UTC)':<16} {'today':<26} {'last wrote':<21} connectors"
        scr.addnstr(0, max(0, w - len(hdr) - 1), hdr, w - 1)
        scr.addnstr(1, 0, cols, w - 1, curses.A_UNDERLINE)

        rows = self.rows()
        list_h = (h - 3) // 2 if self.expanded else h - 3
        top = max(0, self.sel[self.screen] - list_h + 1)
        for n, row in enumerate(rows[top: top + list_h]):
            y = 2 + n
            if self.screen == 1:
                line = inbox_line(row, w - 1)
            elif self.screen == 2:
                line = effort_line(row, w - 1)
            else:
                line = routine_line(row, w - 1)
            attr = curses.A_REVERSE if top + n == self.sel[self.screen] else curses.A_NORMAL
            scr.addnstr(y, 0, line.ljust(w - 1), w - 1, attr)
        if not rows:
            scr.addnstr(2, 0, "(nothing here)", w - 1)

        if self.expanded and rows:
            cur = self.current()
            y0 = 2 + list_h
            scr.hline(y0, 0, "-", w - 1)
            if self.screen == 1:
                lines = detail_text(cur, "## Proposed reply", "proposed_reply", w)
            elif self.screen == 2:
                lines = detail_text(cur, "## Suggested next action", "suggestion", w)
            else:
                lines = [f"{k}: {v}" for k, v in cur.items()]
            avail = h - y0 - 2
            self.detail_off = max(0, min(self.detail_off, max(0, len(lines) - avail)))
            for n, line in enumerate(lines[self.detail_off: self.detail_off + avail]):
                scr.addnstr(y0 + 1 + n, 1, line, w - 2)
        scr.addnstr(h - 1, 0, self.msg[: w - 1], w - 1, curses.A_DIM)
        scr.refresh()

    def prompt(self, label, default=""):
        import curses
        h, w = self.scr.getmaxyx()
        self.scr.addnstr(h - 1, 0, (label + " ").ljust(w - 1), w - 1)
        self.scr.refresh()
        curses.echo()
        curses.curs_set(1)
        try:
            raw = self.scr.getstr(h - 1, min(len(label) + 1, w - 2), 500)
        except KeyboardInterrupt:
            raw = b""
        finally:
            curses.noecho()
            curses.curs_set(0)
        text = raw.decode("utf-8", "replace").strip()
        return text or default

    def edit_in_editor(self, path):
        import curses
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        curses.endwin()
        subprocess.call([editor, path])
        self.scr.refresh()
        self.reload()

    def help(self):
        self.msg = ("0/1/2 screens  j/k move  Enter expand  s/S sort  f filter  R reload  g sync  o open  e edit  q quit | "
                    "Inbox: d done x dismiss r ready w waiting n reopen y copy | Efforts: a add A accept n next N note p pause d done x drop")

    # actions -----------------------------------------------------------------
    def act(self, key):
        cur = self.current()
        if self.screen == 1 and cur:
            item_id = cur["id"]
            if key in ("d", "x", "r", "w", "n"):
                new = {"d": "done", "x": "dismissed", "r": "ready", "w": "waiting", "n": "new"}[key]
                extra = ["--reopen"] if cur.get("status") in ("done", "dismissed") else []
                ok, out = run_script("status.py", item_id, new, *extra)
                self.msg = out
                self.reload()
                return
            if key == "y":
                text = cur.get("proposed_reply") or ""
                self.msg = "copied proposed reply" if text and copy_to_clipboard(text) else "no proposed reply on this item / no clipboard tool"
                return
        if self.screen == 2:
            if key == "a":
                title = self.prompt("title:")
                if not title:
                    self.msg = "cancelled"
                    return
                kind = self.prompt("kind (epic/customer/marketing/coordination/research/other):", "other")
                nxt = self.prompt("next action (optional):")
                args = ["add", "--title", title, "--kind", kind] + (["--next", nxt] if nxt else [])
                ok, out = run_script("efforts.py", *args)
                self.msg = out
                self.reload()
                return
            if cur and key in ("A", "p", "d", "x"):
                new = {"A": "active", "p": "active" if cur.get("status") == "paused" else "paused",
                       "d": "done", "x": "dropped"}[key]
                ok, out = run_script("efforts.py", "status", cur["id"], new)
                self.msg = out
                self.reload()
                return
            if cur and key == "n":
                text = self.prompt("next action:", cur.get("next_action") or "")
                if text:
                    due = self.prompt("due (YYYY-MM-DD, optional):", cur.get("next_action_due") or "")
                    ok, out = run_script("efforts.py", "next", cur["id"], text, *(["--due", due] if due else []))
                    self.msg = out
                    self.reload()
                return
            if cur and key == "N":
                text = self.prompt("note:")
                if text:
                    ok, out = run_script("efforts.py", "note", cur["id"], text)
                    self.msg = out
                    self.reload()
                return
        if key == "g":
            self.msg = "syncing…"
            self.draw()
            ok, out = run_script("sync.py", "-m", "triage", "items", "archive", "efforts", "INBOX.md")
            self.msg = out
            self.reload()
            return
        if key == "o":
            url = (cur or {}).get("url") if self.screen != 0 else "https://claude.ai/code/routines"
            self.msg = "opened" if open_url(url) else "no url"
            return
        if key == "e" and cur and self.screen != 0:
            self.edit_in_editor(cur["_path"])
            return
        self.msg = "?: help"

    # main loop --------------------------------------------------------------
    def run(self):
        import curses
        curses.curs_set(0)
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch in (ord("q"), 27):
                return
            if ch in (ord("0"), ord("1"), ord("2")):
                self.screen = ch - ord("0"); self.expanded = False; self.detail_off = 0
            elif ch == 9:
                self.screen = (self.screen + 1) % 3; self.expanded = False
            elif ch == curses.KEY_BTAB:
                self.screen = (self.screen - 1) % 3; self.expanded = False
            elif ch in (ord("j"), curses.KEY_DOWN):
                self.sel[self.screen] += 1; self.detail_off = 0
            elif ch in (ord("k"), curses.KEY_UP):
                self.sel[self.screen] -= 1; self.detail_off = 0
            elif ch == curses.KEY_NPAGE:
                self.detail_off += 10
            elif ch == curses.KEY_PPAGE:
                self.detail_off = max(0, self.detail_off - 10)
            elif ch in (10, 13, curses.KEY_ENTER):
                self.expanded = not self.expanded; self.detail_off = 0
            elif ch == ord("s") and self.screen in self.sort:
                n = len(INBOX_SORTS if self.screen == 1 else EFFORT_SORTS)
                self.sort[self.screen] = (self.sort[self.screen] + 1) % n
            elif ch == ord("S") and self.screen in self.rev:
                self.rev[self.screen] = not self.rev[self.screen]
            elif ch == ord("f") and self.screen in self.filt:
                n = len(INBOX_FILTERS if self.screen == 1 else EFFORT_FILTERS)
                self.filt[self.screen] = (self.filt[self.screen] + 1) % n; self.sel[self.screen] = 0
            elif ch == ord("R"):
                self.config = load_config(); self.reload(); self.msg = "reloaded"
            elif ch == ord("?"):
                self.help()
            elif 0 <= ch < 256:
                self.act(chr(ch))
            self.current()  # clamp selection


def check():
    """Print each screen once, no curses. Proves loading works and shows the shape."""
    config = load_config()
    items = load_all(include_archive=True)
    efforts = load_efforts(include_closed=True)
    print("== Routines ==")
    for r in routine_rows(config, routine_ids_from_state(), {}):
        print(routine_line(r, 140))
    print("== Inbox (open, newest first) ==")
    for it in inbox_rows(items):
        print(inbox_line(it, 140))
    print("== Efforts (open) ==")
    for e in effort_rows(efforts):
        print(effort_line(e, 140))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        check()
        return
    import curses
    try:
        curses.wrapper(lambda scr: App(scr).run())
    except InboxError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
