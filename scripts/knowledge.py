#!/usr/bin/env python3
"""Knowledge: what closed items taught, so drafts get closer to what the user actually sends.

Usage:
  ./knowledge.py pending [--min-hours H] [--limit N] [--json]   closed items not yet learned from, oldest close first
  ./knowledge.py context [--examples N]                          what a drafting routine reads before it drafts
  ./knowledge.py add --json < facts.json                         [{"topic", "key", "text", "source"}, ...]
  ./knowledge.py add --topic process --key br-schedule --text "..." --source "<item id>"
  ./knowledge.py drop --topic process --key br-schedule          consolidation: remove one learned entry
  ./knowledge.py mark "<item id>" --outcome replied|acted|dismissed|unknown
                      [--sent-file F|-] [--example] [--note "what the user changed"]
  ./knowledge.py stats [--weeks N] [--json]                      outcomes and draft survival per week

The store is knowledge/ in the instance:
  voice.md product.md people.md process.md triage.md   one topic each
  examples/<item file>.md                              drafted vs sent, newest `learn.examples_kept` kept
  ledger.tsv                                           every closed item already learned from

A topic file has a `## Pinned` and a `## Learned` section. Learned entries end
in a tag, `{k:<key> seen:<n> last:<date> src:<item ids>}`, and are the only
lines this script changes. Lines without a tag are the user's and are never
touched; a tagged entry the user moves under `## Pinned` is never rewritten
or dropped. To make the routine forget something, delete the line.

`add` on a known key confirms it (seen +1) or, if the text changed, replaces it
and prints the old text. `mark` records the outcome of a closed item in the
ledger; for `replied` with a draft on the item and `--sent-file`, it scores how
much of the draft survived into what was sent (0-1, word-level). Everything
here is idempotent: `mark` on a ledgered id reports `exists`.
"""

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import sys

from _inbox_common import (CLOSED_STATUSES, KNOWLEDGE_DIR, KNOWLEDGE_TOPICS, LEARN_OUTCOMES,
                           InboxError, _dump_scalar, cfg_get, die, find_path, git, id_to_filename, is_git_repo,
                           iso_utc, load_all, load_config, now_utc, parse_item, parse_ts, read_item, rel,
                           validate_id)

EXAMPLES_DIR = os.path.join(KNOWLEDGE_DIR, "examples")
LEDGER_PATH = os.path.join(KNOWLEDGE_DIR, "ledger.tsv")
LEDGER_FIELDS = ("item", "learned_at", "outcome", "score", "closed_at")

DEFAULTS = {"min_hours_closed": 2, "max_per_run": 30, "max_entries_per_topic": 60,
            "examples_kept": 20, "examples_in_context": 8}

_TAG = re.compile(r"\s*`\{k:([a-z0-9][a-z0-9-]*) seen:(\d+) last:(\S+) src:([^}]*)\}`\s*$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")

TITLES = {"voice": "Voice", "product": "Product", "people": "People", "process": "Process", "triage": "Triage"}
BLURBS = {
    "voice": "How the user writes, learned from what they sent versus what was drafted.",
    "product": "Durable facts about the product: components, versions, terms, customers, known limits.",
    "people": "Who is who: roles, ownership, what each person cares about, how the user addresses them.",
    "process": "How work gets done: meetings, sprints, releases, where which decision is made.",
    "triage": "What the user does not want raised, learned from what they dismissed.",
}
HEADER = """# {title}

{blurb}

Written by the learn routine through `knowledge.py`. Edit freely: delete a line
to make it forget, move a line under `## Pinned` to protect it. Lines without a
trailing `{{k:…}}` tag are yours and are never touched.

## Pinned

## Learned
"""


def _cfg(name):
    return cfg_get(load_config(), f"learn.{name}", DEFAULTS[name])


def _today():
    return now_utc().strftime("%Y-%m-%d")


def topic_path(topic):
    if topic not in KNOWLEDGE_TOPICS:
        raise InboxError(f"bad topic {topic!r}; want one of {', '.join(KNOWLEDGE_TOPICS)}")
    return os.path.join(KNOWLEDGE_DIR, topic + ".md")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Topic files
# ----------------------------------------------------------------------------

