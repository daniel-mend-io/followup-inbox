#!/usr/bin/env python3
"""Render the routine prompt templates from config.yml.

Usage:
  ./render.py                 # writes rendered/<name>.md and rendered/manifest.json into the store
  ./render.py --stdout NAME   # print one rendered prompt
  ./render.py --schedule      # show each routine's cron and today's local times

Templates live in routines/*.md and use:
  {{user.name}}                    dotted keys from config.yml
  {{routine.cron}}                 the current entry under `routines:`
  {{routine.local_times}}          computed: the cron's firing times in config.timezone today
  {{scripts}}                      computed: "python3 scripts" or "python3 <module_path>/scripts"
  {{skill_path}}                   computed: SKILL.md or <module_path>/SKILL.md
  {{slack.exclude_channels|list}}  filters: list (comma-joined, "none" if empty), bullets, json
  {{#if routine.end_of_day}} ... {{/if}}     conditional block (truthy value)
  {{#unless x}} ... {{/unless}}
Unknown placeholders are an error: a prompt with a hole in it must not ship.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

from _inbox_common import MODULE_ROOT, REPO_ROOT, InboxError, cfg_get, die, load_config, local_zone, now_utc

ROUTINES_DIR = os.path.join(MODULE_ROOT, "routines")   # templates ship with the module
OUT_DIR = os.path.join(REPO_ROOT, "rendered")           # output lands in the instance

_BLOCK = re.compile(r"\{\{#(if|unless) ([\w.]+)\}\}(.*?)\{\{/\1\}\}", re.S)
_VAR = re.compile(r"\{\{([\w.]+)(?:\|(\w+))?\}\}")


def cron_local_times(cron, tz):
    """'45 6,10 * * *' -> '08:45 and 12:45' in tz for today. Non-trivial fields -> the raw cron."""
    try:
        minute, hour = cron.split()[:2]
        minutes = [int(m) for m in minute.split(",")]
        hours = [int(h) for h in hour.split(",")]
    except ValueError:
        return f"cron `{cron}` (UTC)"
    today = now_utc().date()
    times = []
    for h in hours:
        for m in minutes:
            utc = dt.datetime(today.year, today.month, today.day, h, m, tzinfo=dt.timezone.utc)
            times.append(utc.astimezone(tz).strftime("%H:%M"))
    times = sorted(set(times))
    if len(times) == 1:
        return times[0]
    return ", ".join(times[:-1]) + " and " + times[-1]


def build_context(config, routine):
    tz = local_zone(config)
    ctx = dict(config)
    r = dict(routine)
    r["local_times"] = cron_local_times(r.get("cron", ""), tz)
    r.setdefault("end_of_day", False)
    r.setdefault("stale_days", 7)
    ctx["routine"] = r
    ctx["today"] = now_utc().astimezone(tz).strftime("%Y-%m-%d")
    module_path = str(config.get("module_path") or "").strip("/ ")
    ctx["module_path"] = module_path
    ctx["scripts"] = f"python3 {module_path}/scripts" if module_path else "python3 scripts"
    ctx["skill_path"] = f"{module_path}/SKILL.md" if module_path else "SKILL.md"
    ctx["inbox_link"] = f"{str(cfg_get(config, 'git.repo_url', '')).rstrip('/')}/blob/{cfg_get(config, 'git.branch', 'main')}/INBOX.md"
    return ctx


def _fmt(value, flt):
    if flt == "list":
        if not value:
            return "none"
        return ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    if flt == "bullets":
        if not value:
            return "- (none)"
        return "\n".join(f"- {v}" for v in (value if isinstance(value, list) else [value]))
    if flt == "json":
        return json.dumps(value, ensure_ascii=False)
    if flt:
        raise InboxError(f"unknown filter |{flt}")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value).rstrip("\n")


def render_text(template, ctx, name="template"):
    missing = []

    def lookup(key):
        val = cfg_get(ctx, key, KeyError)
        if val is KeyError:
            missing.append(key)
            return None
        return val

    def blocks(m):
        kind, key, body = m.group(1), m.group(2), m.group(3)
        val = lookup(key)
        show = bool(val) if kind == "if" else not val
        return body if show else ""

    prev = None
    text = template
    while prev != text:  # nested blocks
        prev = text
        text = _BLOCK.sub(blocks, text)
    text = _VAR.sub(lambda m: _fmt(lookup(m.group(1)), m.group(2)), text)
    if missing:
        raise InboxError(f"{name}: config has no value for: {', '.join(sorted(set(missing)))}")
    return text


def load_templates():
    out = {}
    for name in os.listdir(ROUTINES_DIR):
        if name.endswith(".md") and not name.startswith("."):
            with open(os.path.join(ROUTINES_DIR, name), encoding="utf-8") as f:
                out[name[:-3]] = f.read()
    return out


def render_all(config, only=None):
    templates = load_templates()
    results = []
    for routine in config.get("routines") or []:
        if not isinstance(routine, dict) or "name" not in routine:
            raise InboxError("each entry under routines: needs at least name, template, cron")
        if only and routine["name"] != only:
            continue
        if routine.get("enabled", True) is False and not only:
            continue
        tmpl = templates.get(routine.get("template"))
        if tmpl is None:
            raise InboxError(f"routine {routine['name']}: no template routines/{routine.get('template')}.md")
        ctx = build_context(config, routine)
        prompt = render_text(tmpl, ctx, name=f"routines/{routine['template']}.md")
        results.append({
            "name": routine["name"],
            "cron_expression": routine["cron"],
            "model": routine.get("model") or cfg_get(config, "routines_default.model", "claude-opus-5"),
            "connectors": routine.get("connectors") or ["Slack"],
            "allowed_tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
                             + [f"mcp__{c}" for c in (routine.get("connectors") or ["Slack"])],
            "git_repository": cfg_get(config, "git.repo_url"),
            "prompt": prompt,
        })
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdout", metavar="NAME", help="print one routine's prompt")
    ap.add_argument("--schedule", action="store_true")
    args = ap.parse_args()
    try:
        config = load_config(required=True)
        tz = local_zone(config)
        if args.schedule:
            print(f"timezone: {getattr(tz, 'key', 'UTC')}   (crons are UTC and do not follow DST; see README)")
            for r in config.get("routines") or []:
                flag = "" if r.get("enabled", True) else "   (disabled)"
                print(f"  {r['name']:<28} {r['cron']:<16} today at {cron_local_times(r['cron'], tz)}{flag}")
            return
        if args.stdout:
            res = render_all(config, only=args.stdout)
            if not res:
                raise InboxError(f"no routine named {args.stdout!r} in config.yml")
            sys.stdout.write(res[0]["prompt"])
            return
        res = render_all(config)
        os.makedirs(OUT_DIR, exist_ok=True)
        for r in res:
            with open(os.path.join(OUT_DIR, r["name"] + ".md"), "w", encoding="utf-8") as f:
                f.write(r["prompt"])
        with open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, ensure_ascii=False)
        for r in res:
            print(f"rendered rendered/{r['name']}.md   cron {r['cron_expression']}  "
                  f"(today {cron_local_times(r['cron_expression'], tz)} {getattr(tz, 'key', 'UTC')})")
        print("wrote rendered/manifest.json")
    except InboxError as e:
        die(str(e))


if __name__ == "__main__":
    main()
