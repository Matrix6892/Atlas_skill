"""Claude Code adapter (stage M0): SessionStart and Stop hooks (spec E.4).

* SessionStart: record the technical start (not an owner visit — F15, A76),
  look for incomplete capture of earlier sessions, give the agent its brief
  (additionalContext) and the owner the report Э0 (systemMessage).
* Stop is the end of an answer, not of a session or episode. It only marks
  a heartbeat for capture coverage and, rarely, reminds the agent to leave a
  record. It never blocks twice: `stop_hook_active` is respected and the
  reminder resets its counter.

Hooks must stay fast and must never break the session: any failure is
reported on stderr and the hook exits 0.
"""

import json
import os
import sys

from . import ru
from .brief import build as build_brief
from .ops import Context, write_core, load_state
from .store import Store, find_project, new_id, worktree_fingerprint
from . import views

DEFAULT_PREFS = {"report_at_start": "system_message", "nudge_after_turns": 3, "ask_mood": True}


def prefs(store):
    p = dict(DEFAULT_PREFS)
    p.update(store.load_local("prefs.json"))
    return p


def _session_manifest(store, sid):
    return store.load_local(os.path.join("manifests", "sessions", "%s.json" % _safe(sid)))


def _save_session_manifest(store, sid, data):
    store.save_local(os.path.join("manifests", "sessions", "%s.json" % _safe(sid)), data)


def _safe(sid):
    return "".join(ch for ch in (sid or "unknown") if ch.isalnum() or ch in "-_")[:80] or "unknown"


def _all_session_manifests(store):
    folder = os.path.join(store.manifests, "sessions")
    out = []
    if not os.path.isdir(folder):
        return out
    for name in os.listdir(folder):
        if name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda m: m.get("started_at") or "")


def detect_gaps(store, st, current_sid, fp_now):
    """Find earlier sessions whose capture is incomplete (§6.1, A06, A82).

    Only observable facts are used: a session with file changes but no end
    of answer recorded; files that changed after the session's last confirmed
    record (its checkpoint) — an early record does not cover later work (R09);
    files changed after the last recorded session. These are mismatches of
    observed boundaries, not proof of what was said. Returns new gap records.
    """
    found = []
    manifests = [m for m in _all_session_manifests(store) if m.get("id") != current_sid]
    unchecked = [m for m in manifests if not m.get("checked")]
    for m in unchecked:
        sid = m.get("id")
        if m.get("mode") != "hooks":
            # Manual mode has no end-of-answer events: its coverage is
            # unknown by design and shown as a mode, not as a failure.
            m["checked"] = True
            _save_session_manifest(store, sid, m)
            continue
        wrote = sum(1 for r in st.records if r.get("_session") == sid and r["kind"] != "session.start")
        start_fp = (m.get("fp_start") or {}).get("hash")
        last_fp = (m.get("fp_last") or {}).get("hash")
        now_hash = (fp_now or {}).get("hash")
        is_latest = m is manifests[-1] if manifests else False
        why = None
        what = None
        if not m.get("stops"):
            if (start_fp and now_hash and start_fp != now_hash) or (not start_fp and wrote):
                what, why = "no_stop", "хук конца ответа не сработал или сессия оборвалась"
        elif last_fp and ((st.checkpoints.get(sid) or {}).get("fingerprint") or {}).get("hash", start_fp) != last_fp:
            if wrote:
                what, why = "tail", "после последней записи файлы менялись — эта часть работы не записана"
            else:
                what, why = "no_records", "файлы менялись, а записей нет"
        elif is_latest and last_fp and now_hash and last_fp != now_hash:
            what, why = "changes_outside", "после неё файлы менялись вне записанных сессий"
        if what:
            found.append({"gap_id": new_id("gap"), "session": sid, "agent": m.get("agent"), "what": what,
                          "why": why, "started_at": m.get("started_at")})
        m["checked"] = True
        _save_session_manifest(store, sid, m)
    return found


