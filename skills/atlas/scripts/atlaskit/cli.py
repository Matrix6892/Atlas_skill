"""Command line: `python3 atlas.py <command>`. Owner-facing output is Russian."""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

from . import VERSION, SCHEMA_VERSION
from . import ru
from . import views
from .brief import build as build_brief, build_full
from .hooks import run_hook, session_start_output, prefs as load_prefs, gap_unknown_lines, report_gap_lines, _session_manifest, \
    _save_session_manifest
from .install import connect, disconnect, changes_report, command_line, detect_hooks_mode, script_path
from .ops import Context, apply_receipt, write_core, load_state, SECRET_PATTERNS, norm, scrub_secrets
from .store import Store, AtlasError, NotConnected, find_project, worktree_fingerprint, git_available, git_recent, \
    new_id

EXIT_OK, EXIT_USAGE, EXIT_PARTIAL, EXIT_NOTHING, EXIT_ERROR = 0, 2, 3, 4, 5

FIND_STOPWORDS = {
    "мы", "же", "ведь", "вроде", "решили", "решение", "решали", "договорились", "что", "это", "про", "как",
    "давай", "так", "его", "она", "они", "было", "был", "уже", "там", "тут", "для", "или", "все", "всё",
}


def out(text=""):
    sys.stdout.write(text + ("\n" if not text.endswith("\n") else ""))


def default_agent():
    if os.environ.get("ATLAS_AGENT"):
        return os.environ["ATLAS_AGENT"]
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SANDBOX"):
        return "codex"
    return "agent"


def session_of(args):
    """Session id: explicit flag, then the hook-provided value, then the
    thread id that Codex passes to every command it runs."""
    return (getattr(args, "session", None) or os.environ.get("ATLAS_SESSION_ID")
            or os.environ.get("CODEX_THREAD_ID"))


def agent_of(args):
    return getattr(args, "agent", None) or default_agent()


def open_store(args, must_exist=True):
    project = find_project(args.project)
    if not project:
        if must_exist:
            raise NotConnected("Атлас не подключён к этому проекту (нет папки .atlas/). Скажите «подключи Атлас».")
        return None
    store = Store(project)
    if must_exist and not store.exists():
        raise NotConnected("Атлас не подключён к этому проекту. Скажите «подключи Атлас».")
    return store


def load(store):
    st, problem = load_state(store)
    return st, problem


def problem_line(problem):
    if not problem:
        return None
    return ("Записи повреждены начиная с %s: %s. Показано состояние до этой записи; новые записи не делаются — "
            "запустите «atlas.py doctor»." % (problem["file"], problem["why"]))


def terms_for(store, session):
    if not session:
        return views.Terms()
    m = _session_manifest(store, session)
    return views.Terms(m.get("terms_seen"))


def save_terms(store, session, terms):
    if not session:
        return
    m = _session_manifest(store, session)
    if not m:
        m = {"id": session, "agent": default_agent(), "started_at": ru.now_iso(), "mode": "unknown", "checked": True}
    m["terms_seen"] = sorted(terms.seen)
    _save_session_manifest(store, session, m)


def find_thread(st, value):
    if value in st.threads:
        return st.threads[value]
    found = [t for t in st.live_threads() if norm(t["title"]) == norm(value)]
    if not found:
        found = [t for t in st.live_threads() if norm(value) in norm(t["title"])]
    if len(found) == 1:
        return found[0]
    if not found:
        raise AtlasError("Тема «%s» не найдена." % value)
    raise AtlasError("Под «%s» подходит несколько тем: %s." % (value, "; ".join(
        "%s (%s)" % (t["title"], t["id"]) for t in found)))


