"""Connect / disconnect a project (Э9, §6.1 «Отключить Атлас», A02, A78).

Atlas changes only what it lists: its `.atlas/` folder, one marked block in
CLAUDE.md (and AGENTS.md on request), and — when not installed as a plugin —
two hooks in `.claude/settings.local.json`. Disconnect removes exactly those
and keeps the data.
"""

import json
import os
import subprocess
import sys

from . import SCHEMA_VERSION, VERSION
from . import ru
from .store import Store, AtlasError, write_text_atomic

BLOCK_BEGIN = "<!-- atlas:begin — блок добавлен Атласом; убрать: скажите «отключи Атлас» -->"
BLOCK_END = "<!-- atlas:end -->"
HOOK_MARK = "--via project"

CLAUDE_BLOCK = """## Атлас — память и карта проекта
В этом проекте работает Атлас (скилл `atlas`): агент по ходу работы оставляет короткие записи, владелец видит доклад и итоги.
- Перед записями и ответами владельцу о проекте загрузи скилл `atlas`: в нём формы экранов и правила записи.
- Записи — в `.atlas/`. Файлы записей руками не править: только через команду записи Атласа (квитанция — JSON-файл в `.atlas/.local/inbox/`).
- После значимого шага (сделано, решено, проверено, отложено, не получилось) — короткая запись через скилл `atlas`.
- «Мы же решили…» — это вопрос к памяти: найти решение в записях; новое решение — только из явных слов владельца.
- «Проверил» и «принимаю» — разные записи; принимает только владелец. Деньги, публикация, настоящие данные — только по его явному «да».
- Фразы владельца «где мы?», «что от меня нужно?», «покажи, что готово», «в сторону: …», «ты неверно понял», «на сегодня всё» — это Атлас."""


def script_path():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "atlas.py"))


def command_line():
    exe = os.path.basename(sys.executable) if os.name == "nt" else "python3"
    path = script_path()
    return "%s %s" % (exe, ('"%s"' % path) if " " in path else path)


def detect_hooks_mode():
    path = script_path().replace("\\", "/")
    home = os.path.expanduser("~").replace("\\", "/")
    if path.startswith(home + "/.claude/plugins/"):
        return "plugin"
    return "project"


def agents_block():
    return """## Атлас — память проекта (Codex, opencode и другие агенты без хуков Атласа)
В этом проекте работает Атлас: память и карта проекта. Если у тебя есть скилл `atlas` — следуй ему. Порядок, одна сессия за раз:
1. В начале работы выполни `%s start` и покажи владельцу доклад из вывода как есть.
2. После значимого шага (сделано, решено, проверено, отложено, не получилось) — запись: положи квитанцию JSON в `.atlas/.local/inbox/<имя>.json` и выполни `%s write --file .atlas/.local/inbox/<имя>.json`. Формат квитанций: `%s help receipts`.
3. Владелец сказал «на сегодня всё» — запиши закладку (операция `bookmark.save`) и покажи экран из вывода.
Codex Атлас узнаёт сам; другим агентам добавлять к командам `--agent <имя>`, например `--agent opencode`.
«Мы же решили…» — искать решение в записях (`%s find <слова>`); новое решение только из явных слов владельца.
Деньги, публикация, настоящие данные — только по явному «да» владельца. Файлы `.atlas/` руками не править (кроме квитанций в `.atlas/.local/inbox/`).
Если команда не найдена — скилл Атласа переустановлен в другое место: выполни `atlas.py connect` из новой папки скилла.""" % (
        (command_line(),) * 4)


# ----------------------------------------------------------------------
def _upsert_block(path, body):
    """Insert or replace our marked block; never touch other text."""
    existed = os.path.exists(path)
    text = ""
    if existed:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    block = "%s\n%s\n%s" % (BLOCK_BEGIN, body, BLOCK_END)
    start = text.find("<!-- atlas:begin")
    end = text.find(BLOCK_END)
    if start != -1 and end != -1 and end > start:
        new = text[:start] + block + text[end + len(BLOCK_END):]
        action = "обновил" if new != text else "без изменений"
    else:
        new = (text.rstrip("\n") + "\n\n" + block + "\n") if text.strip() else block + "\n"
        action = "добавил" if existed else "создал файл и добавил"
    if new != text:
        write_text_atomic(path, new)
    return action


def _remove_block(path):
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.find("<!-- atlas:begin")
    end = text.find(BLOCK_END)
    if start == -1 or end == -1:
        return False
    new = text[:start].rstrip("\n") + text[end + len(BLOCK_END):]
    new = new.strip("\n")
    if not new.strip():
        os.remove(path)  # only our block was there
        return True
    write_text_atomic(path, new + "\n")
    return True


def _settings_path(project):
    return os.path.join(project, ".claude", "settings.local.json")