def read_topic(topic):
    """Lines of the topic file, and its tagged entries: key -> {line, section, text, seen, last, src}."""
    path = topic_path(topic)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = HEADER.format(title=TITLES[topic], blurb=BLURBS[topic]).splitlines()
    entries, section = {}, None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        m = _TAG.search(line)
        if line.startswith("- ") and m:
            entries[m.group(1)] = {"line": i, "section": section, "text": line[2:m.start()].strip(),
                                   "seen": int(m.group(2)), "last": m.group(3),
                                   "src": [s for s in m.group(4).split(",") if s]}
    return lines, entries


def _entry_line(key, text, seen, last, src):
    return f"- {text} `{{k:{key} seen:{seen} last:{last} src:{','.join(src)}}}`"


def _learned_end(lines):
    """Index after the last line of the Learned section (where a new entry goes)."""
    start = next((i for i, l in enumerate(lines) if l.strip().lower() == "## learned"), None)
    if start is None:
        lines.extend(["", "## Learned"])
        return len(lines)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _norm(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def add_fact(topic, key, text, source):
    if not key or not _KEY.match(key):
        raise InboxError(f"bad key {key!r} (lowercase letters, digits and dashes, at most 60)")
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        raise InboxError("empty text")
    if "`{k:" in text:
        raise InboxError("text may not contain a knowledge tag")
    if source:
        validate_id(source)
    lines, entries = read_topic(topic)
    old = entries.get(key)
    if old and old["section"] == "pinned":
        return "pinned", old["text"]
    if old:
        src = ([source] if source and source not in old["src"] else []) + old["src"]
        outcome = "confirmed" if _norm(old["text"]) == _norm(text) else "updated"
        new_text = old["text"] if outcome == "confirmed" else text
        lines[old["line"]] = _entry_line(key, new_text, old["seen"] + 1, _today(), src[:3])
        previous = old["text"] if outcome == "updated" else None
    else:
        lines.insert(_learned_end(lines), _entry_line(key, text, 1, _today(), [source] if source else []))
        outcome, previous = "added", None
    _write(topic_path(topic), "\n".join(lines).rstrip("\n") + "\n")
    return outcome, previous


def drop_fact(topic, key):
    lines, entries = read_topic(topic)
    old = entries.get(key)
    if not old:
        raise InboxError(f"no learned entry {key!r} in {topic}")
    if old["section"] == "pinned":
        raise InboxError(f"{topic}/{key} is pinned; only the user removes it")
    del lines[old["line"]]
    _write(topic_path(topic), "\n".join(lines).rstrip("\n") + "\n")
    return old["text"]


def learned_count(topic):
    return sum(1 for e in read_topic(topic)[1].values() if e["section"] != "pinned")


# ----------------------------------------------------------------------------
# Ledger and the work queue
# ----------------------------------------------------------------------------

def read_ledger():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _append_ledger(row):
    new = not os.path.exists(LEDGER_PATH)
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, delimiter="\t", lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in LEDGER_FIELDS})


def archive_times():
    """path (relative) -> when it was last added under archive/, from git. Empty without history."""
    if not is_git_repo():
        return {}
    out = git("log", "--format=%x00%cI", "--name-only", "--diff-filter=A", "--", "archive", check=False).stdout
    times = {}
    for chunk in out.split("\x00")[1:]:
        head, _, names = chunk.partition("\n")
        for name in names.splitlines():
            if name.strip():
                times.setdefault(name.strip(), head.strip())  # log is newest first: keep the latest close
    return times


def pending(min_hours, limit):
    learned = {r["item"] for r in read_ledger()}
    times = archive_times()
    cutoff = now_utc() - dt.timedelta(hours=min_hours)
    rows = []
    for it in load_all(include_archive=True):
        if it.get("status") not in CLOSED_STATUSES or it["id"] in learned:
            continue
        closed = parse_ts(times.get(rel(it["_path"])))
        if closed is not None and closed > cutoff:
            continue  # closed too recently: the reply may not be sent yet
        it["closed_at"] = closed
        rows.append(it)
    # unknown close time (no history, e.g. a shallow clone) counts as old
    rows.sort(key=lambda it: (it["closed_at"] is not None, it["closed_at"] or now_utc()))
    return rows[:limit]


# ----------------------------------------------------------------------------
# Examples and scoring
# ----------------------------------------------------------------------------

def survival(draft, sent):
    """How much of the draft made it into what was sent, word-level, 0-1."""
    a, b = re.findall(r"\w+", draft.lower()), re.findall(r"\w+", sent.lower())
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 2)


