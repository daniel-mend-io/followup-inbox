"""Definition-of-done tests. Stdlib only: python3 -m unittest discover -s tests -v

Each test builds a throwaway store (and, for the git tests, a bare "remote"
plus two clones standing in for two routines) and drives the real scripts
through subprocess with INBOX_ROOT pointing at it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.normpath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(MODULE, "scripts")
sys.path.insert(0, SCRIPTS)
import _inbox_common as common  # noqa: E402


def run(root, script, *args, stdin=None, check=True):
    env = dict(os.environ, INBOX_ROOT=root, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args], input=stdin,
                          text=True, capture_output=True, cwd=root, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"{script} {args} rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
                           *args], cwd=cwd, text=True, capture_output=True, env=env, check=True)


def find(root, item_id):
    """Like common.find_path but for an arbitrary store root (the imported module is bound to MODULE)."""
    name = common.id_to_filename(item_id)
    cand = os.path.join(root, "items", name)
    if os.path.exists(cand):
        return cand
    arch = os.path.join(root, "archive")
    if os.path.isdir(arch):
        for month in sorted(os.listdir(arch), reverse=True):
            cand = os.path.join(arch, month, name)
            if os.path.exists(cand):
                return cand
    return None


def item(i, **over):
    rec = {"id": f"slack:C{i:03d}/1790000000.{i:06d}", "source": "slack", "kind": "commitment",
           "created_at": "2026-09-28T13:51:15Z", "title": f"item {i}", "url": "https://x.slack.com/archives/C/p1",
           "actor": "Alex", "trigger": None, "body": f"body {i}"}
    rec.update(over)
    return rec


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="inbox-")
        os.makedirs(os.path.join(self.root, "items"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_filename_is_deterministic_and_reversible(self):
        for i in ("slack:C031MTZ61J4/1789739475.264789", "calendar:abc_2026@google.com",
                  "email:<a.b@c.d>", "jira:12345", "slack:D01/1.2/extra"):
            f = common.id_to_filename(i)
            self.assertEqual(common.filename_to_id(f), i)
            self.assertNotIn("/", f)
            self.assertNotIn(":", f)
        self.assertEqual(common.id_to_filename("slack:C031MTZ61J4/1789739475.264789"),
                         "slack__C031MTZ61J4__1789739475.264789.md")

    def test_frontmatter_is_real_yaml(self):
        """Every frontmatter we write must parse with a real YAML parser (a future TUI will use one)."""
        try:
            import yaml  # optional; not a dependency of the module
        except ImportError:
            self.skipTest("PyYAML not installed")
        rec = item(1, title='colon: hash # "quotes" and: yes', trigger="after the 8.5 release", meta={"k": [1, 2]})
        text = common.dump_item(rec)
        head = text.split("\n---\n")[0][4:]
        parsed = yaml.safe_load(head)
        self.assertEqual(parsed["title"], rec["title"])
        self.assertEqual(parsed["id"], rec["id"])
        self.assertEqual(parsed["meta"], {"k": [1, 2]})
        self.assertIsNone(parsed["actor"] if "actor" not in rec else parsed.get("x"))

    def test_add_is_idempotent_and_never_touches_status_or_body(self):
        out = run(self.root, "add.py", "--json", stdin=json.dumps([item(1)])).stdout
        self.assertTrue(out.startswith("created"))
        run(self.root, "status.py", item(1)["id"], "waiting")
        # second scan: different title/body/status must not win; empty fields may be filled
        out = run(self.root, "add.py", "--json",
                  stdin=json.dumps([item(1, title="NEW", body="NEW", status="ready", trigger="after release")])).stdout
        self.assertTrue(out.startswith("enriched"), out)
        it = common.read_item(find(self.root, item(1)["id"]))
        self.assertEqual(it["title"], "item 1")
        self.assertEqual(it["body"], "body 1")
        self.assertEqual(it["status"], "waiting")
        self.assertEqual(it["trigger"], "after release")
        out = run(self.root, "add.py", "--json", stdin=json.dumps([item(1)])).stdout
        self.assertTrue(out.startswith("exists"), out)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "items"))), 1)

    def test_closed_items_are_archived_and_never_re_raised(self):
        run(self.root, "add.py", "--json", stdin=json.dumps([item(1), item(2)]))
        run(self.root, "status.py", item(1)["id"], "done", "--note", "replied in thread")
        run(self.root, "status.py", item(2)["id"], "dismissed")
        self.assertEqual(os.listdir(os.path.join(self.root, "items")), [])
        month = time.strftime("%Y-%m", time.gmtime())
        self.assertEqual(len(os.listdir(os.path.join(self.root, "archive", month))), 2)
        # re-scan
        out = run(self.root, "add.py", "--json", stdin=json.dumps([item(1), item(2)])).stdout
        self.assertEqual(out.count("exists"), 2, out)
        self.assertIn("archive/", out)
        self.assertEqual(run(self.root, "list.py", "--ids").stdout.strip(), "")
        ids = run(self.root, "list.py", "--all", "--ids").stdout.split()
        self.assertEqual(sorted(ids), sorted([item(1)["id"], item(2)["id"]]))
        # routines cannot reopen; humans can
        self.assertNotEqual(run(self.root, "status.py", item(1)["id"], "new", check=False).returncode, 0)
        self.assertNotEqual(run(self.root, "draft.py", item(1)["id"], "--text", "x", check=False).returncode, 0)
        run(self.root, "status.py", item(1)["id"], "new", "--reopen")
        self.assertEqual(run(self.root, "list.py", "--ids").stdout.split(), [item(1)["id"]])
        body = common.read_item(find(self.root, item(1)["id"]))["body"]
        self.assertIn("replied in thread", body)

    def test_list_filters_and_digest(self):
        run(self.root, "add.py", "--json", stdin=json.dumps([
            item(1, created_at="2026-09-01T00:00:00Z", status="waiting"),
            item(2, created_at="2026-09-27T00:00:00Z", status="ready", proposed_reply="hi"),
            item(3, source="calendar", id="calendar:evt1", kind="fyi", created_at="2026-09-28T00:00:00Z")]))
        ids = run(self.root, "list.py", "--ids").stdout.split()
        self.assertEqual(ids, ["calendar:evt1", item(2)["id"], item(1)["id"]])  # newest first
        self.assertEqual(run(self.root, "list.py", "--status", "ready", "--ids").stdout.split(), [item(2)["id"]])
        self.assertEqual(run(self.root, "list.py", "--source", "calendar", "--ids").stdout.split(), ["calendar:evt1"])
        self.assertEqual(run(self.root, "list.py", "--kind", "commitment", "--max-age", "10", "--ids").stdout.split(),
                         [item(2)["id"]])
        self.assertEqual(run(self.root, "list.py", "--min-age", "10", "--ids").stdout.split(), [item(1)["id"]])
        js = json.loads(run(self.root, "list.py", "--json").stdout)
        self.assertEqual(js[1]["proposed_reply"], "hi")
        run(self.root, "digest.py")
        with open(os.path.join(self.root, "INBOX.md")) as f:
            digest = f.read()
        self.assertIn("## ready (1)", digest)
        self.assertIn("### calendar", digest)
        self.assertIn("(items/slack__C002__1790000000.000002.md)", digest)
        self.assertIn("draft ready", digest)

    def test_store_root_is_discovered_from_cwd(self):
        """Without INBOX_ROOT, scripts run from anywhere inside the instance find its store."""
        sub = os.path.join(self.root, "deep", "er")
        os.makedirs(sub)
        env = dict(os.environ)
        env.pop("INBOX_ROOT", None)
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "add.py"), "--json"], input=json.dumps(item(1)),
                              text=True, capture_output=True, cwd=sub, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(find(self.root, item(1)["id"])))
        self.assertFalse(os.path.exists(os.path.join(MODULE, "items", common.id_to_filename(item(1)["id"]))))

    def test_render_uses_module_path_and_writes_into_store(self):
        with open(os.path.join(MODULE, "config.example.yml")) as f:
            cfg = f.read()
        with open(os.path.join(self.root, "config.yml"), "w") as f:
            f.write(cfg)
        run(self.root, "render.py")
        with open(os.path.join(self.root, "rendered", "commitments-evening.md")) as f:
            prompt = f.read()
        self.assertIn("python3 tool/scripts/add.py", prompt)
        self.assertIn("git submodule update --init", prompt)
        self.assertIn("Read `tool/SKILL.md`", prompt)
        self.assertIn("--min-age 7", prompt)           # end_of_day block rendered
        self.assertNotIn("{{", prompt)
        with open(os.path.join(self.root, "rendered", "commitments-morning-midday.md")) as f:
            morning = f.read()
        self.assertNotIn("{{", morning)
        self.assertNotIn("stale past", morning)                 # end_of_day block dropped…
        self.assertIn("This DM to the user is the only message", morning)   # …without eating the rest
        self.assertIn("If `sync.py` failed", morning)
        with open(os.path.join(self.root, "rendered", "manifest.json")) as f:
            names = [r["name"] for r in json.load(f)]
        self.assertEqual(names, ["commitments-morning-midday", "commitments-evening", "loose-threads"])
        self.assertFalse(os.path.exists(os.path.join(MODULE, "routines", "rendered")))

    def test_verify_passes(self):
        out = run(self.root, "verify.py").stdout
        self.assertTrue(out.strip().endswith("ok"), out)
        self.assertEqual(os.listdir(os.path.join(self.root, "items")), [])

    def test_ask_kind_is_accepted(self):
        out = run(self.root, "add.py", "--json", stdin=json.dumps([item(1, kind="ask", actor="Alex")])).stdout
        self.assertTrue(out.startswith("created"), out)
        self.assertEqual(run(self.root, "list.py", "--kind", "ask", "--ids").stdout.split(), [item(1)["id"]])

    def test_bad_input_is_rejected(self):
        for bad in (item(1, id="jira:1"), item(1, kind="nope"), item(1, status="drafted"),
                    item(1, created_at="yesterday"), {"id": "slack:C1/1"}):
            self.assertNotEqual(run(self.root, "add.py", "--json", stdin=json.dumps([bad]), check=False).returncode, 0)
        self.assertEqual(os.listdir(os.path.join(self.root, "items")), [])


class GitTests(unittest.TestCase):
    """Two clones of one bare repo stand in for two routines on overlapping crons."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="inbox-git-")
        self.bare = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        seed = os.path.join(self.tmp, "seed")
        git(self.tmp, "clone", "-q", self.bare, seed)
        os.makedirs(os.path.join(seed, "items"))
        open(os.path.join(seed, "items", ".gitkeep"), "w").close()
        open(os.path.join(seed, "config.yml"), "w").write("timezone: UTC\ngit:\n  author_name: routine\n  author_email: r@x\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.clones = []
        for name in ("a", "b"):
            path = os.path.join(self.tmp, name)
            git(self.tmp, "clone", "-q", "-b", "main", self.bare, path)
            self.clones.append(path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fresh(self):
        path = os.path.join(self.tmp, "check-%d" % time.time_ns())
        git(self.tmp, "clone", "-q", "-b", "main", self.bare, path)
        return path

    def test_concurrent_commits_lose_nothing(self):
        a, b = self.clones
        run(a, "add.py", "--json", stdin=json.dumps([item(1), item(2)]))
        run(b, "add.py", "--json", stdin=json.dumps([item(3)]))
        results = {}

        def push(root, msg):
            results[root] = run(root, "sync.py", "-m", msg, check=False)

        t1 = threading.Thread(target=push, args=(a, "a: +2"))
        t2 = threading.Thread(target=push, args=(b, "b: +1"))
        t1.start(); t2.start(); t1.join(); t2.join()
        for r in results.values():
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check = self.fresh()
        ids = run(check, "list.py", "--ids").stdout.split()
        self.assertEqual(sorted(ids), sorted([item(1)["id"], item(2)["id"], item(3)["id"]]))
        with open(os.path.join(check, "INBOX.md")) as f:
            digest = f.read()
        for i in (1, 2, 3):
            self.assertIn(f"item {i}", digest)
        log = git(check, "log", "--oneline").stdout
        self.assertIn("a: +2", log)
        self.assertIn("b: +1", log)

    def test_same_id_from_two_routines_first_writer_wins(self):
        a, b = self.clones
        run(a, "add.py", "--json", stdin=json.dumps([item(1, title="from a", kind="commitment")]))
        run(b, "add.py", "--json", stdin=json.dumps([item(1, title="from b", kind="loose_thread")]))
        run(a, "sync.py", "-m", "a")
        r = run(b, "sync.py", "-m", "b", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check = self.fresh()
        it = common.read_item(os.path.join(check, "items", common.id_to_filename(item(1)["id"])))
        self.assertEqual(it["title"], "from a")
        self.assertEqual(len([n for n in os.listdir(os.path.join(check, "items")) if n.endswith(".md")]), 1)

    def test_human_close_beats_routine_enrichment(self):
        a, b = self.clones
        run(a, "add.py", "--json", stdin=json.dumps([item(1)]))
        run(a, "sync.py", "-m", "routine: add")
        for c in (a, b):
            git(c, "pull", "-q", "--rebase", "origin", "main")
        # human closes it in clone b and pushes
        run(b, "status.py", item(1)["id"], "done")
        run(b, "sync.py", "-m", "human: done")
        # routine in clone a, on a stale checkout, enriches the same item and drafts on it
        run(a, "add.py", "--json", stdin=json.dumps([item(1, trigger="after release")]))
        run(a, "draft.py", item(1)["id"], "--text", "stale draft")
        r = run(a, "sync.py", "-m", "routine: enrich", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        check = self.fresh()
        self.assertEqual(run(check, "list.py", "--ids").stdout.strip(), "")
        month = time.strftime("%Y-%m", time.gmtime())
        archived = os.path.join(check, "archive", month, common.id_to_filename(item(1)["id"]))
        self.assertTrue(os.path.exists(archived))
        self.assertEqual(common.read_item(archived)["status"], "done")
        self.assertFalse(os.path.exists(os.path.join(check, "items", common.id_to_filename(item(1)["id"]))))
        # and a re-scan on the fresh clone does not bring it back
        out = run(check, "add.py", "--json", stdin=json.dumps([item(1)])).stdout
        self.assertTrue(out.startswith("exists") and "archive/" in out, out)

    def test_verify_commit_round_trip(self):
        a = self.clones[0]
        out = run(a, "verify.py", "--commit").stdout
        self.assertTrue(out.strip().endswith("ok"), out)
        check = self.fresh()
        self.assertEqual([n for n in os.listdir(os.path.join(check, "items")) if n.endswith(".md")], [])
        log = git(check, "log", "--oneline").stdout
        self.assertIn("verify: write", log)
        self.assertIn("verify: cleanup", log)

    def test_sync_never_forces(self):
        with open(os.path.join(SCRIPTS, "sync.py")) as f:
            src = f.read()
        self.assertNotIn("--force", src)
        self.assertNotIn("-f\"", src)


if __name__ == "__main__":
    unittest.main()