def gap_sentence(g):
    when = ru.parse_ts(g.get("started_at"))
    day = ru.words_on(when) if when else "в прошлой сессии"
    agent = ru.agent_name(g.get("agent"))
    if g["what"] == "changes_outside":
        return "После сессии %s (%s) файлы менялись вне записанных сессий. Могу восстановить то, что видно в изменениях файлов." % (day, agent)
    return "Сессия %s (%s) записана частично: %s. Могу восстановить то, что видно в изменениях файлов." % (day, agent, g["why"])


def report_gap_lines(st, new_gaps=()):
    """Everything unresolved stays visible to the owner (R10): new gaps as a
    full sentence, older unresolved ones as a short reminder. Showing a gap
    once does not resolve it."""
    new_ids = {g["gap_id"] for g in new_gaps}
    lines = [gap_sentence(g) for g in new_gaps]
    old = [g for g in st.open_gaps() if g["id"] not in new_ids]
    if old:
        when = ru.parse_ts(old[0].get("started_at"))
        day = ru.words_on(when) if when else "в прошлой сессии"
        lines.append("Ещё не восстановлено: сессия %s записана частично%s — «восстанови, что можно»." % (
            day, (" и ещё %d" % (len(old) - 1)) if len(old) > 1 else ""))
    return lines


def gap_unknown_lines(st):
    out = []
    for g in st.open_gaps():
        when = ru.parse_ts(g.get("started_at"))
        day = ru.words_on(when) if when else "прошлая сессия"
        if g["what"] == "changes_outside":
            out.append("После сессии %s файлы менялись вне записанных сессий — «восстанови, что можно»" % day)
        else:
            out.append("Сессия %s записана частично: %s" % (day, g["why"]))
    return out


def start_session(store, sid, agent, source, mode):
    """Shared by the SessionStart hook and the manual `start` command."""
    fp = worktree_fingerprint(store.project)
    st, problem = load_state(store)
    ctx = Context(store.project, agent, sid)
    new_gaps = [] if problem else detect_gaps(store, st, sid, fp)
    recs = []
    if source != "compact":
        recs.append(("session.start", {"session": sid, "agent": agent, "source": source, "mode": mode}, None))
    for g in new_gaps:
        recs.append(("capture.gap", g, None))
    if recs and not problem:
        write_core(store, recs, ctx, via="hook" if mode == "hooks" else "start")
        st, problem = load_state(store)
    m = _session_manifest(store, sid)
    if not m:
        m = {"id": sid, "agent": agent, "started_at": ctx.now, "fp_start": fp, "fp_last": fp,
             "stops": 0, "records_at_last_stop": 0, "dirty_turns": 0, "mode": mode}
    m["last_source"] = source
    m["checked"] = False
    _save_session_manifest(store, sid, m)
    return st, problem, new_gaps


def session_start_output(store, sid, agent, source, mode, command):
    st, problem, new_gaps = start_session(store, sid, agent, source, mode)
    p = prefs(store)
    if problem:
        text = ("Атлас: записи повреждены начиная с %s — %s. Показано состояние до этой записи; "
                "новые записи не делаются. Скажите «проверь Атлас»." % (problem["file"], problem["why"]))
        return {"report": text, "brief": text, "manifest": {}}
    gap_lines = report_gap_lines(st, new_gaps)
    brief, manifest = build_brief(st, command, sid, st.last_tx, mode, gaps=gap_unknown_lines(st))
    briefs = store.load_local(os.path.join("manifests", "briefs.json"))
    seen = {d: rev for d, rev in manifest["decisions"].items()}
    seen["_agent"] = agent
    briefs[sid] = seen
    store.save_local(os.path.join("manifests", "briefs.json"), briefs)
    mode_note = None if mode == "hooks" else "Режим: обновление по команде (у этого агента нет хуков Атласа)."
    report = views.report(st, sid, gap_lines, mode_note)
    return {"report": report, "brief": brief, "manifest": manifest, "prefs": p}


# ----------------------------------------------------------------------
def _read_input():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}