def write_example(item, sent, score, note):
    path = os.path.join(EXAMPLES_DIR, id_to_filename(item["id"]))
    head = ["---"] + [f"{k}: {_dump_scalar(v)}" for k, v in (
        ("item", item["id"]), ("date", _today()), ("kind", item.get("kind")), ("actor", item.get("actor")),
        ("score", score))] + ["---", ""]
    parts = [f"**Context:** {item.get('title') or ''}", ""]
    if item.get("resolution"):
        parts += [f"**Resolution (the user's note):** {item['resolution']}", ""]
    if item.get("proposed_reply"):
        parts += ["## Drafted", "", item["proposed_reply"].strip("\n"), ""]
    parts += ["## Sent", "", sent.strip("\n"), ""]
    if note:
        parts += ["## What changed", "", note.strip(), ""]
    _write(path, "\n".join(head + parts))
    _prune_examples(_cfg("examples_kept"))
    return path


def load_examples():
    out = []
    if not os.path.isdir(EXAMPLES_DIR):
        return out
    for name in sorted(os.listdir(EXAMPLES_DIR)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(EXAMPLES_DIR, name)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            meta = parse_item(text, path)
        except InboxError:
            meta = {}
        out.append({"path": path, "date": str(meta.get("date") or ""), "text": text})
    out.sort(key=lambda e: (e["date"], e["path"]), reverse=True)
    return out


def _prune_examples(keep):
    for ex in load_examples()[keep:]:
        os.remove(ex["path"])  # git keeps it


def mark(item_id, outcome, sent=None, example=False, note=None):
    if outcome not in LEARN_OUTCOMES:
        raise InboxError(f"bad outcome {outcome!r}; want one of {', '.join(LEARN_OUTCOMES)}")
    path = find_path(item_id)
    if not path:
        raise InboxError(f"no item with id {item_id!r}")
    item = read_item(path)
    if item.get("status") not in CLOSED_STATUSES:
        raise InboxError(f"{item_id} is {item.get('status')}; only closed items are learned from")
    if any(r["item"] == item_id for r in read_ledger()):
        return "exists", None, None
    score = None
    if outcome == "replied" and sent and item.get("proposed_reply"):
        score = survival(item["proposed_reply"], sent)
    ex_path = None
    if example:
        if not sent:
            raise InboxError("--example needs --sent-file")
        ex_path = write_example(item, sent, score, note)
    closed = archive_times().get(rel(path), "")
    _append_ledger({"item": item_id, "learned_at": iso_utc(), "outcome": outcome,
                    "score": "" if score is None else f"{score:.2f}", "closed_at": closed})
    return "marked", score, ex_path


# ----------------------------------------------------------------------------
# What drafting routines read
# ----------------------------------------------------------------------------

def context(n_examples):
    out = []
    for topic in KNOWLEDGE_TOPICS:
        path = topic_path(topic)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        body, in_body = [], False
        for line in lines:
            if line.startswith("## "):
                in_body = True
                continue
            if not in_body or not line.strip():
                continue
            m = _TAG.search(line)
            body.append(line[:m.start()] + f" (seen {m.group(2)}x, last {m.group(3)})" if m else line)
        if body:
            out += [f"## {TITLES[topic]}", ""] + body + [""]
    examples = load_examples()[:n_examples]
    if examples:
        out += ["## Examples: drafted versus what the user sent", ""]
        for ex in examples:
            text = ex["text"]
            text = text.split("\n---", 1)[1].lstrip("\n") if text.startswith("---") else text
            out += [text.replace("\n## ", "\n### ").strip(), "", "---", ""]
        out = out[:-2]  # no separator after the last example
    if not out:
        return "(nothing learned yet)\n"
    return "# What the user's past replies taught (knowledge/)\n\n" + "\n".join(out).rstrip("\n") + "\n"


def stats(weeks):
    rows = read_ledger()
    buckets = {}
    for r in rows:
        when = parse_ts(r.get("closed_at") or r.get("learned_at"))
        if not when:
            continue
        y, w, _ = when.isocalendar()
        b = buckets.setdefault(f"{y}-W{w:02d}", {"n": 0, "scores": [], **{o: 0 for o in LEARN_OUTCOMES}})
        b["n"] += 1
        b[r.get("outcome") or "unknown"] = b.get(r.get("outcome") or "unknown", 0) + 1
        if r.get("score"):
            b["scores"].append(float(r["score"]))
    out = []
    for week in sorted(buckets)[-weeks:]:
        b = buckets[week]
        s = b.pop("scores")
        out.append({"week": week, **b, "scored": len(s), "survival": round(sum(s) / len(s), 2) if s else None})
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _record(it):
    rec = {k: v for k, v in it.items() if not k.startswith("_")}
    rec["closed_at"] = iso_utc(it["closed_at"]) if it.get("closed_at") else None
    rec["path"] = rel(it["_path"])
    return rec


def cmd_pending(args):
    rows = pending(args.min_hours if args.min_hours is not None else _cfg("min_hours_closed"),
                   args.limit or _cfg("max_per_run"))
    if args.json:
        print(json.dumps([_record(it) for it in rows], indent=2, ensure_ascii=False, default=str))
        return
    if not rows:
        print("(nothing to learn from)")
    for it in rows:
        closed = iso_utc(it["closed_at"])[:10] if it.get("closed_at") else "?"
        draft = "draft" if it.get("proposed_reply") else "-"
        print(f"{it['status']:<9} {closed:<10} {it['kind']:<12} {draft:<5} {it['title']}  [{it['id']}]")


def cmd_context(args):
    sys.stdout.write(context(args.examples if args.examples is not None else _cfg("examples_in_context")))


def cmd_add(args):
    if args.json:
        payload = json.load(sys.stdin)
        records = payload if isinstance(payload, list) else [payload]
    else:
        if not (args.topic and args.key and args.text):
            raise InboxError("--topic, --key and --text are required (or use --json)")
        records = [{"topic": args.topic, "key": args.key, "text": args.text, "source": args.source}]
    rc, touched = 0, set()
    for rec in records:
        try:
            outcome, previous = add_fact(rec.get("topic"), rec.get("key"), rec.get("text"), rec.get("source"))
            touched.add(rec["topic"])
            print(f"{outcome}\t{rec['topic']}/{rec['key']}" + (f"\twas: {previous}" if previous else ""))
        except InboxError as e:
            rc = 1
            print(f"error\t{rec.get('topic')}/{rec.get('key')}\t{e}", file=sys.stderr)
    cap = _cfg("max_entries_per_topic")
    for topic in sorted(touched):
        n = learned_count(topic)
        if n > cap:
            print(f"warning: {topic} has {n} learned entries (cap {cap}); merge or drop the least useful",
                  file=sys.stderr)
    sys.exit(rc)


def cmd_drop(args):
    text = drop_fact(args.topic, args.key)
    print(f"dropped\t{args.topic}/{args.key}\twas: {text}")


def cmd_mark(args):
    sent = None
    if args.sent_file:
        sent = sys.stdin.read() if args.sent_file == "-" else open(args.sent_file, encoding="utf-8").read()
    outcome, score, ex_path = mark(args.id, args.outcome, sent, args.example, args.note)
    print(f"{outcome}\t{args.id}\t{args.outcome}" + (f"\tsurvival={score:.2f}" if score is not None else ""))
    if ex_path:
        print(f"example\t{rel(ex_path)}")


def cmd_stats(args):
    rows = stats(args.weeks)
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("(nothing learned yet)")
    for r in rows:
        surv = f"{r['survival']:.2f} over {r['scored']}" if r["survival"] is not None else "-"
        print(f"{r['week']}  {r['n']:>3} closed  replied {r['replied']:>2}  acted {r['acted']:>2}  "
              f"dismissed {r['dismissed']:>2}  unknown {r['unknown']:>2}  draft survival {surv}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pending"); p.add_argument("--min-hours", type=float); p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_pending)
    c = sub.add_parser("context"); c.add_argument("--examples", type=int); c.set_defaults(fn=cmd_context)
    a = sub.add_parser("add"); a.add_argument("--json", action="store_true"); a.add_argument("--topic", choices=KNOWLEDGE_TOPICS)
    a.add_argument("--key"); a.add_argument("--text"); a.add_argument("--source"); a.set_defaults(fn=cmd_add)
    d = sub.add_parser("drop"); d.add_argument("--topic", required=True, choices=KNOWLEDGE_TOPICS)
    d.add_argument("--key", required=True); d.set_defaults(fn=cmd_drop)
    m = sub.add_parser("mark"); m.add_argument("id"); m.add_argument("--outcome", required=True, choices=LEARN_OUTCOMES)
    m.add_argument("--sent-file"); m.add_argument("--example", action="store_true"); m.add_argument("--note")
    m.set_defaults(fn=cmd_mark)
    s = sub.add_parser("stats"); s.add_argument("--weeks", type=int, default=8); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_stats)
    args = ap.parse_args()
    try:
        args.fn(args)
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
