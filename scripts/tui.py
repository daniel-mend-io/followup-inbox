#!/usr/bin/env python3
"""Terminal UI over the store. Stdlib only (curses). List on the left, detail on the right.

Usage:
  ./tui.py                # opens on the Inbox screen
  ./tui.py --check        # no curses: print what each screen would show, then exit

Screens (number keys, Tab / Shift-Tab):
  0  Routines   what is scheduled, when it fires locally today, when it last wrote
  1  Inbox      every open item, sortable; detail pane, triage, copy the draft
  2  Efforts    what you carry; accept suggestions, set next actions
  3  Release notes  the draft for the next release: include or drop tickets, edit notes, approve

Keys (all screens): j/k or arrows move, J/K or PgDn/PgUp scroll the detail, s sort,
S reverse, f filter, m status menu, R reload, g git sync, o open link, e edit in $EDITOR,
? help, q quit.
Inbox:   d done   x dismiss   z snooze (hide until a day)   Z wake   r ready   w waiting
         n reopen   y copy proposed reply   (f cycles to the `snoozed` filter to see them)
Efforts: a add    A accept    n next action   N note   p pause/resume   d done   x drop
Notes:   space include/drop   Enter edit note   A approve   U reopen   r request a draft   [ ] other release
"""

import argparse
import datetime as dt
import locale
import os
import re
import shutil
import subprocess
import sys
import textwrap

from _inbox_common import (EFFORT_STATUSES, OPEN_STATUSES, REPO_ROOT, STATUSES, InboxError, git, is_snoozed,
                           woke_recently, load_all, load_config, load_efforts, local_zone, now_utc, parse_ts, rel)

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
STATE_IDS = os.path.join(REPO_ROOT, "state", "routine-ids.md")
RENDERED = os.path.join(REPO_ROOT, "rendered")

INBOX_SORTS = ("newest", "oldest", "status", "source", "kind", "actor", "age")
INBOX_FILTERS = ("open", "ready", "waiting", "new", "snoozed", "all")
EFFORT_SORTS = ("status", "due", "touched", "kind", "title")
EFFORT_FILTERS = ("open", "suggested", "active", "paused", "all")

SOURCE_GLYPH = {"slack": "#", "jira": "◆", "email": "✉", "calendar": "▣"}
SOURCE_LABEL = {"slack": "slack", "jira": "jira", "email": "email", "calendar": "cal"}
STATUS_GLYPH = {"ready": "●", "waiting": "◐", "new": "○", "done": "✓", "dismissed": "×",
                "suggested": "○", "active": "●", "paused": "◐", "dropped": "×"}
KIND_LABEL = {"commitment": "promise", "ask": "ask", "needs_reply": "reply", "loose_thread": "thread", "fyi": "fyi"}


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
    """Snoozed items show only under the `snoozed` and `all` filters."""
    if filt == "snoozed":
        items = [i for i in items if is_snoozed(i)]
    elif filt != "all":
        items = [i for i in items if not is_snoozed(i)]
    if filt == "open":
        items = [i for i in items if i.get("status") in OPEN_STATUSES]
    elif filt not in ("all", "snoozed"):
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


def last_writes(config):
    out = {}
    for r in config.get("routines") or []:
        t = r.get("template", "")
        if t and t not in out:
            out[t] = last_store_write(t)
    return out


