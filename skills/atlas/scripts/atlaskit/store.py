"""Storage: the single write point's file layer (spec E.1, E.3, E.7).

Canonical data lives in `.atlas/data/records/` as one JSON file per accepted
transaction. A transaction file is published with write-to-temp, fsync and an
atomic rename, then re-read and hash-checked before the core says "записано".
Loading stops at the first damaged file: later transactions are never applied
on top of a broken one.
"""

import errno
import hashlib
import json
import os
import secrets
import subprocess
import time

from . import SCHEMA_VERSION
from . import ru

ATLAS_DIR = ".atlas"
ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"

GITIGNORE_BODY = """# Атлас: локальное и личное не уходит в git (Приложение F.2 ТЗ)
.local/
generated/
index.html
"""


class AtlasError(Exception):
    """Error with a message that can be shown as is."""


class NotConnected(AtlasError):
    pass


def new_id(prefix):
    return "%s-%s" % (prefix, "".join(secrets.choice(ID_ALPHABET) for _ in range(8)))


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_project(start=None):
    """Walk up to the directory that holds `.atlas/`.

    An explicit place (argument, then ATLAS_PROJECT_DIR / CLAUDE_PROJECT_DIR)
    is a hard limit: if Atlas is not there, the answer is «not found», never
    another project from the current directory (R11). Only without any
    explicit place does the search start from the current directory."""
    env = os.environ.get("ATLAS_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR")
    base = os.path.abspath(start or env or os.getcwd())
    cur = base
    while True:
        if os.path.isdir(os.path.join(cur, ATLAS_DIR)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


class Store:
    def __init__(self, project_dir):
        self.project = os.path.abspath(project_dir)
        self.root = os.path.join(self.project, ATLAS_DIR)
        self.records_dir = os.path.join(self.root, "data", "records")
        self.local = os.path.join(self.root, ".local")
        self.private_dir = os.path.join(self.local, "private")
        self.manifests = os.path.join(self.local, "manifests")
        self.pending = os.path.join(self.local, "pending")
        self.inbox = os.path.join(self.local, "inbox")
        self.diagnostics = os.path.join(self.local, "diagnostics")
        self.locks = os.path.join(self.local, "locks")
        self.config_path = os.path.join(self.root, "atlas.config.json")

    # ----- layout -------------------------------------------------------
    def exists(self):
        return os.path.isfile(self.config_path)

    def ensure_layout(self):
        for d in (
            self.records_dir,
            os.path.join(self.root, "evidence"),
            os.path.join(self.root, "template"),
            os.path.join(self.root, "generated"),
            self.private_dir,
            self.manifests,
            os.path.join(self.manifests, "sessions"),
            self.pending,
            self.inbox,
            self.diagnostics,
            self.locks,
            os.path.join(self.local, "cache"),
        ):
            os.makedirs(d, exist_ok=True)
        gi = os.path.join(self.root, ".gitignore")
        if not os.path.exists(gi):
            write_text_atomic(gi, GITIGNORE_BODY)

    # ----- config -------------------------------------------------------
    def load_config(self):
        if not self.exists():
            raise NotConnected("Атлас не подключён к этому проекту. Скажите «подключи Атлас».")
        with open(self.config_path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_config(self, cfg):
        write_text_atomic(self.config_path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    def load_local(self, name, default=None):
        path = os.path.join(self.local, name)
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {} if default is None else default

    def save_local(self, name, data):
        path = os.path.join(self.local, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ----- transactions -------------------------------------------------
    def tx_files(self):
        if not os.path.isdir(self.records_dir):
            return []
        names = [n for n in os.listdir(self.records_dir) if n.endswith(".json") and not n.startswith(".")]
        return sorted(names)

    def load_transactions(self):
        """Return (transactions, problem). `problem` describes the first damaged
        or out-of-order file; everything from it on is not applied (E.3)."""
        txs = []
        problem = None
        expected = 1
        for name in self.tx_files():
            path = os.path.join(self.records_dir, name)
            stem = name[:-5]
            try:
                number = int(stem.split("-")[0])
            except ValueError:
                problem = {"file": name, "why": "имя файла не похоже на запись Атласа"}
                break
            try:
                with open(path, encoding="utf-8") as fh:
                    tx = json.load(fh)
            except (OSError, ValueError) as exc:
                problem = {"file": name, "why": "файл повреждён (%s)" % exc.__class__.__name__}
                break
            if tx.get("tx") != number:
                problem = {"file": name, "why": "номер внутри файла не совпадает с именем"}
                break
            if number != expected:
                if number == expected - 1:
                    why = "два файла с одним номером — вероятно, слияние веток git; нужен явный выбор порядка"
                else:
                    why = "пропущен номер %d — запись могла потеряться" % expected
                problem = {"file": name, "why": why}
                break
            body = canonical_json(tx.get("records", []))
            if tx.get("hash") != sha256_text(body):
                problem = {"file": name, "why": "содержимое не совпадает с контрольной суммой"}
                break
            if tx.get("schema_version") != SCHEMA_VERSION:
                problem = {"file": name, "why": "другая версия схемы (%s); нужна миграция" % tx.get("schema_version")}
                break
            txs.append(tx)
            expected += 1
        return txs, problem

    def publish_transaction(self, tx):
        """Atomically publish one transaction and confirm it by re-reading."""
        tx = dict(tx)
        tx["schema_version"] = SCHEMA_VERSION
        tx["hash"] = sha256_text(canonical_json(tx["records"]))
        name = "%06d.json" % tx["tx"]
        final = os.path.join(self.records_dir, name)
        if os.path.exists(final):
            raise AtlasError("Запись %s уже существует — другой писатель успел раньше. Повторите." % name)
        os.makedirs(self.pending, exist_ok=True)
        tmp = os.path.join(self.pending, ".%s.%d.tmp" % (name, os.getpid()))
        data = json.dumps(tx, ensure_ascii=False, indent=1) + "\n"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        fsync_dir(self.records_dir)
        with open(final, encoding="utf-8") as fh:
            back = json.load(fh)
        if back.get("hash") != tx["hash"]:
            raise AtlasError("Запись %s не подтвердилась при повторном чтении." % name)
        return tx

    def rewrite_transaction(self, tx):
        """Only for sensitive-content removal (F.3): the one exception to
        immutability. Keeps the number, recomputes the hash."""
        tx = dict(tx)
        tx["hash"] = sha256_text(canonical_json(tx["records"]))
        name = "%06d.json" % tx["tx"]
        final = os.path.join(self.records_dir, name)
        tmp = os.path.join(self.pending, ".%s.redact.tmp" % name)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(tx, ensure_ascii=False, indent=1) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        fsync_dir(self.records_dir)
        return tx

    # ----- lock ---------------------------------------------------------
    def lock(self, timeout=15.0):
        return WriterLock(os.path.join(self.locks, "writer.lock"), timeout)


class WriterLock:
    """Portable exclusive lock (O_CREAT|O_EXCL) with stale-lock recovery."""

    STALE_SECONDS = 120

    def __init__(self, path, timeout):
        self.path = path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + self.timeout
        self.token = "%d %f %s" % (os.getpid(), time.time(), secrets.token_hex(8))
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, self.token.encode())
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if self._stale():
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    raise AtlasError("Запись занята другим процессом Атласа; попробуйте ещё раз.")
                time.sleep(0.05)

    def _stale(self):
        """A lock is stale only when its owner process is gone. Age alone never
        takes the lock from a live (maybe slow) writer (R13)."""
        try:
            with open(self.path) as fh:
                parts = fh.read().split()
            pid = int(parts[0])
        except (OSError, ValueError, IndexError):
            # Unreadable owner (e.g. a crash between create and write): only
            # an old file is treated as abandoned.
            try:
                return time.time() - os.path.getmtime(self.path) > self.STALE_SECONDS
            except OSError:
                return True
        return not pid_alive(pid)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Remove the lock only if it is still ours: never delete a lock that
        # another writer took over.
        try:
            with open(self.path) as fh:
                mine = fh.read() == self.token
        except OSError:
            mine = False
        if mine:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def fsync_dir(path):
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text_atomic(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ----- git (read-only; never runs project scripts, F.5) -------------------

def _git(project, *args, binary=False):
    try:
        out = subprocess.run(
            ["git", "-C", project] + list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout if binary else out.stdout.decode("utf-8", "replace")


def git_available(project):
    return _git(project, "rev-parse", "--is-inside-work-tree") is not None


# Untracked build and cache artifacts that running a check produces: they are
# not part of the tested version (otherwise every test run would "change" it).
ARTIFACT_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".venv", "venv",
    "node_modules", ".next", ".nuxt", ".cache", ".parcel-cache", ".turbo", "coverage", "htmlcov",
    ".gradle", "target", "dist", "build", ".idea", ".vscode",
}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp", ".swp")
ARTIFACT_NAMES = {".DS_Store", "Thumbs.db", ".coverage"}


def _is_artifact(path):
    parts = path.split("/")
    return (any(p in ARTIFACT_PARTS for p in parts[:-1]) or parts[-1] in ARTIFACT_NAMES
            or parts[-1].endswith(ARTIFACT_SUFFIXES))


def _mode_of(path):
    try:
        st = os.lstat(path)
    except OSError:
        return "?"
    if os.path.islink(path):
        return "120000"
    return "100755" if st.st_mode & 0o111 else "100644"


def _blob_id(path):
    """git's blob id of a file's current content (content identity)."""
    try:
        if os.path.islink(path):
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        else:
            with open(path, "rb") as fh:
                data = fh.read()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def worktree_fingerprint(project):
    """Identify the tested state by content (B.5), ignoring `.atlas/`.

    The hash covers every tracked and untracked (not ignored) file with its
    git blob id, so two different dirty states of one branch differ, while a
    `git add` or a commit of the same content does not change it. Returns
    None without git."""
    if _git(project, "rev-parse", "--is-inside-work-tree") is None:
        return None
    head = (_git(project, "rev-parse", "HEAD") or "no-commit").strip()
    # path → "mode blob": the executable bit changes behaviour even when the
    # bytes are the same (R12).
    files = {}
    staged = _git(project, "ls-files", "-s", "-z") or ""
    for entry in staged.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3 and parts[2] == "0":
            files[path] = "%s %s" % (parts[0], parts[1])
        else:
            files[path] = None  # merge conflict: take the worktree content
    changed = (_git(project, "diff", "--name-only", "-z") or "").split("\0")
    untracked = [p for p in (_git(project, "ls-files", "--others", "--exclude-standard", "-z") or "").split("\0")
                 if p and not _is_artifact(p)]
    dirty = False
    for path in [p for p in changed + untracked if p] + [p for p, b in files.items() if b is None]:
        if path.startswith(ATLAS_DIR + "/"):
            continue
        full = os.path.join(project, path)
        if os.path.lexists(full):
            files[path] = "%s %s" % (_mode_of(full), _blob_id(full))
        else:
            files.pop(path, None)
        dirty = True
    h = hashlib.sha256()
    for path in sorted(files):
        if path.startswith(ATLAS_DIR + "/"):
            continue
        h.update(path.encode("utf-8", "surrogateescape") + b"\0" + (files[path] or "?").encode() + b"\n")
    return {"head": head[:12], "dirty": dirty, "hash": h.hexdigest()[:16]}


def git_recent(project, limit=20):
    """Raw material for the first-run analysis (Э9): recent commits with files."""
    log = _git(project, "log", "-n", str(limit), "--date=iso-strict", "--name-only",
               "--pretty=format:@@%h|%ad|%s")
    status = _git(project, "status", "--porcelain=v1")
    return log, status
