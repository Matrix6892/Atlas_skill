import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, os.pardir, "skills", "atlas", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

from atlaskit import cli, views  # noqa: E402
from atlaskit.install import connect  # noqa: E402
from atlaskit.ops import Context, apply_receipt, load_state  # noqa: E402
from atlaskit.store import Store  # noqa: E402

OWNER = {"quote": "да, так и делаем"}


class ProjectCase(unittest.TestCase):
    """A fresh git project with Atlas connected (project-mode hooks)."""

    git = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="atlas-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        if self.git:
            self.sh("git", "init", "-q")
            self.sh("git", "config", "user.email", "t@example.com")
            self.sh("git", "config", "user.name", "t")
            self.touch("README.md", "# demo\n")
            self.sh("git", "add", "-A")
            self.sh("git", "commit", "-qm", "init")
        self.env = {k: os.environ.get(k) for k in ("ATLAS_SESSION_ID", "ATLAS_AGENT", "CLAUDE_PROJECT_DIR",
                                                    "ATLAS_PROJECT_DIR", "CLAUDE_ENV_FILE")}
        for k in self.env:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_env)
        connect(self.project, name="Заметки для друзей", hooks="project")
        self.store = Store(self.project)

    def _restore_env(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def sh(self, *args):
        return subprocess.run(list(args), cwd=self.project, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout.decode()

    def touch(self, rel, text):
        path = os.path.join(self.project, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)

    def write(self, ops, session="s1", agent="claude-code", **extra):
        receipt = dict(extra, ops=ops)
        return apply_receipt(self.store, receipt, Context(self.project, agent, session))

    def ok(self, ops, **kw):
        res = self.write(ops, **kw)
        self.assertEqual(res["failed"], [], res["failed"])
        return res

    def fails(self, ops, contains=None, **kw):
        res = self.write(ops, **kw)
        self.assertTrue(res["failed"], "expected a failure")
        if contains:
            self.assertIn(contains, " | ".join(f["why"] for f in res["failed"]))
        return res

    def state(self):
        st, problem = load_state(self.store)
        self.assertIsNone(problem)
        return st

    def cli(self, *argv, stdin=None):
        buf = io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(buf):
                code = cli.main(["--project", self.project] + list(argv))
        finally:
            sys.stdin = old_stdin
        return code, buf.getvalue()

    def hook(self, event, payload):
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        buf = io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            with redirect_stdout(buf):
                cli.main(["hook", event, "--via", "project"])
        finally:
            sys.stdin = old_stdin
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        text = buf.getvalue().strip()
        return json.loads(text) if text else None

    # a small ready-made topic: «Вход через Google» with three criteria
    def login_topic(self):
        res = self.ok([
            {"op": "goal.set", "text": "Друг сам создаёт первую заметку", "source": OWNER},
            {"op": "thread.create", "ref": "t", "title": "Вход через Google", "leads_to_goal": True},
            {"op": "criterion.add", "ref": "c1", "thread": "@t", "text": "Человек входит через Google и видит имя"},
            {"op": "criterion.add", "ref": "c2", "thread": "@t", "text": "Человек входит с телефона"},
            {"op": "criterion.add", "ref": "c3", "thread": "@t", "text": "После входа человек попадает куда решим"},
        ])
        return res["refs"]

    def present(self, refs, criteria=("c1", "c3")):
        res = self.ok([{"op": "result.present", "ref": "r", "thread": refs["t"],
                        "criteria": [refs[c] for c in criteria], "summary": "входит и видит имя",
                        "check": {"steps": ["Нажмите «Войти»"], "expect": "видно имя", "duration": "около пяти минут"}}])
        return res["refs"]["r"]