def _load_settings(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise AtlasError("Файл %s не читается как JSON — хуки не добавлены, файл не тронут." % path)
    if not isinstance(data, dict):
        raise AtlasError("Файл %s имеет неожиданный вид — хуки не добавлены, файл не тронут." % path)
    return data


def _is_ours(entry):
    for h in (entry or {}).get("hooks", []):
        cmd = h.get("command", "")
        if "atlas.py" in cmd and HOOK_MARK in cmd:
            return True
    return False


def _hook_entry(event):
    cmd = '"%s" "%s" hook %s %s' % (sys.executable, script_path(), event, HOOK_MARK)
    return {"hooks": [{"type": "command", "command": cmd, "timeout": 30}]}


def install_project_hooks(project):
    path = _settings_path(project)
    data = _load_settings(path)
    hooks = data.setdefault("hooks", {})
    before = json.dumps(hooks, sort_keys=True)
    for event, name in (("SessionStart", "session-start"), ("Stop", "stop")):
        entries = [e for e in hooks.get(event, []) if not _is_ours(e)]
        entries.append(_hook_entry(name))
        hooks[event] = entries
    changed = json.dumps(hooks, sort_keys=True) != before
    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed


def remove_project_hooks(project):
    path = _settings_path(project)
    if not os.path.exists(path):
        return False
    data = _load_settings(path)
    hooks = data.get("hooks") or {}
    removed = False
    for event in list(hooks):
        kept = [e for e in hooks[event] if not _is_ours(e)]
        if len(kept) != len(hooks[event]):
            removed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed:
        if not hooks:
            data.pop("hooks", None)
        if not data:
            os.remove(path)  # the file held only Atlas hooks
        else:
            write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return removed


def _git_ignored(project, rel):
    try:
        out = subprocess.run(["git", "-C", project, "check-ignore", "-q", rel],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.returncode == 0


# ----------------------------------------------------------------------
def connect(project, name=None, hooks="auto", agents_md=False, claude_md=True):
    from .ops import Context, write_core  # local import to avoid a cycle

    store = Store(project)
    fresh = not store.exists()
    store.ensure_layout()
    changes = []
    if fresh:
        cfg = {"schema_version": SCHEMA_VERSION, "atlas_version": VERSION, "enabled": True,
               "created_at": ru.now_iso()}
        store.save_config(cfg)
        changes.append("создал папку .atlas/ (записи, настройки; личное — в .atlas/.local/, в git не попадает)")
        ctx = Context(project, "atlas", None)
        write_core(store, [("project.init", {"name": name or os.path.basename(os.path.abspath(project))}, None)], ctx)
    else:
        cfg = store.load_config()
        if not cfg.get("enabled", True):
            cfg["enabled"] = True
            store.save_config(cfg)
            changes.append("снова включил запись в .atlas/ (данные были сохранены)")
    claude_path = os.path.join(project, "CLAUDE.md")
    if claude_md or os.path.exists(claude_path):
        action = _upsert_block(claude_path, CLAUDE_BLOCK)
        if action != "без изменений":
            changes.append("%s блок Атласа в CLAUDE.md (инструкции агента)" % action)
    if agents_md:
        action = _upsert_block(os.path.join(project, "AGENTS.md"), agents_block())
        if action != "без изменений":
            changes.append("%s блок Атласа в AGENTS.md (ручной режим для Codex и других агентов)" % action)
    mode = detect_hooks_mode() if hooks == "auto" else hooks
    if mode == "project":
        if install_project_hooks(project):
            changes.append("добавил два хука (события, на которые агент вызывает Атлас) в .claude/settings.local.json: "
                           "старт сессии и конец ответа")
        ignored = _git_ignored(project, ".claude/settings.local.json")
        if ignored is False:
            changes.append("внимание: .claude/settings.local.json не в .gitignore — это ваши личные настройки с путём к Атласу")
    elif mode == "plugin":
        remove_project_hooks(project)
    local = store.load_local("install.json")
    if mode != "keep":
        local.update({"hooks_mode": mode, "script": script_path()})
    local.update({"installed_at": ru.now_iso(), "atlas_version": VERSION,
                  "agents_md": bool(agents_md or local.get("agents_md"))})
    store.save_local("install.json", local)
    return {"fresh": fresh, "changes": changes, "mode": mode}


def disconnect(project):
    store = Store(project)
    if not store.exists():
        raise AtlasError("Атлас к этому проекту не подключён.")
    removed = []
    if _remove_block(os.path.join(project, "CLAUDE.md")):
        removed.append("блок Атласа из CLAUDE.md")
    if _remove_block(os.path.join(project, "AGENTS.md")):
        removed.append("блок Атласа из AGENTS.md")
    try:
        if remove_project_hooks(project):
            removed.append("два хука Атласа из .claude/settings.local.json")
    except AtlasError as exc:
        removed.append("хуки не тронуты: %s" % exc)
    cfg = store.load_config()
    cfg["enabled"] = False
    cfg["disabled_at"] = ru.now_iso()
    store.save_config(cfg)
    return removed


def changes_report(project):
    """«Покажи изменения»: exactly what Atlas added to the project."""
    store = Store(project)
    lines = []
    if store.exists():
        lines.append("Папка .atlas/ — записи Атласа (канонические: .atlas/data/records/; личное: .atlas/.local/)")
    for fname in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(project, fname)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if "<!-- atlas:begin" in fh.read():
                    lines.append("%s — блок между метками atlas:begin и atlas:end; остальной текст не тронут" % fname)
    try:
        data = _load_settings(_settings_path(project))
        n = sum(1 for ev in (data.get("hooks") or {}).values() for e in ev if _is_ours(e))
        if n:
            lines.append(".claude/settings.local.json — %d хука Атласа (старт сессии и конец ответа)" % n)
    except AtlasError as exc:
        lines.append(str(exc))
    local = store.load_local("install.json") if store.exists() else {}
    if local.get("hooks_mode") == "plugin":
        lines.append("Хуки приходят из плагина Атласа — в настройки проекта ничего не записано")
    lines.append("Код проекта, ваши инструкции вне блока и настройки git Атлас не менял.")
    return lines
