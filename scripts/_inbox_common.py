"""Shared helpers for the follow-up inbox scripts.

Stdlib only. One item = one Markdown file with YAML frontmatter under items/
(open) or archive/YYYY-MM/ (closed). Git history is the audit trail.

Every verb script imports from here and stays small.
"""

import datetime as _dt
import json
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODULE_ROOT = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))


def _discover_root():
    """Where the store is.

    1. $INBOX_ROOT, if set.
    2. The nearest directory at or above the current one that holds a
       config.yml or an items/ directory. This is the instance repository when
       the module is vendored into it as a submodule (e.g. tool/).
    3. The module's own directory (a one-repo layout, or a local check).
    """
    env = os.environ.get("INBOX_ROOT")
    if env:
        return os.path.abspath(env)
    cur = os.getcwd()
    while True:
        if os.path.exists(os.path.join(cur, "config.yml")) or os.path.isdir(os.path.join(cur, "items")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return MODULE_ROOT


REPO_ROOT = _discover_root()
ITEMS_DIR = os.path.join(REPO_ROOT, "items")
ARCHIVE_DIR = os.path.join(REPO_ROOT, "archive")
DIGEST_PATH = os.path.join(REPO_ROOT, "INBOX.md")
CONFIG_PATH = os.path.join(REPO_ROOT, "config.yml")
ENV_PATH = os.path.join(REPO_ROOT, ".env")

SOURCES = ("slack", "jira", "email", "calendar")
# commitment   the user promised something          (from: the user)
# ask          someone asked the user to do something (to: the user)
# needs_reply  a direct question to the user is unanswered
# loose_thread a conversation stopped without landing
# fyi          informational; nothing owed
KINDS = ("commitment", "ask", "needs_reply", "loose_thread", "fyi")
STATUSES = ("new", "waiting", "ready", "done", "dismissed")
OPEN_STATUSES = ("new", "waiting", "ready")
CLOSED_STATUSES = ("done", "dismissed")

# Frontmatter keys, in the order they are written. Mirrors the inbox_items
# columns; `body` and `proposed_reply` live in the Markdown body instead.
FIELDS = ("id", "source", "kind", "created_at", "seen_at", "title", "url",
          "actor", "trigger", "status", "meta")
REQUIRED = ("id", "source", "kind", "created_at", "title")

PROPOSED_HEADING = "## Proposed reply"


class InboxError(Exception):
    """Raised for user-facing errors. Scripts print the message and exit 1."""


# ----------------------------------------------------------------------------
# .env (credentials never in git) — same shape as the Jira scripts' loader.
# v1 needs no secrets: sources are reached through the agent's connectors.
# ----------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    """Read a .env file into a dict (simple KEY=VALUE parser). Missing file → {}."""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ----------------------------------------------------------------------------
# Time
# ----------------------------------------------------------------------------

def now_utc():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso_utc(dt=None):
    dt = dt or now_utc()
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value):
    """Parse an ISO-8601 timestamp (Z or offset). Naive values are taken as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        raise InboxError(f"bad timestamp: {value!r} (want ISO-8601, e.g. 2026-09-28T13:51:15Z)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def local_zone(config=None):
    """zoneinfo for config.timezone, else UTC."""
    tz = (config or {}).get("timezone") if config is not None else None
    if not tz:
        return _dt.timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        return _dt.timezone.utc


# ----------------------------------------------------------------------------
# id <-> filename. Deterministic and reversible, so a re-scan of the same
# source event always lands on the same path (this is what makes `add`
# idempotent, replacing the DB's `resolution=ignore-duplicates`).
#
#   slack:C031MTZ61J4/1789739475.264789  ->  slack__C031MTZ61J4__1789739475.264789.md
#
# ':' and '/' become '__'. '_' and anything outside [A-Za-z0-9.-] become
# %XX so the double underscore stays unambiguous. Decoding: the first '__'
# is ':' (source separator), later ones are '/'.
# ----------------------------------------------------------------------------

_SAFE = re.compile(r"[A-Za-z0-9.\-]")
_ID_RE = re.compile(r"^[a-z]+:[^\s]+$")


def validate_id(item_id):
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        raise InboxError(f"bad id: {item_id!r} (want <source>:<source-specific-key>, no whitespace)")
    source = item_id.split(":", 1)[0]
    if source not in SOURCES and source != "effort":
        raise InboxError(f"bad id: {item_id!r} (prefix must be one of {', '.join(SOURCES)})")
    return item_id


def id_to_filename(item_id):
    validate_id(item_id)
    out = []
    for ch in item_id:
        if ch in ":/":
            out.append("__")
        elif _SAFE.match(ch):
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out) + ".md"


def filename_to_id(name):
    stem = name[:-3] if name.endswith(".md") else name
    parts = stem.split("__")
    if len(parts) < 2:
        raise InboxError(f"not an item filename: {name!r}")
    decoded = parts[0] + ":" + "/".join(parts[1:])
    # undo %XX
    def _unhex(m):
        return bytes.fromhex(m.group(0).replace("%", "")).decode("utf-8")
    return re.sub(r"(?:%[0-9A-Fa-f]{2})+", _unhex, decoded)


# ----------------------------------------------------------------------------
# Frontmatter (a deliberately small YAML subset: one scalar per line).
# Written values are bare when safe, JSON-quoted otherwise (JSON strings are
# valid YAML double-quoted scalars), so any YAML reader can parse our files
# and we can parse anything a human is likely to type into `status:`.
# ----------------------------------------------------------------------------

_BARE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@/+\-]*$")
_TS_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:\d{2})?)?$")
_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~", ""}


def _dump_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    s = str(value)
    if _TS_LIKE.match(s):
        return s
    if _BARE_OK.match(s) and s.lower() not in _RESERVED and not s.replace(".", "", 1).isdigit() \
            and s == s.strip():
        return s
    return json.dumps(s, ensure_ascii=False)


def _load_scalar(raw):
    s = raw.strip()
    if s == "" or s in ("null", "~"):
        return None
    if s.startswith('"'):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s.strip('"')
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            if s.startswith("[") and s.endswith("]"):
                inner = s[1:-1].strip()
                return [_load_scalar(x) for x in inner.split(",")] if inner else []
            return s
    if s == "true":
        return True
    if s == "false":
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def dump_item(item, fields=FIELDS, section_heading=PROPOSED_HEADING, section_key="proposed_reply"):
    """Serialise a record dict to file text: frontmatter (fields, in order), body, one named section."""
    lines = ["---"]
    for key in fields:
        if key == "meta" and not item.get("meta"):
            continue
        lines.append(f"{key}: {_dump_scalar(item.get(key))}")
    lines.append("---")
    body = (item.get("body") or "").strip("\n")
    section = (item.get(section_key) or "").strip("\n")
    text = "\n".join(lines) + "\n"
    if body:
        text += "\n" + body + "\n"
    if section:
        text += "\n" + section_heading + "\n\n" + section + "\n"
    return text


def parse_item(text, path=None, section_heading=PROPOSED_HEADING, section_key="proposed_reply"):
    """Parse file text into a dict (frontmatter keys + body + the named section)."""
    if not text.startswith("---"):
        raise InboxError(f"{path or 'item'}: missing frontmatter")
    parts = text.split("\n---", 1)
    head = parts[0][3:]
    rest = parts[1] if len(parts) > 1 else ""
    if rest.startswith("\n"):
        rest = rest[1:]
    item = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise InboxError(f"{path or 'item'}: bad frontmatter line {line!r}")
        item[key.strip()] = _load_scalar(value)
    body, section = _split_body(rest, section_heading)
    item["body"] = body
    item[section_key] = section
    if path:
        item["_path"] = path
    return item


def _split_body(rest, heading=PROPOSED_HEADING):
    marker = "\n" + heading
    text = "\n" + rest
    idx = text.find(marker)
    if idx == -1:
        return rest.strip("\n"), ""
    body = text[:idx].strip("\n")
    reply = text[idx + len(marker):].strip("\n")
    return body, reply


def validate_item(item):
    for key in REQUIRED:
        if not item.get(key):
            raise InboxError(f"missing required field: {key}")
    validate_id(item["id"])
    if item["source"] not in SOURCES:
        raise InboxError(f"bad source {item['source']!r}; want one of {', '.join(SOURCES)}")
    if item["id"].split(":", 1)[0] != item["source"]:
        raise InboxError(f"id prefix {item['id'].split(':',1)[0]!r} does not match source {item['source']!r}")
    if item["kind"] not in KINDS:
        raise InboxError(f"bad kind {item['kind']!r}; want one of {', '.join(KINDS)}")
    if item.get("status", "new") not in STATUSES:
        raise InboxError(f"bad status {item['status']!r}; want one of {', '.join(STATUSES)}")
    parse_ts(item["created_at"])
    if item.get("seen_at"):
        parse_ts(item["seen_at"])
    if item.get("meta") is not None and not isinstance(item["meta"], dict):
        raise InboxError("meta must be a JSON object")


# ----------------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------------

def read_item(path):
    with open(path, encoding="utf-8") as f:
        return parse_item(f.read(), path)


def write_item(path, item, **dump_kwargs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(dump_item(item, **dump_kwargs))
    os.replace(tmp, path)


def iter_paths(include_archive=False):
    """Yield item file paths: items/ first, then archive/ (newest month first)."""
    if os.path.isdir(ITEMS_DIR):
        for name in sorted(os.listdir(ITEMS_DIR)):
            if name.endswith(".md") and not name.startswith("."):
                yield os.path.join(ITEMS_DIR, name)
    if include_archive and os.path.isdir(ARCHIVE_DIR):
        for month in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
            mdir = os.path.join(ARCHIVE_DIR, month)
            if not os.path.isdir(mdir):
                continue
            for name in sorted(os.listdir(mdir)):
                if name.endswith(".md") and not name.startswith("."):
                    yield os.path.join(mdir, name)


def load_all(include_archive=False):
    items = []
    for path in iter_paths(include_archive):
        try:
            items.append(read_item(path))
        except InboxError as e:
            print(f"warning: skipping {rel(path)}: {e}", file=sys.stderr)
    return items


def find_path(item_id):
    """Locate an item by id, in items/ then archive/. None if absent."""
    name = id_to_filename(item_id)
    candidate = os.path.join(ITEMS_DIR, name)
    if os.path.exists(candidate):
        return candidate
    if os.path.isdir(ARCHIVE_DIR):
        for month in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
            candidate = os.path.join(ARCHIVE_DIR, month, name)
            if os.path.exists(candidate):
                return candidate
    return None


def archive_dir_for(dt=None):
    dt = dt or now_utc()
    return os.path.join(ARCHIVE_DIR, dt.strftime("%Y-%m"))


def rel(path):
    return os.path.relpath(path, REPO_ROOT)


def sort_newest_first(items):
    return sorted(items, key=lambda it: parse_ts(it["created_at"]) or now_utc(), reverse=True)


# ----------------------------------------------------------------------------
# Efforts: the things the user has to keep in their head — epics, customer
# situations, marketing pushes, cross-team coordination. One file each under
# efforts/. Added by hand, or suggested by a routine (status `suggested`) from
# the issue tracker for the user to accept. Same file shape as items.
# ----------------------------------------------------------------------------

EFFORTS_DIR = os.path.join(REPO_ROOT, "efforts")
EFFORT_FIELDS = ("id", "kind", "title", "source", "url", "status", "next_action",
                 "next_action_due", "last_touched", "created_at", "meta")
EFFORT_KINDS = ("epic", "customer", "marketing", "coordination", "research", "other")
EFFORT_STATUSES = ("suggested", "active", "paused", "done", "dropped")
EFFORT_OPEN = ("suggested", "active", "paused")
SUGGESTION_HEADING = "## Suggested next action"
_EFFORT_ID_RE = re.compile(r"^(effort|jira):[^\s]+$")


def validate_effort_id(effort_id):
    if not isinstance(effort_id, str) or not _EFFORT_ID_RE.match(effort_id):
        raise InboxError(f"bad effort id: {effort_id!r} (want effort:<slug> or jira:<KEY>)")
    return effort_id


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:60] or "effort"


def effort_path(effort_id):
    validate_effort_id(effort_id)
    return os.path.join(EFFORTS_DIR, id_to_filename(effort_id.replace("effort:", "effort:", 1)))


def read_effort(path):
    with open(path, encoding="utf-8") as f:
        return parse_item(f.read(), path, section_heading=SUGGESTION_HEADING, section_key="suggestion")


def write_effort(path, effort):
    write_item(path, effort, fields=EFFORT_FIELDS, section_heading=SUGGESTION_HEADING, section_key="suggestion")


def load_efforts(include_closed=False):
    out = []
    if not os.path.isdir(EFFORTS_DIR):
        return out
    for name in sorted(os.listdir(EFFORTS_DIR)):
        if not name.endswith(".md") or name.startswith("."):
            continue
        try:
            e = read_effort(os.path.join(EFFORTS_DIR, name))
        except InboxError as ex:
            print(f"warning: skipping efforts/{name}: {ex}", file=sys.stderr)
            continue
        if include_closed or e.get("status") in EFFORT_OPEN:
            out.append(e)
    return out


def validate_effort(e):
    for key in ("id", "kind", "title"):
        if not e.get(key):
            raise InboxError(f"effort: missing required field: {key}")
    validate_effort_id(e["id"])
    if e["kind"] not in EFFORT_KINDS:
        raise InboxError(f"bad effort kind {e['kind']!r}; want one of {', '.join(EFFORT_KINDS)}")
    if e.get("status", "active") not in EFFORT_STATUSES:
        raise InboxError(f"bad effort status {e['status']!r}; want one of {', '.join(EFFORT_STATUSES)}")


# ----------------------------------------------------------------------------
# Config (config.yml). A small YAML subset parser so the module stays stdlib
# only: nested maps, lists of scalars, lists of maps, quoted scalars, `|`
# block strings, comments. No anchors, no flow collections, no multi-doc.
# ----------------------------------------------------------------------------

def load_config(path=CONFIG_PATH, required=False):
    if not os.path.exists(path):
        if required:
            raise InboxError(f"no config at {rel(path)} — copy config.example.yml to config.yml and fill it in")
        return {}
    with open(path, encoding="utf-8") as f:
        return parse_yaml_subset(f.read(), path)


def parse_yaml_subset(text, path="config.yml"):
    lines = []
    for raw in text.splitlines():
        if raw.strip().startswith("#") or not raw.strip():
            lines.append(None)  # keep line numbers aligned
        else:
            lines.append(raw.rstrip("\n"))
    pos = [0]

    def peek():
        while pos[0] < len(lines) and lines[pos[0]] is None:
            pos[0] += 1
        return lines[pos[0]] if pos[0] < len(lines) else None

    def indent_of(line):
        return len(line) - len(line.lstrip(" "))

    def strip_comment(v):
        # strip a trailing " # comment"; for quoted scalars, keep up to the closing quote
        v = v.strip()
        if v.startswith(('"', "'")):
            q = v[0]
            i = 1
            while i < len(v):
                if v[i] == "\\" and q == '"':
                    i += 2
                    continue
                if v[i] == q:
                    return v[: i + 1]
                i += 1
            return v
        idx = v.find(" #")
        return v[:idx] if idx != -1 else v

    def parse_block(indent):
        line = peek()
        if line is None:
            return None
        if line.lstrip().startswith("- "):
            return parse_list(indent_of(line))
        return parse_map(indent_of(line))

    def parse_block_string(indent):
        out = []
        while True:
            if pos[0] >= len(lines):
                break
            raw = lines[pos[0]]
            if raw is None:
                out.append("")
                pos[0] += 1
                continue
            if indent_of(raw) < indent:
                break
            out.append(raw[indent:])
            pos[0] += 1
        return "\n".join(out).strip("\n") + "\n"

    def parse_value_after_key(value, indent, lineno):
        value = value.strip()
        if value.startswith("#"):
            value = ""
        if value == "":
            nxt = peek()
            if nxt is not None and indent_of(nxt) > indent:
                return parse_block(indent_of(nxt))
            return None
        if value == "|":
            nxt = peek()
            if nxt is None or indent_of(nxt) <= indent:
                return ""
            return parse_block_string(indent_of(nxt))
        return _load_scalar(strip_comment(value).strip())

    def parse_map(indent):
        result = {}
        while True:
            line = peek()
            if line is None or indent_of(line) < indent:
                return result
            if indent_of(line) > indent:
                raise InboxError(f"{path}:{pos[0]+1}: unexpected indent")
            if line.lstrip().startswith("- "):
                return result
            key, sep, value = line.strip().partition(":")
            if not sep:
                raise InboxError(f"{path}:{pos[0]+1}: expected 'key: value'")
            pos[0] += 1
            result[key.strip()] = parse_value_after_key(value, indent, pos[0])

    def parse_list(indent):
        result = []
        while True:
            line = peek()
            if line is None or indent_of(line) < indent or not line.lstrip().startswith("- "):
                return result
            if indent_of(line) > indent:
                raise InboxError(f"{path}:{pos[0]+1}: unexpected indent")
            content = line.strip()[2:]
            if ":" in content and not content.startswith(('"', "'")) and not content.startswith(("http:", "https:")):
                # list item that is a map: rewrite the line as the map's first entry
                lines[pos[0]] = " " * (indent + 2) + content
                result.append(parse_map(indent + 2))
            else:
                pos[0] += 1
                result.append(_load_scalar(strip_comment(content).strip()))

    return parse_map(indent_of(peek())) if peek() is not None else {}


def cfg_get(config, dotted, default=None):
    cur = config
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


# ----------------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------------

def git(*args, check=True, capture=True, cwd=None, env=None):
    cmd = ["git", *args]
    full_env = dict(os.environ)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd or REPO_ROOT, text=True, env=full_env,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)
    if check and proc.returncode != 0:
        raise InboxError(f"git {' '.join(args)} failed ({proc.returncode}):\n{(proc.stderr or '').strip()}")
    return proc


def is_git_repo():
    return git("rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)