# ----------------------------------------------------------------------
def cmd_connect(args):
    project = os.path.abspath(args.project or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    hooks, agents_md, claude_md = args.hooks, args.agents_md, True
    if agent_of(args) != "claude-code":
        # Codex, opencode and others: no Claude hooks to add; they follow the
        # manual order from AGENTS.md. Hooks set up by Claude Code stay as they are.
        if hooks == "auto":
            hooks = "keep"
        agents_md = True
        claude_md = False  # CLAUDE.md gets the block only if it already exists
    res = connect(project, name=args.name, hooks=hooks, agents_md=agents_md, claude_md=claude_md)
    store = Store(project)
    st, _ = load(store)
    name = st.project_name or os.path.basename(project)
    if res["fresh"]:
        out("Атлас подключён к «%s»." % name)
    else:
        out("Атлас уже был подключён к «%s»; проверил установку." % name)
    if res["changes"]:
        out("Изменил:    " + res["changes"][0] + ".")
        for c in res["changes"][1:]:
            out("            " + c + ".")
    else:
        out("Изменил:    ничего — всё уже на месте.")
    out("Не трогал:  код, ваши инструкции вне блока Атласа, настройки git.")
    if res["mode"] == "plugin":
        out("Хуки:       приходят из плагина Атласа.")
    elif res["mode"] in ("none", "keep"):
        out("Хуки:       у этого агента нет хуков Атласа — режим «обновление по команде»: "
            "доклад и записи по инструкции в AGENTS.md.")
    out("Договорённость соблюдает агент; технически Атлас защищает только свои записи — "
        "команды агента вне Атласа он не останавливает (рекомендательный режим).")
    if res["mode"] in ("plugin", "project"):
        out("Хуки начнут работать со следующей сессии Claude Code.")
    return EXIT_OK


def cmd_disconnect(args):
    store = open_store(args)
    removed = disconnect(store.project)
    out("Атлас выключен. Проект работает как раньше.")
    out("Убрал: %s." % (", ".join(removed) if removed else "добавленных блоков и хуков не нашёл"))
    out("Данные сохранены в .atlas/ — экспорт в обычный файл: «atlas.py export». Включить снова: «подключи Атлас».")
    return EXIT_OK


def cmd_changes(args):
    store = open_store(args)
    for line in changes_report(store.project):
        out(line)
    return EXIT_OK


def cmd_start(args):
    store = open_store(args)
    cfg = store.load_config()
    if not cfg.get("enabled", True):
        out("Запись выключена, данные сохранены. Включить: «подключи Атлас».")
        return EXIT_OK
    sid = session_of(args) or new_id("manual")
    agent = agent_of(args)
    res = session_start_output(store, sid, agent, "startup", "manual", command_line())
    out("=== ДОКЛАД ДЛЯ ВЛАДЕЛЬЦА (показать как есть) ===")
    out(res["report"])
    out("")
    out("=== СВОДКА ДЛЯ АГЕНТА ===")
    out(res["brief"])
    out("Сессия для записей: --session %s --agent %s" % (sid, agent))
    return EXIT_OK


def cmd_report(args):
    store = open_store(args)
    st, problem = load(store)
    gap_lines = ([problem_line(problem)] if problem else []) + report_gap_lines(st)
    out(views.report(st, session_of(args), gap_lines))
    return EXIT_OK


def cmd_overview(args):
    store = open_store(args)
    st, problem = load(store)
    gaps = gap_unknown_lines(st)
    if problem:
        gaps.insert(0, problem_line(problem))
    out(views.overview(st, gaps, st.last_tx))
    return EXIT_OK


def cmd_needs(args):
    store = open_store(args)
    st, problem = load(store)
    session = session_of(args)
    terms = terms_for(store, session)
    briefs = store.load_local(os.path.join("manifests", "briefs.json"))
    out(views.needs_view(st, briefs, bool(st.active_frames()), terms))
    save_terms(store, session, terms)
    return EXIT_OK


def cmd_result(args):
    store = open_store(args)
    st, problem = load(store)
    session = session_of(args)
    terms = terms_for(store, session)
    if args.target:
        r = st.results.get(args.target)
        if r is None:
            t = find_thread(st, args.target)
            r = st.latest_result(t["id"])
            if r is None:
                out("У темы «%s» пока нет предъявленного результата." % t["title"])
                return EXIT_OK
        out(views.result_card(st, r, worktree_fingerprint(store.project), terms))
    else:
        items = [n for n in st.needs() if n["type"] == "result"]
        if not items:
            out("Предъявленных результатов, которые ждут проверки или приёмки, нет.")
            return EXIT_OK
        fp = worktree_fingerprint(store.project)
        out("\n\n".join(views.result_card(st, n["obj"], fp, terms) for n in items))
    save_terms(store, session, terms)
    return EXIT_OK


def cmd_topic(args):
    store = open_store(args)
    st, problem = load(store)
    session = session_of(args)
    terms = terms_for(store, session)
    t = find_thread(st, args.thread)
    out(views.topic_view(st, t, full=args.all, terms=terms))
    save_terms(store, session, terms)
    return EXIT_OK


def cmd_parking(args):
    store = open_store(args)
    st, _ = load(store)
    out(views.parking_view(st, show_all=args.all))
    return EXIT_OK


def cmd_decisions(args):
    store = open_store(args)
    st, _ = load(store)
    out(views.decisions_view(st, include_closed=args.all))
    return EXIT_OK


def cmd_find(args):
    store = open_store(args)
    st, _ = load(store)
    q = norm(" ".join(args.text))
    words = [w for w in re.split(r"\W+", q) if len(w) > 2 and w not in FIND_STOPWORDS]
    if not words:
        raise AtlasError("Слишком короткий запрос.")
    stems = [w[:max(4, len(w) - 2)] for w in words]

    def hit(text):
        # Word stems, most of them present: «решили не делать телефонную
        # регистрацию» finds «Телефонную регистрацию не делаем».
        t = norm(text or "")
        if not t:
            return False
        found = sum(1 for s in stems if s in t)
        return found >= max(1, (len(stems) + 1) // 2)

    lines = []
    for d in sorted(st.decisions.values(), key=lambda d: d["number"]):
        if d.get("error"):
            continue
        if hit(d.get("text")) or hit(d.get("question")):
            src = d.get("decided_source") or {}
            status = {"decided": "действует", "proposed": "открытый вопрос", "superseded": "заменено",
                      "withdrawn": "снято"}[d["status"]]
            who = " — ваше" if src.get("type") == "owner_message" else ""
            when = ru.short_date(ru.parse_ts(d.get("decided_at") or d["created_at"]))
            lines.append("решение %d (%s): %s — %s%s, %s" % (
                d["number"], d["id"], d.get("text") or d.get("question"), status, who, when))
            if src.get("quote"):
                lines.append("    слова владельца: «%s»" % src["quote"])
    for t in st.live_threads():
        if hit(t["title"]) or hit(t.get("why")):
            lines.append("тема %s «%s» — %s" % (t["id"], t["title"], ru.THREAD_STATE[st.view_state(t)]))
    for c in st.criteria.values():
        if not c.get("error") and hit(c["text"]):
            t = st.threads.get(c["thread_id"], {})
            lines.append("условие готовности %s «%s» — тема «%s»%s" % (
                c["id"], c["text"], t.get("title", "?"), ", снято" if c["withdrawn"] else ""))
    for p in st.parking.values():
        if not p.get("error") and hit(p["text"]):
            lines.append("парковка %s «%s» — %s" % (p["id"], p["text"], {"parked": "в парковке", "promoted": "стала темой",
                                                                       "released": "отпущено"}[p["status"]]))
    for n in st.notes.values():
        if not n.get("error") and hit(n["text"]):
            lines.append("запись %s (%s, %s): %s" % (n["id"], n["kind"], ru.short_date(ru.parse_ts(n["created_at"])), n["text"]))
    if not lines:
        out("В записях ничего не нашлось по «%s». Если это решение — его в записях нет; "
            "спросите владельца, записать ли его сейчас." % " ".join(args.text))
        return EXIT_OK
    out("\n".join(lines))
    return EXIT_OK


def cmd_brief(args):
    store = open_store(args)
    st, problem = load(store)
    session = session_of(args)
    local = store.load_local("install.json")
    mode = "hooks" if local.get("hooks_mode") in ("plugin", "project") else "manual"
    fn = build_full if args.full else build_brief
    text, manifest = fn(st, command_line(), session, st.last_tx, mode, gaps=gap_unknown_lines(st))
    if problem:
        out(problem_line(problem))
    out(text)
    if session:
        briefs = store.load_local(os.path.join("manifests", "briefs.json"))
        seen = dict(manifest["decisions"])
        seen["_agent"] = agent_of(args)
        briefs[session] = seen
        store.save_local(os.path.join("manifests", "briefs.json"), briefs)
    return EXIT_OK


def cmd_show(args):
    store = open_store(args)
    st, _ = load(store)
    obj = st.lookup(args.id)
    if obj is None:
        rec = next((r for r in st.records if r.get("id") == args.id), None)
        if rec is None:
            raise AtlasError("Объект «%s» не найден." % args.id)
        obj = rec
    clean = {k: v for k, v in obj.items() if not k.startswith("_")}
    out(json.dumps(clean, ensure_ascii=False, indent=2))
    return EXIT_OK


def render_write(res):
    lines = []
    applied, failed = res["applied"], res["failed"]
    if res.get("repeat"):
        lines.append("Повтор квитанции: она уже была принята (запись №%d), ничего не удвоено. Прежний результат:" % res["tx"])
    elif res.get("dry_run"):
        lines.append("Проверка без записи — было бы записано:")
    elif res.get("tx"):
        lines.append("Записано, запись подтверждена (снимок %d):" % res["tx"])
    elif res.get("whole"):
        lines.append("Ничего не записано: пакет применяется только целиком, а в нём ошибка.")
    else:
        lines.append("Ничего не записано.")
    for i, line in applied:
        lines.append("  + %s" % line)
    if failed:
        lines.append("Не записано:")
        for f in failed:
            lines.append("  - операция %d (%s): %s" % (f["index"] + 1, f["op"], f["why"]))
    if res.get("refs"):
        lines.append("Ссылки: " + " ".join("%s=%s" % kv for kv in sorted(res["refs"].items())))
    if failed and applied:
        code = EXIT_PARTIAL
    elif failed:
        code = EXIT_NOTHING
    else:
        code = EXIT_OK
    return "\n".join(lines), code


def cmd_write(args):
    store = open_store(args)
    cfg = store.load_config()
    if not cfg.get("enabled", True):
        raise AtlasError("Запись выключена (Атлас отключён), данные сохранены. Включить: «подключи Атлас».")
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()
    try:
        receipt = json.loads(raw)
    except ValueError as exc:
        raise AtlasError("Квитанция не читается как JSON: %s" % exc)
    session = session_of(args)
    ctx = Context(store.project, agent_of(args), session)
    res = apply_receipt(store, receipt, ctx, dry_run=args.dry_run)
    text, code = render_write(res)
    out(text)
    if args.file and not args.dry_run and (res.get("tx") or res.get("repeat")) and not res.get("failed"):
        # A receipt dropped into .atlas/.local/inbox/ is consumed once written.
        path = os.path.abspath(args.file)
        if os.path.dirname(path) == os.path.abspath(store.inbox):
            try:
                os.remove(path)
            except OSError:
                pass
    if args.file and res.get("failed") and (res.get("tx") or res.get("repeat")):
        out("Квитанция оставлена в inbox: неприменённое исправьте новой квитанцией с новым ключом.")
    if res.get("tx") and not res.get("dry_run") and not res.get("repeat"):
        st, _ = load(store)
        kinds = [op.get("op") for op in receipt["ops"] if isinstance(op, dict)]
        if "bookmark.save" in kinds:
            bm = st.last_bookmark()
            if bm and bm["created_tx"] == res["tx"]:
                p = load_prefs(store)
                out("")
                out("=== ПОКАЗАТЬ ВЛАДЕЛЬЦУ ===")
                out(views.bookmark_view(st, bm, ask_mood=bool(p.get("ask_mood", True))))
    return code


def cmd_check(args):
    """Observed run (B.2): the core itself runs the command and records the
    exit code — the only way to get «проверено тестом».

    Lifecycle (R07): everything is validated BEFORE the process starts —
    project enabled, result presented and not withdrawn, criteria inside the
    version. The command line is stored and shown only scrubbed (R05). The
    state is fingerprinted before and after; a run that changed files is
    marked, not silently bound to the result."""
    store = open_store(args)
    if not store.load_config().get("enabled", True):
        raise AtlasError("Запись выключена (Атлас отключён): проверку не запускаю и не записываю. "
                         "Включить: «подключи Атлас».")
    if not args.cmd:
        raise AtlasError("Укажите команду проверки после «--», например: atlas.py check --thread «Вход» --criteria cr-… -- npm test")
    cmd = args.cmd[1:] if args.cmd[0] == "--" else args.cmd
    if not cmd:
        raise AtlasError("Пустая команда проверки.")
    st, problem = load(store)
    if problem:
        raise AtlasError(problem_line(problem))
    if args.result:
        r = st.results.get(args.result)
        if not r or r.get("error"):
            raise AtlasError("Результат «%s» не найден." % args.result)
    else:
        if not args.thread:
            raise AtlasError("Укажите --thread или --result: проверка привязывается к версии результата.")
        t = find_thread(st, args.thread)
        r = st.latest_result(t["id"])
        if not r:
            raise AtlasError("У темы «%s» нет предъявленного результата. Сначала result.present, потом проверка — "
                             "она привязывается к версии." % t["title"])
    if r.get("withdrawn"):
        raise AtlasError("Результат r%d отозван — проверять нечего." % r["version"])
    crits = [c.strip() for c in (args.criteria or "").split(",") if c.strip()]
    if not crits:
        raise AtlasError("Укажите --criteria: какие условия готовности проверяет эта команда (не больше, чем она проверяет).")
    in_version = {e["id"] for e in r["criteria"]}
    receipt_crits = []
    for c in crits:
        crit = st.criteria.get(c)
        if not crit or crit.get("error") or crit["withdrawn"] or crit["id"] not in in_version:
            raise AtlasError("Условие «%s» не входит в версию r%d — команду не запускаю." % (c, r["version"]))
        receipt_crits.append({"id": crit["id"], "scope_rev": crit["scope_rev"]})
    shown_cmd = scrub_secrets(" ".join(cmd))[:500]
    env = scrub_secrets(args.env or "")
    fp_before = worktree_fingerprint(store.project)
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=store.project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=args.timeout)
        code = proc.returncode
        output = proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, output = None, "превышено время ожидания %d с" % args.timeout
    except OSError as exc:
        code, output = None, "команда не запустилась: %s" % exc
    took = time.time() - started
    fp_after = worktree_fingerprint(store.project)
    changed = bool(fp_before and fp_after and fp_before.get("hash") != fp_after.get("hash"))
    tail = scrub_secrets("\n".join(output.strip().splitlines()[-15:]))
    outcome = "passed" if code == 0 else ("failed" if code is not None else "could_not_check")
    ctx = Context(store.project, agent_of(args), session_of(args))
    data = {"verification_id": new_id("ver"), "result_id": r["id"], "thread_id": r["thread_id"],
            "criteria": receipt_crits, "method": "automated_run", "outcome": outcome,
            "environment": env, "limitations": [], "detail": None, "fingerprint": fp_before,
            "fingerprint_after": fp_after, "changed_during_run": changed,
            "observed": {"command": shown_cmd, "exit_code": code, "seconds": round(took, 1),
                         "tail": tail[-2000:], "observer": "atlas-core"}}
    write_core(store, [("verify", data, {"type": "core", "observer": "atlas-core"})], ctx, via="check")
    t = st.threads[r["thread_id"]]
    word = {"passed": "проверено тестом", "failed": "тест не прошёл — тема возвращается в работу",
            "could_not_check": "проверить не удалось (провалом не считается)"}[outcome]
    out("Запуск наблюдался ядром Атласа: `%s` → %s за %.0f с." % (
        shown_cmd, ("код %d" % code) if code is not None else "без кода завершения", took))
    out("Записано: «%s» (версия r%d) — %s: %s." % (t["title"], r["version"], word,
                                                 "; ".join(st.criteria[c["id"]]["text"] for c in receipt_crits)))
    rfp = (r.get("fingerprint") or {}).get("hash")
    if rfp and fp_before and rfp != fp_before.get("hash"):
        out("Внимание: код менялся после предъявления r%d — проверка относится к текущему состоянию, "
            "к r%d она не засчитается. Предъявите новую версию (result.present) и проверьте её." % (r["version"], r["version"]))
    if changed:
        out("Внимание: сама проверка изменила файлы проекта — к версии r%d она не засчитается, "
            "пока не станет ясно, что изменилось." % r["version"])
    if outcome != "passed":
        out("Хвост вывода:\n" + tail)
    return EXIT_OK if outcome == "passed" else EXIT_PARTIAL


def cmd_mood(args):
    store = open_store(args)
    value = " ".join(args.value).strip().lower()
    allowed = {"тяжело": "hard", "нормально": "ok", "в кайф": "great", "пропустить": None}
    if value in ("не спрашивай", "не спрашивать", "off"):
        p = store.load_local("prefs.json")
        p["ask_mood"] = False
        store.save_local("prefs.json", p)
        out("Больше не спрашиваю «как прошло». Вернуть: «atlas.py prefs ask_mood true».")
        return EXIT_OK
    if value not in allowed:
        raise AtlasError("Ответ: тяжело, нормально, в кайф, пропустить или «не спрашивай».")
    if allowed[value] is None:
        out("Пропущено. Ничего не записано.")
        return EXIT_OK
    path = os.path.join(store.private_dir, "mood.jsonl")
    os.makedirs(store.private_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": ru.now_iso(), "mood": allowed[value]}, ensure_ascii=False) + "\n")
    out("Сохранено только у вас на компьютере (.atlas/.local/private) — не попадает в git, экспорт и сводку агенту.")
    return EXIT_OK


def cmd_prefs(args):
    store = open_store(args)
    p = load_prefs(store)
    if args.key is None:
        for k, v in sorted(p.items()):
            out("%s = %s" % (k, json.dumps(v, ensure_ascii=False)))
        return EXIT_OK
    allowed = {"report_at_start": ("system_message", "agent", "off"), "nudge_after_turns": int, "ask_mood": bool}
    if args.key not in allowed:
        raise AtlasError("Настройки: %s." % ", ".join(sorted(allowed)))
    if args.value is None:
        raise AtlasError("Укажите значение.")
    kind = allowed[args.key]
    if kind is int:
        value = int(args.value)
    elif kind is bool:
        value = args.value.lower() in ("1", "true", "да", "yes", "on")
    else:
        if args.value not in kind:
            raise AtlasError("Допустимо: %s." % ", ".join(kind))
        value = args.value
    local = store.load_local("prefs.json")
    local[args.key] = value
    store.save_local("prefs.json", local)
    out("%s = %s (только на этом компьютере)" % (args.key, json.dumps(value, ensure_ascii=False)))
    return EXIT_OK


def cmd_rules(args):
    store = open_store(args)
    st, _ = load(store)
    lines = []
    for f in st.active_frames():
        if f.get("standing"):
            lines.append("Договорённость по умолчанию: %s; остановлюсь: %s (%s)" % (f["doing"], f["stop_when"], f["id"]))
    for r in st.active_rules():
        lines.append("Правило: %s (%s)" % (r["text"], r["id"]))
    for r in st.proposed_rules():
        lines.append("Предложено, не действует: %s (%s)" % (r["text"], r["id"]))
    lines.append("Лимит тем в работе: %s" % st.settings.get("wip_limit", 3))
    out("\n".join(lines) if lines else "Правил проекта нет.")
    return EXIT_OK


def cmd_scan(args):
    store = open_store(args)
    if not git_available(store.project):
        out("git в проекте нет — последние изменения увидеть не могу. Назовите тему сами.")
        return EXIT_OK
    log, status = git_recent(store.project, args.limit)
    out("Сырьё для разбора (ничего не запускал, только читал git):")
    if status and status.strip():
        out("Незакоммиченные изменения:")
        for line in status.strip().splitlines()[:30]:
            if ".atlas/" not in line:
                out("  " + line)
    if log:
        out("Последние коммиты (дата, сообщение, файлы):")
        for block in log.split("@@")[1:]:
            head, *files = block.strip().splitlines()
            h, date, subj = (head.split("|", 2) + ["", ""])[:3]
            files = [f for f in files if f and not f.startswith(".atlas/")]
            out("  %s %s — %s" % (date[:10], subj, ", ".join(files[:6]) + (" …" if len(files) > 6 else "")))
    return EXIT_OK


def cmd_export(args):
    store = open_store(args)
    st, problem = load(store)
    lines = ["# %s — экспорт Атласа" % (st.project_name or "Проект"), "",
             "Собрано %s из записей (снимок %d). Личное (оценки «как прошло», сырые журналы) не включено." % (
                 ru.words_full(ru.now()), st.last_tx), ""]
    if problem:
        lines += ["> " + problem_line(problem), ""]
    g = st.goal()
    lines += ["## Цель", "", g["text"] if g else "не записана", ""]
    lines += ["## Темы", ""]
    for t in sorted(st.live_threads(), key=lambda t: t["created_tx"]):
        lines.append("### %s — %s" % (t["title"], ru.THREAD_STATE[st.view_state(t)]))
        lines.append("")
        lines.append("```text")
        lines.append(views.topic_view(st, t, full=True))
        lines.append("```")
        lines.append("")
    lines += ["## Решения", "", "```text", views.decisions_view(st, include_closed=True), "```", ""]
    lines += ["## Парковка", "", "```text", views.parking_view(st, show_all=True), "```", ""]
    released = [p for p in st.parking.values() if p["status"] == "released" and not p.get("error")]
    if released:
        lines += ["## Архив идей (отпущено)", ""]
        lines += ["- %s — %s" % (p["text"], p.get("status_reason") or "") for p in released]
        lines.append("")
    lines += ["## Канонические записи", "",
              "Все записи — обычные JSON-файлы в `.atlas/data/records/`; их можно читать без Атласа.", ""]
    path = args.out or os.path.join(store.root, "export", "atlas-%s.md" % ru.now().strftime("%Y-%m-%d"))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    out("Экспорт готов: %s. Личное не включено." % os.path.relpath(path, store.project))
    return EXIT_OK


# The field that names the object a record creates or changes (for redact:
# delete the object's own records, not every record that mentions it).
PRIMARY_KEY = {
    "goal": "goal_id", "thread": "thread_id", "criterion": "criterion_id", "note": "note_id",
    "attempt": "attempt_id", "decision": "decision_id", "park": "item_id", "result": "result_id",
    "verify": "verification_id", "accept": "acceptance_id", "frame": "frame_id", "rule": "rule_id",
    "bookmark": "bookmark_id", "capture": "gap_id",
}
# Structure, not content: kept by redaction so links and projections still work.
STRUCT_KEYS = {"kind", "state", "method", "outcome", "status", "urgency", "risk", "class", "action", "to",
               "from", "observer", "exit_code", "version", "number", "scope_rev", "seconds", "head", "hash",
               "dirty", "type", "agent", "session", "at", "created", "delegated", "substantive", "standing",
               "mode", "code_changed", "over_limit", "goal_related", "observable", "new_approach",
               "changed_during_run", "downgraded", "can_defer", "auto", "op", "group", "index", "refs"}
ID_RE = re.compile(r"^(?:th|cr|dec|park|res|ver|acc|fr|rule|note|try|bm|fix|gap|goal|rec|tx)-[a-z0-9]{8}$")


def _redact_value(value, marker, removed, key=None):
    if key in STRUCT_KEYS or key and key.endswith("_id"):
        return value
    if isinstance(value, str):
        if ID_RE.match(value) or value == marker:
            return value
        if len(value.strip()) >= 3:
            removed.append(value)
        return marker
    if isinstance(value, list):
        return [_redact_value(v, marker, removed, key if not isinstance(v, (dict, list)) else None) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v, marker, removed, k) for k, v in value.items()}
    return value


def _redact_hits(txs, target):
    hits = []
    for tx in txs:
        for rec in tx["records"]:
            d = rec.get("data", {})
            primary = PRIMARY_KEY.get(rec["kind"].split(".")[0])
            if (rec.get("id") == target or (primary and d.get(primary) == target)
                    or (rec["kind"] == "correct" and d.get("target") == target)):
                hits.append((tx, rec))
    return hits


def cmd_redact(args):
    """Sensitive content removal (F.3): the one exception to immutability.

    Removes every text of the object's own records at any depth (R06), keeps
    IDs, types and links, then re-reads the records to check that none of the
    removed text is left. Limits are named: git history, exports, what was
    already sent to the model."""
    store = open_store(args)
    st, problem = load(store)
    if problem:
        raise AtlasError(problem_line(problem))
    target = args.target
    txs, _ = store.load_transactions()
    hits = _redact_hits(txs, target)
    if not hits:
        raise AtlasError("Не нашёл записей объекта «%s»." % target)
    tracked = subprocess.run(["git", "-C", store.project, "ls-files", "--", ".atlas/data/records/"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
    exports = sorted(glob.glob(os.path.join(store.root, "export", "*.md")))
    out("Где это лежит:")
    for tx, rec in hits:
        out("  .atlas/data/records/%06d.json — запись %s (%s)" % (tx["tx"], rec["id"], rec["kind"]))
    out("  доклады и сводки — собираются из записей и после удаления текста не покажут")
    if exports:
        out("  экспорты в .atlas/export/ (%d) — Атлас их не чистит: удалите их или сделайте экспорт заново" % len(exports))
    out("Удалить не могу: %sуже отправленное агенту и провайдеру модели; копии вне проекта и резервные копии." % (
        "историю git (эти файлы уже в git); " if tracked else ""))
    if not args.confirm:
        out("Ничего не изменено. Чтобы удалить из Атласа — только по явному «да» владельца: atlas.py redact %s --confirm" % target)
        return EXIT_OK
    marker = "[удалено владельцем %s]" % ru.short_date(ru.now())
    removed = []
    with store.lock():
        txs, _ = store.load_transactions()
        hits = _redact_hits(txs, target)
        by_tx = {}
        for tx, rec in hits:
            by_tx.setdefault(tx["tx"], tx)
            rec["data"] = _redact_value(rec.get("data", {}), marker, removed)
            src = rec.get("source") or {}
            if src.get("quote"):
                removed.append(src["quote"])
                src["quote"] = marker
            for key in ("delegation",):
                if isinstance(src.get(key), dict) and src[key].get("quote"):
                    removed.append(src[key]["quote"])
                    src[key]["quote"] = marker
        for tx in by_tx.values():
            # The stored receipt outcome repeats texts of its records.
            outcome = (tx.get("origin") or {}).get("outcome")
            if outcome:
                tx["origin"]["outcome"] = _redact_value(outcome, marker, removed)
            store.rewrite_transaction(tx)
    ctx = Context(store.project, agent_of(args), session_of(args))
    write_core(store, [("redaction", {"target": target, "records": [r["id"] for _, r in hits], "marker": marker},
                        {"type": "owner_message", "quote": "подтверждено командой redact --confirm"})], ctx, via="redact")
    # Check the leftovers in every canonical record, not only in the edited ones.
    left = set()
    texts = {t for t in removed if len(t) >= 4}
    for name in store.tx_files():
        with open(os.path.join(store.records_dir, name), encoding="utf-8") as fh:
            body = fh.read()
        for t in texts:
            if json.dumps(t, ensure_ascii=False)[1:-1] in body:
                left.add(name)
    out("Удалено из Атласа: очищено %s; ID и связи сохранены, в журнале — маркер без самого текста." % ru.count_words(
        len(hits), "запись", "записи", "записей"))
    if left:
        out("Тот же текст ещё встречается в других записях: %s — они относятся к другим объектам; "
            "удалите их отдельно (atlas.py redact <ID>)." % ", ".join(sorted(left)))
    else:
        out("Проверка остатков: в записях Атласа этого текста больше нет.")
    out("Если это был действующий ключ или пароль — его стоит сменить: удаление текста не отменяет возможного раскрытия.")
    return EXIT_OK


def cmd_doctor(args):
    project = find_project(args.project)
    ok = True
    out("Атлас %s, схема %s, Python %s" % (VERSION, SCHEMA_VERSION, sys.version.split()[0]))
    if sys.version_info < (3, 8):
        out("  Python старше 3.8 — нужен 3.8 или новее.")
        ok = False
    if not project:
        out("  Проект не подключён (нет .atlas/ выше текущей папки).")
        return EXIT_OK
    store = Store(project)
    out("  Проект: %s" % project)
    cfg = store.load_config()
    out("  Запись: %s" % ("включена" if cfg.get("enabled", True) else "выключена, данные сохранены"))
    out("  git: %s" % ("есть — версии результатов привязываются к состоянию кода" if git_available(project)
                       else "нет — версии результатов нельзя привязать к коду"))
    local = store.load_local("install.json")
    mode = local.get("hooks_mode") or "неизвестно (установка с другого компьютера — хуки из плагина, если он стоит)"
    out("  Хуки: %s" % mode)
    if local.get("script") and not os.path.exists(local["script"]):
        out("  Скрипт хуков не найден: %s — выполните «подключи Атлас» ещё раз." % local["script"])
        ok = False
    txs, problem = store.load_transactions()
    out("  Записей (транзакций): %d" % len(txs))
    if problem:
        ok = False
        out("  ПОВРЕЖДЕНИЕ: %s — %s. Всё до этого файла цело; новые записи не делаются, пока не разберёмся." % (
            problem["file"], problem["why"]))
    pend = [n for n in os.listdir(store.pending)] if os.path.isdir(store.pending) else []
    if pend:
        out("  Недописанные временные файлы: %d (прерванная запись; в записи не попали)" % len(pend))
    lock = os.path.join(store.locks, "writer.lock")
    if os.path.exists(lock):
        out("  Файл блокировки существует — если никто не пишет, он будет снят автоматически.")
    out("Итог: %s" % ("в порядке" if ok else "есть проблемы — см. выше"))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_help(args):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    topic = args.topic or "receipts"
    path = os.path.join(here, os.pardir, "references", "%s.md" % topic)
    if not os.path.exists(path):
        raise AtlasError("Справки «%s» нет. Есть: receipts, screens, phrases." % topic)
    with open(path, encoding="utf-8") as fh:
        out(fh.read())
    return EXIT_OK


def cmd_hook(args):
    return run_hook(args.event, args.via, command_line())


# ----------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="atlas.py", description="Атлас — память и карта проекта (этап M0).")
    p.add_argument("--project", help="папка проекта (по умолчанию ищется .atlas/ вверх от текущей)")
    p.add_argument("--session", help="ID сессии агента (по умолчанию $ATLAS_SESSION_ID)")
    p.add_argument("--agent", help="кто пишет: claude-code, codex … (по умолчанию $ATLAS_AGENT)")
    # The same options are accepted after the subcommand too.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=argparse.SUPPRESS)
    common.add_argument("--session", default=argparse.SUPPRESS)
    common.add_argument("--agent", default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command")
    _add = sub.add_parser

    def add_parser(name, **kw):
        kw.setdefault("parents", [common])
        return _add(name, **kw)

    sub.add_parser = add_parser

    s = sub.add_parser("connect", help="подключить Атлас к проекту (Э9)")
    s.add_argument("--name", help="название проекта словами владельца")
    s.add_argument("--hooks", choices=("auto", "project", "plugin", "none", "keep"), default="auto")
    s.add_argument("--agents-md", action="store_true", help="добавить блок ручного режима в AGENTS.md (Codex)")
    s.set_defaults(fn=cmd_connect)
    sub.add_parser("disconnect", help="отключить Атлас; данные остаются").set_defaults(fn=cmd_disconnect)
    sub.add_parser("changes", help="что Атлас изменил в проекте").set_defaults(fn=cmd_changes)
    sub.add_parser("start", help="ручной старт сессии: доклад и сводка (для агентов без хуков)").set_defaults(fn=cmd_start)
    sub.add_parser("report", help="доклад при входе (Э0)").set_defaults(fn=cmd_report)
    sub.add_parser("overview", help="«покажи всё»: главная в чате").set_defaults(fn=cmd_overview)
    sub.add_parser("needs", help="«Нужно от вас» (Э3)").set_defaults(fn=cmd_needs)
    s = sub.add_parser("result", help="карточка результата (Э4)")
    s.add_argument("target", nargs="?", help="тема или ID результата")
    s.set_defaults(fn=cmd_result)
    s = sub.add_parser("topic", help="история темы (Э2, кратко)")
    s.add_argument("thread")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_topic)
    s = sub.add_parser("parking", help="парковка, порция для уборки (Э8)")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_parking)
    s = sub.add_parser("decisions", help="решения и открытые вопросы")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_decisions)
    s = sub.add_parser("find", help="поиск в записях («мы же решили…»)")
    s.add_argument("text", nargs="+")
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("brief", help="сводка агенту (E.5)")
    s.add_argument("--full", action="store_true")
    s.set_defaults(fn=cmd_brief)
    s = sub.add_parser("show", help="объект целиком, с ревизией")
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    s = sub.add_parser("write", help="единственная точка записи: квитанция JSON из stdin")
    s.add_argument("--file")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_write)
    s = sub.add_parser("check", help="наблюдаемый запуск проверки: atlas.py check --thread T --criteria C -- команда")
    s.add_argument("--thread")
    s.add_argument("--result")
    s.add_argument("--criteria")
    s.add_argument("--env", default="на компьютере", help="где проверено, словами: «на компьютере»")
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("cmd", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("mood", help="«как прошло?» — личное, только локально")
    s.add_argument("value", nargs="+")
    s.set_defaults(fn=cmd_mood)
    s = sub.add_parser("prefs", help="локальные настройки (доклад при старте, напоминание, вопрос «как прошло»)")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_prefs)
    sub.add_parser("rules", help="правила проекта").set_defaults(fn=cmd_rules)
    s = sub.add_parser("scan", help="сырьё для первого разбора: последние изменения в git (Э9)")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("export", help="экспорт в обычный Markdown-файл")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("redact", help="удалить чувствительное из Атласа (F.3)")
    s.add_argument("target")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(fn=cmd_redact)
    sub.add_parser("doctor", help="диагностика установки и целостности записей").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("help", help="справка: receipts, screens, phrases")
    s.add_argument("topic", nargs="?")
    s.set_defaults(fn=cmd_help)
    s = sub.add_parser("hook", help="адаптер Claude Code (вызывают хуки)")
    s.add_argument("event", choices=("session-start", "stop"))
    s.add_argument("--via", choices=("plugin", "project"), default="project")
    s.set_defaults(fn=cmd_hook)
    sub.add_parser("version").set_defaults(fn=lambda a: out("Атлас %s (схема %s)" % (VERSION, SCHEMA_VERSION)) or EXIT_OK)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
            sys.stdin.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.fn(args) or EXIT_OK
    except NotConnected as exc:
        out(str(exc))
        return EXIT_NOTHING
    except AtlasError as exc:
        out(str(exc))
        return EXIT_ERROR