def _guard(store, via):
    """Run a hook only for connected, enabled projects and only from the
    channel that installed it (plugin vs project settings) — no double runs."""
    if not store.exists():
        return False
    try:
        cfg = store.load_config()
    except Exception:
        return False
    if not cfg.get("enabled", True):
        return False
    mode = store.load_local("install.json").get("hooks_mode")
    if via == "plugin":
        return mode in (None, "plugin")
    if via == "project":
        return mode == "project"
    return True


def run_hook(event, via, command):
    data = _read_input()
    base = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    project = find_project(base)
    if not project:
        return 0
    store = Store(project)
    if not _guard(store, via):
        return 0
    sid = data.get("session_id") or "unknown"
    try:
        if event == "session-start":
            return _hook_session_start(store, data, sid, command)
        if event == "stop":
            return _hook_stop(store, data, sid)
    except Exception as exc:  # a hook must never break the session
        sys.stderr.write("Атлас: хук %s не выполнен: %s\n" % (event, exc))
        return 0
    return 0


def _hook_session_start(store, data, sid, command):
    source = data.get("source") or "startup"
    out = session_start_output(store, sid, "claude-code", source, "hooks", command)
    p = out.get("prefs") or prefs(store)
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write("export ATLAS_SESSION_ID=%s\n" % _safe(sid))
                fh.write("export ATLAS_AGENT=claude-code\n")
        except OSError:
            pass
    show = source in ("startup", "clear", "resume") and p.get("report_at_start") != "off"
    ctx_lines = [out["brief"], ""]
    if show and p.get("report_at_start") == "system_message":
        ctx_lines.append("Владельцу при старте показан доклад Атласа (Э0) системным сообщением:")
        ctx_lines.append(out["report"])
        ctx_lines.append("Не повторяй доклад, если владелец не спросит «где мы?». Ответ «давай» относится к строке «Предлагаю».")
    elif show and p.get("report_at_start") == "agent":
        ctx_lines.append("Первым ответом покажи владельцу этот доклад дословно (не длиннее 10 строк), затем отвечай на его сообщение:")
        ctx_lines.append(out["report"])
    result = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(ctx_lines)}}
    if show and p.get("report_at_start") == "system_message":
        result["systemMessage"] = out["report"]
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


NUDGE = ("Атлас: за последние ответы файлы проекта менялись, а записей в Атласе от этой сессии нет. "
         "Если был значимый шаг (сделано, решено, проверено, отложено, не получилось) — оставь короткую запись "
         "через скилл atlas и скажи владельцу одной строкой, что записал. Если значимого шага не было — "
         "ничего не записывай и ответь одним словом «ок».")


def _hook_stop(store, data, sid):
    m = _session_manifest(store, sid)
    if not m:
        m = {"id": sid, "agent": "claude-code", "started_at": ru.now_iso(), "fp_start": None,
             "stops": 0, "records_at_last_stop": 0, "dirty_turns": 0, "mode": "hooks", "late_start": True}
    fp = worktree_fingerprint(store.project)
    st, problem = load_state(store)
    wrote = sum(1 for r in st.records if r.get("_session") == sid and r["kind"] not in ("session.start", "capture.gap"))
    m["stops"] = m.get("stops", 0) + 1
    m["last_stop_at"] = ru.now_iso()
    if m.get("fp_start") is None:
        m["fp_start"] = fp
    changed = bool(fp and (m.get("fp_last") or {}).get("hash") != fp.get("hash"))
    if wrote > m.get("records_at_last_stop", 0):
        m["dirty_turns"] = 0
    elif changed:
        m["dirty_turns"] = m.get("dirty_turns", 0) + 1
    m["records_at_last_stop"] = wrote
    m["fp_last"] = fp
    nudge = False
    limit = int(prefs(store).get("nudge_after_turns") or 0)
    if not data.get("stop_hook_active") and limit > 0 and m["dirty_turns"] >= limit and not problem:
        nudge = True
        m["dirty_turns"] = 0
    _save_session_manifest(store, sid, m)
    if nudge:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": NUDGE}},
                                    ensure_ascii=False))
    return 0