def routine_rows(config, ids=None, lastwrite=None):
    from render import cron_local_times
    tz = local_zone(config)
    ids = ids or {}
    rows = []
    for r in config.get("routines") or []:
        name = r.get("name", "?")
        tmpl = r.get("template", "")
        rows.append({
            "name": name, "template": tmpl,
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


def wrap(text, width):
    out = []
    for para in (text or "").split("\n"):
        out.extend(textwrap.wrap(para, max(8, width), break_long_words=True, break_on_hyphens=False) or [""])
    return out


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

class LineEditor:
    """The text being typed into a prompt: insert mode, cursor keys, Unicode.

    `key` takes what curses get_wch() returns (a str, or an int for special
    keys) and returns "save", "cancel" or None. Kept free of curses so the
    tests can drive it.
    """

    def __init__(self, text="", width=60):
        self.buf, self.pos, self.width = list(text), len(text), max(1, width)

    @property
    def text(self):
        return "".join(self.buf)

    def rows(self):
        """The text cut into rows of `width`, and the cursor's (row, col)."""
        t, w = self.text, self.width
        rows = [t[i:i + w] for i in range(0, len(t), w)] or [""]
        if len(t) % w == 0 and t:
            rows.append("")  # cursor sits at the start of a fresh row
        return rows, divmod(self.pos, w)

    def key(self, k, keys=None):
        k_ = keys or {}
        if k in ("\n", "\r") or k == k_.get("ENTER"):
            return "save"
        if k == "\x1b":
            return "cancel"
        if k in ("\x7f", "\b") or k == k_.get("BACKSPACE"):
            if self.pos:
                self.pos -= 1
                del self.buf[self.pos]
        elif k == k_.get("DC") or k == "\x04":                        # Delete, Ctrl-D
            if self.pos < len(self.buf):
                del self.buf[self.pos]
        elif k == k_.get("LEFT") or k == "\x02":
            self.pos = max(0, self.pos - 1)
        elif k == k_.get("RIGHT") or k == "\x06":
            self.pos = min(len(self.buf), self.pos + 1)
        elif k == k_.get("UP"):
            self.pos = max(0, self.pos - self.width)
        elif k == k_.get("DOWN"):
            self.pos = min(len(self.buf), self.pos + self.width)
        elif k == k_.get("HOME") or k == "\x01":                      # Ctrl-A
            self.pos = 0
        elif k == k_.get("END") or k == "\x05":                       # Ctrl-E
            self.pos = len(self.buf)
        elif k == "\x15":                                             # Ctrl-U: clear
            self.buf, self.pos = [], 0
        elif k == "\x0b":                                             # Ctrl-K: to the end
            del self.buf[self.pos:]
        elif k == "\x17":                                             # Ctrl-W: word back
            i = self.pos
            while i and self.buf[i - 1] == " ":
                i -= 1
            while i and self.buf[i - 1] != " ":
                i -= 1
            del self.buf[i:self.pos]
            self.pos = i
        elif isinstance(k, str) and k.isprintable():
            for ch in k:                                               # a paste can arrive as several chars
                self.buf.insert(self.pos, ch)
                self.pos += 1
        return None


class Theme:
    """Color pairs. Falls back gracefully on terminals without 256 colors."""

    def __init__(self):
        import curses
        self.c = curses
        self.ok = curses.has_colors()
        self.unicode = "utf" in (locale.getpreferredencoding(False) or "").lower()
        if not self.ok:
            self.pairs = {}
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        many = curses.COLORS >= 256
        # name: (fg, bg)
        spec = {
            "bar":     (231 if many else curses.COLOR_WHITE, 24 if many else curses.COLOR_BLUE),
            "tab_on":  (16 if many else curses.COLOR_BLACK, 75 if many else curses.COLOR_CYAN),
            "accent":  (75 if many else curses.COLOR_CYAN, -1),
            "dim":     (243 if many else curses.COLOR_WHITE, -1),
            "ready":   (114 if many else curses.COLOR_GREEN, -1),
            "waiting": (221 if many else curses.COLOR_YELLOW, -1),
            "new":     (176 if many else curses.COLOR_MAGENTA, -1),
            "closed":  (240 if many else curses.COLOR_WHITE, -1),
            "sel":     (231 if many else curses.COLOR_WHITE, 237 if many else curses.COLOR_BLUE),
            "reply":   (114 if many else curses.COLOR_GREEN, -1),
            "kind_commitment": (117 if many else curses.COLOR_CYAN, -1),
            "kind_ask":        (213 if many else curses.COLOR_MAGENTA, -1),
            "kind_needs_reply": (222 if many else curses.COLOR_YELLOW, -1),
            "kind_loose_thread": (111 if many else curses.COLOR_BLUE, -1),
            "kind_fyi":        (245 if many else curses.COLOR_WHITE, -1),
            "err":     (203 if many else curses.COLOR_RED, -1),
            "msg":     (250 if many else curses.COLOR_WHITE, -1),
        }
        self.pairs = {}
        for n, (name, (fg, bg)) in enumerate(spec.items(), start=1):
            try:
                curses.init_pair(n, fg, bg)
                self.pairs[name] = curses.color_pair(n)
            except curses.error:
                self.pairs[name] = 0

    def a(self, name, *extra):
        attr = self.pairs.get(name, 0) if self.ok else 0
        for e in extra:
            attr |= e
        return attr

    def status_attr(self, status):
        return self.a({"ready": "ready", "active": "ready", "waiting": "waiting", "paused": "waiting",
                       "new": "new", "suggested": "new"}.get(status, "closed"))

    def g(self, uni, ascii_):
        return uni if self.unicode else ascii_


class App:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.t = Theme()
        self.screen = 1
        self.sel = {0: 0, 1: 0, 2: 0, 3: 0}
        self.rn_idx = 0
        self.detail_off = 0
        self.sort = {1: 0, 2: 0}
        self.rev = {1: False, 2: False}
        self.filt = {1: 0, 2: 0}
        self.msg = ""
        self.msg_err = False
        self.config = load_config()
        self.reload()

    # data ------------------------------------------------------------------
    def reload(self):
        self.items = load_all(include_archive=True)
        self.efforts = load_efforts(include_closed=True)
        self.routines = routine_rows(self.config, routine_ids_from_state(), last_writes(self.config))
        from releasenotes import all_docs
        self.rn_docs = all_docs()
        self.rn_idx = max(0, min(self.rn_idx, len(self.rn_docs) - 1))

    def rn_doc(self):
        return self.rn_docs[self.rn_idx] if self.rn_docs else None

    def rows(self):
        if self.screen == 1:
            return inbox_rows(self.items, INBOX_SORTS[self.sort[1]], self.rev[1], INBOX_FILTERS[self.filt[1]])
        if self.screen == 2:
            return effort_rows(self.efforts, EFFORT_SORTS[self.sort[2]], self.rev[2], EFFORT_FILTERS[self.filt[2]])
        if self.screen == 3:
            doc = self.rn_doc()
            return doc["tickets"] if doc else []
        return self.routines

    def current(self):
        rows = self.rows()
        if not rows:
            return None
        self.sel[self.screen] = max(0, min(self.sel[self.screen], len(rows) - 1))
        return rows[self.sel[self.screen]]

    def say(self, text, err=False):
        self.msg, self.msg_err = text, err

    # low-level drawing --------------------------------------------------------
    def put(self, y, x, text, attr=0, maxw=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        maxw = w - x if maxw is None else min(maxw, w - x)
        if maxw <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, maxw, attr)
        except self.t.c.error:
            pass

    def fill(self, y, x, width, attr):
        self.put(y, x, " " * max(0, width), attr)

    def box(self, y, x, h, w, title="", attr=0):
        t = self.t
        tl, tr, bl, br = t.g("╭", "+"), t.g("╮", "+"), t.g("╰", "+"), t.g("╯", "+")
        hz, vt = t.g("─", "-"), t.g("│", "|")
        self.put(y, x, tl + hz * (w - 2) + tr, attr)
        for yy in range(y + 1, y + h - 1):
            self.put(yy, x, vt, attr)
            self.put(yy, x + w - 1, vt, attr)
        self.put(y + h - 1, x, bl + hz * (w - 2) + br, attr)
        if title:
            self.put(y, x + 2, f" {title} ", attr | self.t.c.A_BOLD)

    # screens -------------------------------------------------------------------
    def draw(self):
        c = self.t.c
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.draw_header(w)
        rows = self.rows()
        side_by_side = w >= 100
        top, bottom = 2, h - 2
        if side_by_side:
            lw = max(52, min(int(w * 0.45), 80))
            self.draw_list(top, 0, bottom - top, lw, rows)
            self.draw_detail(top, lw + 1, bottom - top, w - lw - 1)
        else:
            lh = max(5, (bottom - top) // 2)
            self.draw_list(top, 0, lh, w, rows)
            self.draw_detail(top + lh, 0, bottom - top - lh, w)
        self.draw_footer(h, w)
        scr.refresh()

    def draw_header(self, w):
        c = self.t.c
        t = self.t
        self.fill(0, 0, w, t.a("bar"))
        self.put(0, 1, t.g("☰", "=") + " followup-inbox", t.a("bar", c.A_BOLD))
        x = 20
        for n, name in enumerate(("Routines", "Inbox", "Efforts", "Release notes")):
            label = f" {n} {name} "
            attr = t.a("tab_on", c.A_BOLD) if n == self.screen else t.a("bar")
            self.put(0, x, label, attr)
            x += len(label) + 1
        open_items = [i for i in self.items if i.get("status") in OPEN_STATUSES and not is_snoozed(i)]
        n_snoozed = sum(1 for i in self.items if is_snoozed(i))
        counts = {s: sum(1 for i in open_items if i["status"] == s) for s in ("ready", "waiting", "new")}
        drafts = sum(1 for i in open_items if i.get("proposed_reply"))
        sugg = sum(1 for e in self.efforts if e.get("status") == "suggested")
        right = (f"{counts['ready']} ready {t.g('·', '|')} {counts['waiting']} waiting {t.g('·', '|')} "
                 f"{counts['new']} new {t.g('·', '|')} {drafts} drafts {t.g('·', '|')} {sugg} suggested efforts"
                 + (f" {t.g('·', '|')} {n_snoozed} snoozed" if n_snoozed else ""))
        tz = local_zone(self.config)
        clock = now_utc().astimezone(tz).strftime("%a %d %b %H:%M")
        self.put(0, max(x + 2, w - len(right) - len(clock) - 4), right, t.a("bar"))
        self.put(0, w - len(clock) - 1, clock, t.a("bar", c.A_BOLD))
        # sub-header: sort / filter
        if self.screen == 1:
            sub = (f"  {'src':<7} {'kind':<7} {'age':>3} {'who':<12} title            "
                   f"sort {INBOX_SORTS[self.sort[1]]}{' ↓' if self.rev[1] else ''}   filter {INBOX_FILTERS[self.filt[1]]}")
        elif self.screen == 2:
            sub = f"sort {EFFORT_SORTS[self.sort[2]]}{' ↓' if self.rev[2] else ''}   filter {EFFORT_FILTERS[self.filt[2]]}"
        elif self.screen == 3:
            doc = self.rn_doc()
            if doc:
                from releasenotes import counts
                cnt = counts(doc)
                due = dt.date.fromisoformat(doc["due"]).strftime("%a %d %b") if doc.get("due") else "?"
                versions = ", ".join(doc.get("jira_versions") or []) or doc["release"]
                sub = (f"{versions}   {doc['status']}   due {due}   {cnt['included']}/{cnt['tickets']} customer facing"
                       + (f"   {cnt['applied']} applied" if doc["status"] in ("approved", "applied") else "")
                       + (f"   release {self.rn_idx + 1}/{len(self.rn_docs)}  [ ] to switch" if len(self.rn_docs) > 1 else ""))
            else:
                sub = "no release notes yet: r asks the release-notes routine for a draft of the next release"
        else:
            sub = "crons are UTC; 'today' is the local firing time; 'last wrote' is the newest store commit from that scanner"
        self.put(1, 1, sub, t.a("dim"))

    def draw_list(self, y, x, h, w, rows):
        c, t = self.t.c, self.t
        if not rows:
            self.put(y + 1, x + 2, "nothing here", t.a("dim", c.A_ITALIC if hasattr(c, "A_ITALIC") else 0))
            return
        top = max(0, self.sel[self.screen] - h + 1)
        for n, row in enumerate(rows[top: top + h]):
            yy = y + n
            selected = top + n == self.sel[self.screen]
            base = t.a("sel") if selected else 0
            self.fill(yy, x, w, base)
            marker = t.g("▌", ">") if selected else " "
            self.put(yy, x, marker, t.a("accent", c.A_BOLD) if selected else 0)
            if self.screen == 1:
                self.draw_inbox_row(yy, x + 2, w - 3, row, base, selected)
            elif self.screen == 2:
                self.draw_effort_row(yy, x + 2, w - 3, row, base, selected)
            elif self.screen == 3:
                self.draw_note_row(yy, x + 2, w - 3, row, base, selected)
            else:
                self.draw_routine_row(yy, x + 2, w - 3, row, base, selected)
        if len(rows) > h:
            pos = f"{self.sel[self.screen] + 1}/{len(rows)}"
            self.put(y + h - 1, x + w - len(pos) - 1, pos, t.a("dim"))

    def draw_inbox_row(self, y, x, w, it, base, selected):
        c, t = self.t.c, self.t
        st = it.get("status", "")
        self.put(y, x, STATUS_GLYPH.get(st, "?"), (base if selected else 0) | (t.status_attr(st) if not selected else c.A_BOLD))
        src = it.get("source") or ""
        self.put(y, x + 2, f"{SOURCE_GLYPH.get(src, '?')} {SOURCE_LABEL.get(src, src)[:5]:<5}", base | t.a("dim"))
        kind = KIND_LABEL.get(it.get("kind"), it.get("kind", ""))[:7]
        self.put(y, x + 10, f"{kind:<7}", base | (t.a("kind_" + str(it.get("kind"))) if not selected else c.A_BOLD))
        self.put(y, x + 18, f"{fmt_age(it['created_at']):>3}", base | t.a("dim"))
        who = (it.get("actor") or "").split(",")[0].split(" (")[0][:12]
        self.put(y, x + 22, f"{who:<12}", base | (t.a("accent") if not selected else 0))
        title_w = w - 36
        title = it.get("title") or ""
        if is_snoozed(it):
            title = t.g("☾ ", "z ") + parse_ts(it["snoozed_until"]).astimezone(local_zone(self.config)).strftime("%d %b  ") + title
        elif woke_recently(it):
            title = t.g("↺ ", "^ ") + title
        if it.get("proposed_reply"):
            self.put(y, x + w - 1, t.g("✎", "*"), base | t.a("reply", c.A_BOLD))
            title_w -= 2
        self.put(y, x + 35, title[:title_w], base | (c.A_BOLD if selected else 0), title_w)

    def draw_effort_row(self, y, x, w, e, base, selected):
        c, t = self.t.c, self.t
        st = e.get("status", "")
        self.put(y, x, STATUS_GLYPH.get(st, "?"), base | (t.status_attr(st) if not selected else c.A_BOLD))
        self.put(y, x + 2, f"{(e.get('kind') or '')[:9]:<9}", base | t.a("dim"))
        due = str(e.get("next_action_due") or "")[5:10]
        self.put(y, x + 12, f"{due:<5}", base | (t.a("waiting") if due else 0))
        title_w = w - 20
        title = e.get("title") or ""
        if not e.get("next_action") and e.get("suggestion"):
            self.put(y, x + w - 1, t.g("✦", "*"), base | t.a("new", c.A_BOLD))
            title_w -= 2
        self.put(y, x + 18, title[:title_w], base | (c.A_BOLD if selected else 0), title_w)

    def draw_note_row(self, y, x, w, tk, base, selected):
        c, t = self.t.c, self.t
        box = "[x]" if tk.get("include") else "[ ]"
        self.put(y, x, box, base | (t.a("ready", c.A_BOLD) if tk.get("include") and not selected else 0))
        self.put(y, x + 4, f"{tk['key']:<11}", base | t.a("accent") if not selected else base)
        mark = t.g("✓", "+") if tk.get("applied") else ("!" if tk.get("apply_error") else (t.g("✎", "*") if tk.get("edited") else " "))
        self.put(y, x + 16, mark, base | (t.a("reply", c.A_BOLD) if not selected else c.A_BOLD))
        self.put(y, x + 18, (tk.get("summary") or "")[: w - 18], base | (c.A_BOLD if selected else 0), w - 18)

    def draw_routine_row(self, y, x, w, r, base, selected):
        c, t = self.t.c, self.t
        self.put(y, x, t.g("●", "*") if r["enabled"] else t.g("○", "o"), base | (t.a("ready") if r["enabled"] else t.a("closed")))
        self.put(y, x + 2, f"{r['name'][:26]:<26}", base | (c.A_BOLD if selected else 0))
        self.put(y, x + 29, f"today {r['local']}"[: w - 30], base | t.a("dim"))

    def draw_detail(self, y, x, h, w):
        c, t = self.t.c, self.t
        cur = self.current()
        if cur is None:
            return
        if self.screen == 1:
            title = f"{cur.get('source')} {t.g('·', '/')} {KIND_LABEL.get(cur.get('kind'), cur.get('kind'))}"
        elif self.screen == 2:
            title = f"effort {t.g('·', '/')} {cur.get('kind')}"
        elif self.screen == 3:
            title = f"{cur['key']} {t.g('·', '/')} {'customer facing' if cur.get('include') else 'internal'}"
        else:
            title = "routine"
        self.box(y, x, h, w, title, t.a("dim"))
        inner_w = w - 4
        lines = self.detail_lines(cur, inner_w)
        avail = h - 2
        self.detail_off = max(0, min(self.detail_off, max(0, len(lines) - avail)))
        for n, (text, attr) in enumerate(lines[self.detail_off: self.detail_off + avail]):
            self.put(y + 1 + n, x + 2, text, attr, inner_w)
        if len(lines) > avail:
            pct = f" {min(100, int(100 * (self.detail_off + avail) / len(lines)))}% "
            self.put(y + h - 1, x + w - len(pct) - 2, pct, t.a("dim"))

    def detail_lines(self, cur, w):
        """[(text, attr)] for the right pane."""
        c, t = self.t.c, self.t
        L = []
        bold = c.A_BOLD
        if self.screen == 1:
            for ln in wrap(cur.get("title", ""), w):
                L.append((ln, bold))
            L.append(("", 0))
            st = cur.get("status", "")
            L.append((f"{STATUS_GLYPH.get(st, '?')} {st}", t.status_attr(st) | bold))
            tz = local_zone(self.config)
            created = parse_ts(cur["created_at"]).astimezone(tz).strftime("%a %d %b %Y %H:%M")
            meta = [("who", cur.get("actor")), ("trigger", cur.get("trigger")),
                    ("created", f"{created}  ({fmt_age(cur['created_at'])} ago)"), ("id", cur.get("id")),
                    ("link", cur.get("url")), ("resolved", cur.get("resolution")),
                    ("snoozed", ("until " + parse_ts(cur["snoozed_until"]).astimezone(tz).strftime("%a %d %b %H:%M"))
                     if is_snoozed(cur) else None),
                    ("back", "from snooze, " + parse_ts(cur["snoozed_until"]).astimezone(tz).strftime("%a %d %b %H:%M")
                     if woke_recently(cur) else None)]
            for k, v in meta:
                if v:
                    for n, ln in enumerate(wrap(str(v), w - 10)):
                        L.append(((f"{k:<8}" if n == 0 else " " * 8) + "  " + ln, t.a("dim") if n else 0))
            L.append(("", 0))
            L.append((t.g("─", "-") * w, t.a("dim")))
            for ln in wrap(cur.get("body") or "(no detail)", w):
                L.append((ln, 0))
            if cur.get("proposed_reply"):
                L.append(("", 0))
                L.append((f"{t.g('✎', '*')} proposed reply   (y to copy)", t.a("reply", bold)))
                L.append((t.g("─", "-") * w, t.a("reply")))
                for ln in wrap(cur["proposed_reply"], w - 2):
                    L.append((t.g("┃ ", "| ") + ln, t.a("reply")))
        elif self.screen == 2:
            for ln in wrap(cur.get("title", ""), w):
                L.append((ln, bold))
            L.append(("", 0))
            st = cur.get("status", "")
            L.append((f"{STATUS_GLYPH.get(st, '?')} {st}", t.status_attr(st) | bold))
            meta = [("next", cur.get("next_action")), ("due", cur.get("next_action_due")),
                    ("touched", cur.get("last_touched")), ("id", cur.get("id")), ("link", cur.get("url"))]
            for k, v in meta:
                if v:
                    for n, ln in enumerate(wrap(str(v), w - 10)):
                        L.append(((f"{k:<8}" if n == 0 else " " * 8) + "  " + ln, t.a("dim") if n else 0))
            if cur.get("suggestion"):
                L.append(("", 0))
                L.append((f"{t.g('✦', '*')} suggested next action   (A to accept, n to set your own)", t.a("new", bold)))
                L.append((t.g("─", "-") * w, t.a("new")))
                for ln in wrap(cur["suggestion"], w - 2):
                    L.append((t.g("┃ ", "| ") + ln, t.a("new")))
            if cur.get("body"):
                L.append(("", 0))
                L.append((t.g("─", "-") * w, t.a("dim")))
                for ln in wrap(cur["body"], w):
                    L.append((ln, 0))
        elif self.screen == 3:
            for ln in wrap(cur.get("summary") or "", w):
                L.append((ln, bold))
            L.append(("", 0))
            state = ("applied" if cur.get("applied") else f"failed: {cur['apply_error']}" if cur.get("apply_error")
                     else "edited by you" if cur.get("edited") else "as drafted")
            for k, v in (("type", cur.get("type")), ("status", cur.get("status")), ("link", cur.get("url")),
                         ("state", state), ("why", cur.get("reason"))):
                if v:
                    for n, ln in enumerate(wrap(str(v), w - 10)):
                        L.append(((f"{k:<8}" if n == 0 else " " * 8) + "  " + ln, t.a("dim") if n else 0))
            L.append(("", 0))
            if cur.get("include"):
                L.append((f"{t.g('✎', '*')} release note   (Enter to edit, space to drop)", t.a("reply", bold)))
                L.append((t.g("─", "-") * w, t.a("reply")))
                for ln in wrap(cur.get("note") or "(no note yet: Enter to write one)", w - 2):
                    L.append((t.g("┃ ", "| ") + ln, t.a("reply")))
            else:
                L.append(("not in the release notes   (space to include)", t.a("dim")))
                if cur.get("note"):
                    for ln in wrap(cur["note"], w - 2):
                        L.append((t.g("┃ ", "| ") + ln, t.a("dim")))
        else:
            L.append((cur["name"], bold))
            L.append(("", 0))
            meta = [("enabled", "yes" if cur["enabled"] else "no"), ("cron", f"{cur['cron']}  (UTC)"),
                    ("today", cur["local"]), ("last wrote", cur["last_write"]), ("connectors", cur["connectors"]),
                    ("template", f"routines/{cur['template']}.md"), ("routine id", cur["id"])]
            for k, v in meta:
                L.append((f"{k:<11}  {v}", 0))
            path = os.path.join(RENDERED, cur["name"] + ".md")
            if os.path.exists(path):
                L.append(("", 0))
                L.append((f"rendered prompt  {rel(path)}", t.a("dim")))
                L.append((t.g("─", "-") * w, t.a("dim")))
                with open(path, encoding="utf-8") as f:
                    for ln in wrap(f.read(), w):
                        L.append((ln, t.a("dim")))
            else:
                L.append(("", 0))
                L.append(("not rendered yet: python3 tool/scripts/render.py", t.a("dim")))
        return L

    def draw_footer(self, h, w):
        c, t = self.t.c, self.t
        if self.screen == 1:
            keys = [("d", "done"), ("x", "dismiss"), ("z", "snooze"), ("r", "ready"), ("w", "waiting"), ("m", "status…"),
                    ("y", "copy draft"), ("o", "open"), ("e", "edit"), ("s", "sort"), ("f", "filter"), ("g", "sync"), ("?", "help")]
        elif self.screen == 2:
            keys = [("A", "accept"), ("n", "next"), ("N", "note"), ("a", "add"), ("p", "pause"), ("d", "done"),
                    ("x", "drop"), ("o", "open"), ("e", "edit"), ("s", "sort"), ("f", "filter"), ("g", "sync")]
        elif self.screen == 3:
            keys = [("space", "include"), ("Enter", "edit note"), ("A", "approve"), ("U", "reopen"), ("r", "request draft"),
                    ("[ ]", "release"), ("o", "open"), ("g", "sync")]
        else:
            keys = [("o", "routines page"), ("R", "reload"), ("q", "quit")]
        x = 1
        for k, label in keys:
            self.put(h - 2, x, f" {k} ", t.a("tab_on", c.A_BOLD))
            self.put(h - 2, x + 3, f" {label}  ", t.a("dim"))
            x += 5 + len(label)
            if x > w - 12:
                break
        if self.msg:
            self.put(h - 1, 1, self.msg, t.a("err" if self.msg_err else "msg"), w - 2)

    # input --------------------------------------------------------------------
    def prompt(self, label, default=""):
        """Modal text box. Returns the text (or `default` if left empty), None on Esc."""
        c, t = self.t.c, self.t
        h, w = self.scr.getmaxyx()
        bw = max(20, min(w - 4, 78))
        text_rows = 4
        bh = text_rows + 4
        y, x = max(1, (h - bh) // 2), max(0, (w - bw) // 2)
        ed = LineEditor(default, bw - 4)
        keys = {n: getattr(c, "KEY_" + n) for n in ("ENTER", "BACKSPACE", "DC", "LEFT", "RIGHT", "UP", "DOWN", "HOME", "END")}
        hint = "Enter save · Esc cancel · ←→ ↑↓ · Ctrl-A/E start/end · Ctrl-U clear"
        try:
            c.set_escdelay(25)
        except (AttributeError, c.error):
            pass
        c.curs_set(1)
        try:
            while True:
                for yy in range(y, y + bh):
                    self.fill(yy, x, bw, 0)
                self.box(y, x, bh, bw, label, t.a("accent"))
                rows, (crow, ccol) = ed.rows()
                top = max(0, crow - text_rows + 1)
                for n, row in enumerate(rows[top:top + text_rows]):
                    self.put(y + 1 + n, x + 2, row)
                self.put(y + bh - 2, x + 2, hint, t.a("dim"), bw - 4)
                self.scr.move(y + 1 + crow - top, x + 2 + ccol)
                self.scr.refresh()
                try:
                    k = self.scr.get_wch()
                except KeyboardInterrupt:
                    return None
                except c.error:
                    continue
                if k == c.KEY_RESIZE:
                    h, w = self.scr.getmaxyx()
                    continue
                if k == "\x1b":
                    k = self._escape_sequence(keys)  # an arrow curses did not translate, or a real Esc
                done = ed.key(k, keys)
                if done == "cancel":
                    return None
                if done == "save":
                    return ed.text.strip() or default
        finally:
            c.curs_set(0)

    def _escape_sequence(self, keys):
        """After an Esc, read what follows at once: `[D` / `OD` and friends are arrows, nothing is Esc."""
        self.scr.nodelay(True)
        try:
            seq = ""
            for _ in range(3):
                try:
                    seq += str(self.scr.get_wch())
                except self.t.c.error:
                    break
        finally:
            self.scr.nodelay(False)
        arrows = {"D": "LEFT", "C": "RIGHT", "A": "UP", "B": "DOWN", "H": "HOME", "F": "END"}
        if len(seq) >= 2 and seq[0] in "[O" and seq[-1] in arrows:
            return keys[arrows[seq[-1]]]
        return "\x1b"

    def pick(self, title, options, current=None):
        """Modal list picker; returns the chosen option or None."""
        c, t = self.t.c, self.t
        h, w = self.scr.getmaxyx()
        bw = max(len(title) + 6, max(len(o) for o in options) + 8)
        bh = len(options) + 2
        y, x = max(1, (h - bh) // 2), max(0, (w - bw) // 2)
        idx = options.index(current) if current in options else 0
        while True:
            self.box(y, x, bh, bw, title, t.a("accent"))
            for n, o in enumerate(options):
                attr = t.a("sel", c.A_BOLD) if n == idx else 0
                self.fill(y + 1 + n, x + 1, bw - 2, attr)
                self.put(y + 1 + n, x + 2, f"{STATUS_GLYPH.get(o, ' ')} {o}", attr | (0 if n == idx else t.status_attr(o)))
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (ord("q"), 27):
                return None
            if ch in (ord("j"), c.KEY_DOWN):
                idx = (idx + 1) % len(options)
            elif ch in (ord("k"), c.KEY_UP):
                idx = (idx - 1) % len(options)
            elif ch in (10, 13, c.KEY_ENTER):
                return options[idx]
            elif 0 <= ch < 256 and chr(ch) in "".join(o[0] for o in options):
                for o in options:
                    if o[0] == chr(ch):
                        return o

    def edit_in_editor(self, path):
        c = self.t.c
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        c.endwin()
        subprocess.call([editor, path])
        self.scr.refresh()
        self.reload()

    def help(self):
        self.say("0/1/2/3 screens · j/k move · J/K scroll detail · Enter/m status menu · s/S sort · f filter · R reload · g sync · o open · e edit · q quit"
                 "  |  Inbox: d done x dismiss z snooze Z wake r ready w waiting n reopen y copy  |  Efforts: a add A accept n next N note p pause d done x drop  |  Notes: space include Enter edit A approve U reopen r request [ ] release")

    # actions -----------------------------------------------------------------
    def set_item_status(self, cur, new):
        extra = ["--reopen"] if cur.get("status") in ("done", "dismissed") and new not in ("done", "dismissed") else []
        if new in ("done", "dismissed") and cur.get("status") not in ("done", "dismissed"):
            ask = "how was it resolved?" if new == "done" else "why dismiss it?"
            note = self.prompt(f"{ask} (Enter on empty to skip)")
            if note is None:
                self.say("cancelled")
                return
            if note:
                extra += ["--note", note]
        ok, out = run_script("status.py", cur["id"], new, *extra)
        self.say(out, not ok)
        self.reload()

    def act(self, key):
        cur = self.current()
        if self.screen == 1 and cur:
            if key in ("d", "x", "r", "w", "n"):
                self.set_item_status(cur, {"d": "done", "x": "dismissed", "r": "ready", "w": "waiting", "n": "new"}[key])
                return
            if key == "m":
                new = self.pick("set status", list(STATUSES), cur.get("status"))
                if new and new != cur.get("status"):
                    self.set_item_status(cur, new)
                return
            if key == "z":
                if cur.get("status") not in OPEN_STATUSES:
                    self.say("only open items can be snoozed", True)
                    return
                when = self.prompt("snooze until: 3 · 2w · 4h · tomorrow · fri · next-week · next-release · 14.10")
                if not when:
                    self.say("cancelled")
                    return
                ok, out = run_script("snooze.py", cur["id"], when)
                self.say(out, not ok)
                self.reload()
                return
            if key == "Z":
                ok, out = run_script("snooze.py", cur["id"], "--wake")
                self.say(out, not ok)
                self.reload()
                return
            if key == "y":
                text = cur.get("proposed_reply") or ""
                self.say("copied the proposed reply to the clipboard" if text and copy_to_clipboard(text)
                         else "no proposed reply on this item, or no clipboard tool found", not text)
                return
        if self.screen == 2:
            if key == "a":
                title = self.prompt("title:")
                if not title:
                    self.say("cancelled")
                    return
                kind = self.pick("kind", ["epic", "customer", "marketing", "coordination", "research", "other"]) or "other"
                nxt = self.prompt("next action (optional):")
                ok, out = run_script("efforts.py", "add", "--title", title, "--kind", kind, *(["--next", nxt] if nxt else []))
                self.say(out, not ok)
                self.reload()
                return
            if cur and key in ("A", "p", "d", "x", "m"):
                if key == "m":
                    new = self.pick("set status", list(EFFORT_STATUSES), cur.get("status"))
                    if not new or new == cur.get("status"):
                        return
                else:
                    new = {"A": "active", "p": "active" if cur.get("status") == "paused" else "paused",
                           "d": "done", "x": "dropped"}[key]
                ok, out = run_script("efforts.py", "status", cur["id"], new)
                self.say(out, not ok)
                self.reload()
                return
            if cur and key == "n":
                text = self.prompt("next action:", cur.get("next_action") or "")
                if text:
                    due = self.prompt("due (YYYY-MM-DD, optional):", cur.get("next_action_due") or "")
                    ok, out = run_script("efforts.py", "next", cur["id"], text, *(["--due", due] if due else []))
                    self.say(out, not ok)
                    self.reload()
                return
            if cur and key == "N":
                text = self.prompt("note:")
                if text:
                    ok, out = run_script("efforts.py", "note", cur["id"], text)
                    self.say(out, not ok)
                    self.reload()
                return
        if self.screen == 3:
            if self.act_notes(key, cur):
                return
        if key == "g":
            self.say("syncing…")
            self.draw()
            ok, out = run_script("sync.py", "-m", "triage", "items", "archive", "efforts", "knowledge", "releasenotes",
                                 "state/releases.json", "INBOX.md")
            self.say(out, not ok)
            self.reload()
            return
        if key == "o":
            url = (cur or {}).get("url") if self.screen != 0 else "https://claude.ai/code/routines"
            self.say("opened in the browser" if open_url(url) else "no link on this one", not url)
            return
        if key == "e" and cur and self.screen in (1, 2):
            self.edit_in_editor(cur["_path"])
            return

    def act_notes(self, key, cur):
        """Keys on the release-notes screen. True when the key was handled."""
        doc = self.rn_doc()
        if key in ("[", "]"):
            if self.rn_docs:
                self.rn_idx = (self.rn_idx + (1 if key == "]" else -1)) % len(self.rn_docs)
                self.sel[3] = 0
            return True
        if key == "r":
            ok, out = run_script("releasenotes.py", "request")
            self.say(out + ("   (the release-notes routine drafts it on its next run; g to sync first)" if ok else ""), not ok)
            self.reload()
            return True
        if not doc:
            return key in (" ", "m", "A", "U")
        if key == "A":
            if self.pick(f"approve {doc['release']}? the routine then writes to the tracker", ["no", "yes"]) != "yes":
                self.say("not approved")
                return True
            ok, out = run_script("releasenotes.py", "approve", doc["release"])
            self.say(out + ("   (g to sync, so the routine sees it)" if ok else ""), not ok)
            self.reload()
            return True
        if key == "U":
            ok, out = run_script("releasenotes.py", "reopen", doc["release"])
            self.say(out, not ok)
            self.reload()
            return True
        if cur and key == " ":
            ok, out = run_script("releasenotes.py", "set", doc["release"], cur["key"],
                                 "--include", "no" if cur.get("include") else "yes")
            self.say(out, not ok)
            self.reload()
            return True
        if cur and key == "m":
            text = self.prompt(f"release note for {cur['key']}", cur.get("note") or "")
            if text is None:
                self.say("cancelled")
                return True
            args = ["--note", text] + ([] if cur.get("include") or not text else ["--include", "yes"])
            ok, out = run_script("releasenotes.py", "set", doc["release"], cur["key"], *args)
            self.say(out, not ok)
            self.reload()
            return True
        return False

    # main loop --------------------------------------------------------------
    def run(self):
        c = self.t.c
        c.curs_set(0)
        self.scr.keypad(True)
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch in (ord("q"), 27):
                return
            if ch == c.KEY_RESIZE:
                continue
            if ch in (ord("0"), ord("1"), ord("2"), ord("3")):
                self.screen = ch - ord("0"); self.detail_off = 0
            elif ch == 9:
                self.screen = (self.screen + 1) % 4; self.detail_off = 0
            elif ch == c.KEY_BTAB:
                self.screen = (self.screen - 1) % 4; self.detail_off = 0
            elif ch in (ord("j"), c.KEY_DOWN):
                self.sel[self.screen] += 1; self.detail_off = 0
            elif ch in (ord("k"), c.KEY_UP):
                self.sel[self.screen] -= 1; self.detail_off = 0
            elif ch in (ord("J"), c.KEY_NPAGE):
                self.detail_off += 5
            elif ch in (ord("K"), c.KEY_PPAGE):
                self.detail_off = max(0, self.detail_off - 5)
            elif ch in (ord("G"), c.KEY_END):
                self.sel[self.screen] = 10 ** 6
            elif ch in (ord("g"),) and False:
                pass
            elif ch in (10, 13, c.KEY_ENTER):
                self.act("m")
            elif ch == ord("s") and self.screen in self.sort:
                n = len(INBOX_SORTS if self.screen == 1 else EFFORT_SORTS)
                self.sort[self.screen] = (self.sort[self.screen] + 1) % n
            elif ch == ord("S") and self.screen in self.rev:
                self.rev[self.screen] = not self.rev[self.screen]
            elif ch == ord("f") and self.screen in self.filt:
                n = len(INBOX_FILTERS if self.screen == 1 else EFFORT_FILTERS)
                self.filt[self.screen] = (self.filt[self.screen] + 1) % n; self.sel[self.screen] = 0
            elif ch == ord("R"):
                self.config = load_config(); self.reload(); self.say("reloaded")
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
    for r in routine_rows(config, routine_ids_from_state(), last_writes(config)):
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
    locale.setlocale(locale.LC_ALL, "")
    import curses
    try:
        curses.wrapper(lambda scr: App(scr).run())
    except InboxError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
