"""Owner-facing screens in chat (stage M0), written by the core without a model.

Every string here follows §7а «Как Атлас пишет»: the point first, owner
words, name before code, words instead of icons, facts instead of grades,
three trust sources kept apart, unknowns named with a reason, one step at
the end, dates in words where people read, no blame, bounded length.
"""

from . import ru

METHOD_WORDS = {
    "automated_run": "тестом",
    "owner_manual": "вами вручную",
    "external": "независимой проверкой",
    "agent_report": "со слов агента",
}
STATUS_SHORT = {
    "passed": "проверено",
    "failed": "не работает",
    "could_not_check": "не удалось проверить",
    "applicability_unknown": "проверено для другого состояния кода",
    "agent_only": "агент сообщил, что проверил; наблюдения нет",
    "changed": "условие изменилось после предъявления",
    "none": "не проверено",
}
REJECTION_WORDS = {
    "defect": "дефект согласованного",
    "new_wish": "новое пожелание",
    "environment": "несоответствие среде",
    "check_error": "ошибка инструкции проверки",
}


class Terms:
    """§4.2: a word from the glossary gets a hint on its first appearance
    in a session; afterwards the word alone."""

    def __init__(self, seen=None):
        self.seen = set(seen or [])

    def hint(self, word, sep=" — "):
        if word in self.seen:
            return ""
        self.seen.add(word)
        return sep + ru.TERM_HINTS[word]


def ts(value):
    return ru.parse_ts(value)


def title_lc(t):
    return ru.lc_first(t["title"])


def result_name(st, r, lower=True, terms=None):
    t = st.threads[r["thread_id"]]
    name = title_lc(t) if lower else t["title"]
    hint = terms.hint("версия") if terms else ""
    return "%s (версия r%d%s)" % (name, r["version"], hint)


def decision_name(d, lower=False):
    q = d.get("question") or d.get("text") or ""
    if lower:
        q = ru.lc_first(q)
    word = "вопрос" if d["status"] == "proposed" else "решение"
    return "%s (%s %d)" % (q, word, d["number"])


def align(rows, indent=""):
    """rows: [(label, [lines])] → aligned text lines."""
    width = max(len(label) for label, _ in rows) + 1
    out = []
    for label, lines in rows:
        if isinstance(lines, str):
            lines = [lines]
        for i, line in enumerate(lines):
            head = (label + ":").ljust(width) if i == 0 else " " * width
            out.append(indent + head + " " + line)
    return out


# ----------------------------------------------------------------------
# trust
def acceptance_summary(st, r):
    rows = st.result_trust(r)
    accs = [row["acceptance"] for row in rows]
    if not accs:
        return "не принято"
    rejected = [a for a in accs if a and a["outcome"] == "rejected"]
    if rejected:
        rj = rejected[-1].get("rejection") or {}
        return "не принято: %s" % REJECTION_WORDS.get(rj.get("class"), "причина не указана")
    done = [a for a in accs if a]
    if not done:
        return "не принято"
    purposes = sorted({a.get("purpose") for a in done if a.get("purpose")})
    purpose = (" для " + ", ".join(purposes)) if purposes else ""
    caveat = any(a["outcome"] == "accepted_with_caveat" for a in done)
    if len(done) < len(accs):
        return "принято частично (%d из %d)%s" % (len(done), len(accs), purpose)
    return "принято%s%s" % (purpose, " с оговоркой" if caveat else "")


def method_phrase(rows):
    methods = [r["check"] for r in rows if r["check"]["status"] == "passed"]
    if not methods:
        return ""
    kinds = {m["method"] for m in methods}
    envs = {m.get("environment") for m in methods if m.get("environment")}
    if len(kinds) != 1:
        return ""
    phrase = METHOD_WORDS.get(next(iter(kinds)), "")
    if len(envs) == 1:
        phrase += " " + next(iter(envs))
    return phrase


def trust_line_result(st, r, capital=False):
    rows = st.result_trust(r)
    passed = sum(1 for row in rows if row["check"]["status"] == "passed")
    checked = "проверено %d из %d" % (passed, len(rows))
    how = method_phrase(rows)
    if how:
        checked += ", " + how
    line = "сказано агентом · %s · %s" % (checked, acceptance_summary(st, r))
    return ru.capital(line) if capital else line


def trust_line_thread(st, t):
    r = st.latest_result(t["id"])
    crits = st.thread_criteria(t)
    if not r:
        if not crits:
            return "результата пока нет; условия готовности не заданы"
        return "результата пока нет; условий готовности: %d" % len(crits)
    rows = st.result_trust(r)
    passed = sum(1 for row in rows if row["check"]["status"] == "passed")
    total = max(len(crits), len(rows))
    return "сказано · проверено %d из %d · %s" % (passed, total, acceptance_summary(st, r))


def criterion_status(st, t, c, latest):
    """One line of status for a criterion on the topic page (Э2)."""
    if latest:
        entry = next((e for e in latest["criteria"] if e["id"] == c["id"]), None)
        if entry:
            chk = st.criterion_check(latest, entry)
            s = chk["status"]
            if s == "passed":
                env = (" " + chk["environment"]) if chk.get("environment") else ""
                return "проверено %s%s (версия r%d)" % (METHOD_WORDS.get(chk["method"], ""), env, latest["version"])
            if s == "failed":
                return "не работает: проверка %s (версия r%d)" % (ru.short_date(ts(chk["at"])), latest["version"])
            if s == "none":
                pending = [d for d in st.open_decisions() if d.get("criterion_id") == c["id"]]
                if pending:
                    return "ждёт вашего ответа (вопрос %d)" % pending[0]["number"]
                return "не проверено"
            return STATUS_SHORT[s]
    pending = [d for d in st.open_decisions() if d.get("criterion_id") == c["id"]]
    if pending:
        return "ждёт вашего ответа (вопрос %d)" % pending[0]["number"]
    tries = [a for a in st.attempts.values() if a.get("criterion_id") == c["id"] and not a.get("error")]
    failed = [a for a in tries if a["outcome"] == "failed"]
    if failed:
        dates = sorted({ru.short_date(ts(a["created_at"])) for a in failed})
        return "не проверено: %s %s, не вышло" % (
            ru.count_words(len(failed), "попытка", "попытки", "попыток"), ", ".join(dates))
    return "не проверено"


# ----------------------------------------------------------------------
# next step (Appendix D, simplified for M0)
def next_step(st):
    bm = st.last_bookmark()
    if bm and bm.get("next"):
        n = bm["next"]
        valid = True
        ref = n.get("ref")
        if n["kind"] == "check_result":
            r = st.results.get(ref)
            valid = bool(r) and st.result_awaiting(r) and st.latest_result(r["thread_id"])["id"] == r["id"]
        elif n["kind"] == "answer_decision":
            d = st.decisions.get(ref)
            valid = bool(d) and d["status"] == "proposed" and not d.get("error")
        elif n["kind"] == "continue_thread":
            t = st.threads.get(ref)
            valid = bool(t) and st.view_state(t) == "active" and not t.get("error")
        if valid:
            return {"text": n["text"], "why": n.get("why"), "source": "bookmark"}
    for item in st.needs():
        if item["type"] == "result":
            r = item["obj"]
            dur = ((r.get("check") or {}).get("duration")) or "время неизвестно"
            has_steps = bool((r.get("check") or {}).get("steps"))
            return {"text": "проверить %s самому, %s%s" % (
                result_name(st, r), dur, "; инструкция готова" if has_steps else "; инструкцию проверки агент ещё не написал"),
                "why": None, "source": "needs", "kind": "check_result"}
        if item["type"] == "decision" and item["prio"] <= 2:
            d = item["obj"]
            return {"text": "ответить: %s" % decision_name(d), "why": None, "source": "needs"}
    active = [t for t in st.live_threads() if st.view_state(t) == "active"]
    if active:
        t = sorted(active, key=lambda t: t.get("progress_at") or "")[-1]
        return {"text": "продолжить «%s»" % t["title"], "why": None, "source": "threads"}
    planned = [t for t in st.live_threads() if t["state"] == "planned"]
    if planned:
        return {"text": "взять в работу «%s» из следующего спринта" % planned[0]["title"], "why": None, "source": "threads"}
    return {"text": "начать с одной темы — назовите её", "why": None, "source": "empty"}


def need_short(st, item):
    if item["type"] == "result":
        return "проверить и принять %s" % result_name(st, item["obj"])
    if item["type"] == "decision":
        d = item["obj"]
        return decision_name(d, lower=True) + (" — отложен" if d.get("deferred") else "")
    return "подтвердить правило «%s»" % item["obj"]["text"]


def is_empty(st):
    return not (st.threads or st.parking or st.decisions or st.goals or st.bookmarks)


# ----------------------------------------------------------------------
# Э0 — доклад при входе
def report(st, current_session=None, gap_lines=None, mode_note=None):
    name = st.project_name or "Проект"
    gap_lines = [g for g in (gap_lines or []) if g]
    if len(gap_lines) > 1:
        # One line about incomplete capture keeps the report within 10 lines.
        gap_lines = [gap_lines[0] + " Ещё %d — в «покажи всё»." % (len(gap_lines) - 1)]
    if is_empty(st):
        lines = []
        if gap_lines:
            lines += gap_lines
        lines.append("Атлас ещё ничего не знает об этом проекте. Начнём с одной темы?")
        lines.append("Скажите, какая тема сейчас самая неясная, или «разбери последнюю сессию».")
        return "\n".join(lines)
    lines = list(gap_lines or [])
    bm = st.last_bookmark()
    if bm:
        lines.append("%s · закладка %s" % (name, ru.words_from(ts(bm["created_at"]))))
    else:
        lines.append("%s · закладки пока нет" % name)
    g = st.goal()
    rows = []
    lines.append("Цель: %s" % (g["text"] if g else "не записана"))
    if bm:
        rows.append(("Где остановились", bm["stopped_at"]))
        others = st.sessions_after(bm["created_tx"], exclude_session=current_session)
        if not others:
            rows.append(("После закладки", "других сессий не было."))
        else:
            parts = []
            for sid, info in list(others.items())[:2]:
                done = [r["data"].get("text") for r in info["records"] if r["kind"] == "note.progress"]
                agent = ru.agent_name(info["agent"])
                if done:
                    parts.append("в другой сессии (%s) сделано: %s" % (agent, "; ".join(done[:2])))
                else:
                    parts.append("в другой сессии (%s) %s" % (
                        agent, ru.count_words(len(info["records"]), "запись", "записи", "записей")))
            rows.append(("После закладки", "; ".join(parts) + "."))
    else:
        t = _last_touched_thread(st)
        if t:
            last = _last_progress(st, t)
            rows.append(("Где остановились", "%s%s (собрано автоматически из записей)" % (
                t["title"], (" — " + last) if last else "")))
    needs = st.needs()
    if needs:
        shown = [need_short(st, n) for n in needs[:2]]
        if len(needs) > 2:
            shown[-1] += "; и ещё %d — «что от меня нужно?»" % (len(needs) - 2)
        shown = [s + (";" if i < len(shown) - 1 else "") for i, s in enumerate(shown)]
        rows.append(("Нужно от вас", shown))
    else:
        rows.append(("Нужно от вас", "ничего."))
    lines += align(rows)
    lines.append("")
    step = next_step(st)
    lines.append("Предлагаю: %s.%s" % (step["text"].rstrip("."), (" " + step["why"].rstrip(".") + ".") if step.get("why") else ""))
    if mode_note:
        lines.append(mode_note)
    lines.append("Скажите «давай», «другое» или «покажи всё».")
    return "\n".join(lines)


def _last_touched_thread(st):
    ts_ = [t for t in st.live_threads() if t["state"] not in ("released", "candidate")]
    if not ts_:
        return None
    return sorted(ts_, key=lambda t: t.get("last_tx", 0))[-1]


def _last_progress(st, t):
    notes = [n for n in st.notes.values()
             if n.get("thread_id") == t["id"] and n["kind"] == "progress" and not n.get("error")]
    if not notes:
        return None
    return sorted(notes, key=lambda n: n["created_tx"])[-1]["text"]


# ----------------------------------------------------------------------
# Э3 — «Нужно от вас»
def delivery_line(st, d, briefs):
    receivers = set()
    if d.get("decided_agent"):
        receivers.add(ru.agent_name(d["decided_agent"]))
    for sid, seen in (briefs or {}).items():
        if seen.get(d["id"]) == d["rev"]:
            receivers.add(ru.agent_name(seen.get("_agent")))
    for sid, ack in d.get("acks", {}).items():
        receivers.add(ru.agent_name(ack.get("agent")))
    parts = ["записано"]
    if receivers:
        parts.append(", ".join(sorted(receivers)) + " получил")
    else:
        parts.append("рабочая сессия ещё не получила")
    if d.get("applied"):
        parts.append("учтено в работе (со слов агента)")
    else:
        parts.append("учёт в работе не подтверждён")
    return " · ".join(parts)


def needs_view(st, briefs=None, frames_active=False, terms=None):
    items = st.needs()
    lines = []
    if not items:
        lines.append("Сейчас от вас ничего не нужно." + (" Агент работает в рамках договорённости." if frames_active else ""))
    else:
        lines.append("НУЖНО ОТ ВАС (%d)" % len(items))
        answer_hint = None
        for item in items:
            lines.append("")
            if item["type"] == "result":
                r = item["obj"]
                dur = (r.get("check") or {}).get("duration")
                lines.append("• Проверить и принять %s%s" % (result_name(st, r, terms=terms), (", " + dur) if dur else ""))
                lines.append("  → карточка результата: скажите «покажи, что готово»")
            elif item["type"] == "rule":
                lines.append("• Подтвердить правило для договорённости: «%s»" % item["obj"]["text"])
                lines.append("  Пока вы не подтвердили, правило не действует.")
            else:
                d = item["obj"]
                lines.append("• %s" % decision_name(d))
                if d.get("risk", "normal") != "normal":
                    lines.append("  Затронуто: %s — нужно ваше явное «да»; молчание согласием не станет." % _risk(d))
                elif d.get("deferred"):
                    lines.append("  отложено вами; остаётся здесь, пока не решите")
                elif d.get("can_defer", True) and d.get("urgency") == "normal":
                    lines.append("  не срочно, можно отложить")
                elif d.get("urgency") == "blocking":
                    lines.append("  без ответа стоит выбранная работа")
                if d.get("deadline"):
                    lines.append("  срок: %s" % d["deadline"])
                for o in d.get("options", []):
                    lines.append("  %s%s" % (ru.capital(o["label"]), (" — " + o["consequence"]) if o.get("consequence") else ""))
                adv = d.get("advice")
                if adv:
                    what = adv.get("option") or adv.get("text")
                    text = "Агент советует: %s" % what
                    if adv.get("text") and adv.get("option"):
                        text += " — " + adv["text"]
                    text += "; совет перестанет быть верным, %s" % adv["wrong_if"]
                    lines.append("  " + text.rstrip(".") + ".")
                prov = d.get("provisional")
                if prov:
                    lines.append("  Пока агент работает с вариантом «%s» — временно, до вашего ответа." % (
                        prov.get("option") or prov.get("text")))
                if answer_hint is None:
                    answer_hint = _answer_phrases(d)
        if answer_hint:
            lines.append("")
            lines.append("Ответить: " + answer_hint)
    recent = _recent_answers(st)
    if recent:
        lines.append("")
        lines.append("НЕДАВНИЕ ОТВЕТЫ")
        for d in recent:
            lines.append("  %s — %s" % (d["text"], delivery_line(st, d, briefs)))
    return "\n".join(lines)


def _risk(d):
    return {"money": "деньги", "publish": "публикация", "real_data": "настоящие данные",
            "external": "внешние обязательства", "irreversible": "трудно обратимое изменение",
            "unknown": "риск неизвестен"}.get(d.get("risk"), d.get("risk"))


def _answer_phrases(d):
    phrases = []
    opts = d.get("options", [])
    letters = "АБВГ"
    for i, o in enumerate(opts):
        p = "«%s»" % ru.lc_first(o["label"])
        if i == 0:
            p += " (то же, что «вопрос %d — %s»)" % (d["number"], letters[0])
        phrases.append(p)
    if d.get("risk", "normal") == "normal":
        phrases.append("«реши сам»")
    phrases.append("«нужно больше данных»")
    if d.get("can_defer", True):
        phrases.append("«позже»")
    return ", ".join(phrases)


def _recent_answers(st):
    bm = st.last_bookmark()
    since = bm["created_tx"] if bm else 0
    out = [d for d in st.decided() if d.get("last_tx", 0) > since and d.get("decided_source")]
    return sorted(out, key=lambda d: d["last_tx"])[-3:]


# ----------------------------------------------------------------------
# Э4 — карточка результата
def result_card(st, r, current_fp=None, terms=None):
    t = st.threads[r["thread_id"]]
    lines = []
    when = ru.words_on(ts(r["created_at"]))
    hint = terms.hint("версия", sep=" — ") if terms else ""
    lines.append("РЕЗУЛЬТАТ · %s (версия r%d%s; предъявлен %s)" % (t["title"], r["version"], hint, when))
    latest = st.latest_result(t["id"])
    if latest and latest["id"] != r["id"]:
        lines.append("Сейчас уже версия r%d — проверка и приёмка привязаны к номеру версии." % latest["version"])
    rfp = (r.get("fingerprint") or {}).get("hash")
    if current_fp and rfp and current_fp.get("hash") != rfp:
        lines.append("Код менялся после предъявления — проверять будете текущее состояние; "
                     "к версии r%d проверка может не относиться." % r["version"])
    lines.append("Что сделано: %s" % r["summary"])
    if r.get("excluded"):
        lines.append("Не входит:   %s." % ", ".join(r["excluded"]).rstrip("."))
    lines.append(trust_line_result(st, r, capital=True))
    fit = r.get("fit_for")
    not_fit = r.get("not_fit_for")
    if fit or not_fit:
        text = "Годится для %s" % fit if fit else ""
        if not_fit:
            text += ("; для %s — нет" if text else "Не годится для %s") % not_fit
        lines.append(text.rstrip(".") + ".")
    caveats = list(r.get("caveats") or [])
    for d in st.open_decisions():
        if d.get("thread_id") == t["id"] and d.get("provisional"):
            caveats.append("%s — временный выбор агента, ждёт вашего ответа (вопрос %d)" % (
                ru.lc_first(d["question"].rstrip("?")), d["number"]))
    for row in st.result_trust(r):
        acc = row["acceptance"]
        if acc and acc["outcome"] == "accepted_with_caveat" and acc.get("caveat"):
            caveats.append("принято с оговоркой: %s" % acc["caveat"])
    if caveats:
        lines.append("Оговорки: %s." % "; ".join(c.rstrip(".") for c in caveats))
    chk = r.get("check") or {}
    steps = chk.get("steps") or []
    all_done = all(row["acceptance"] for row in st.result_trust(r))
    if all_done:
        lines.append("")
        lines.append("Если что-то перестанет работать — скажите «не работает: …».")
        return "\n".join(lines)
    lines.append("")
    if not steps:
        lines.append("КАК ПРОВЕРИТЬ САМОМУ: инструкции пока нет — агент её не записал.")
        lines.append("Скажите «напиши инструкцию проверки» или «принимаю без проверки».")
        return "\n".join(lines)
    lines.append("КАК ПРОВЕРИТЬ САМОМУ%s" % ((" (%s)" % chk["duration"]) if chk.get("duration") else ""))
    n = 0
    if chk.get("run"):
        lines.append("  0. %s" % chk["run"])
    for s in steps:
        n += 1
        lines.append("  %d. %s" % (n, s))
    tail = []
    if chk.get("expect"):
        tail.append("Должно быть: %s." % chk["expect"].rstrip("."))
    if chk.get("failure"):
        tail.append("Провал: %s." % chk["failure"].rstrip("."))
    if tail:
        lines.append("  %d. %s" % (n + 1, " ".join(tail)))
    accept_phrase = "«принимаю для показа»" if "показ" in (fit or "") else "«принимаю»"
    lines.append("Скажите «проверил, работает», «не работает: …» или %s." % accept_phrase)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Э2 — страница темы (кратко, в чате)
def topic_view(st, t, full=False, terms=None):
    lines = []
    vstate = st.view_state(t)
    latest = st.latest_result(t["id"])
    head = ru.THREAD_STATE[vstate]
    if vstate == "presented" and latest:
        head += " (версия r%d)" % latest["version"]
    head += " · начата %s" % ru.short_date(ts(t["created_at"]))
    lines.append("%s      %s" % (t["title"].upper(), head))
    if t.get("why"):
        lines.append("Зачем: %s" % t["why"])
    if t.get("state_reason") and t["state"] in ("paused", "released", "frozen"):
        lines.append("Причина: %s" % t["state_reason"])
    lines.append("")
    lines.append("ГОТОВО ЛИ?  %s" % trust_line_thread(st, t))
    if latest:
        said = latest.get("claim") or latest["summary"]
        lines.append("  Сказано агентом:  %s — «%s»" % (ru.short_date(ts(latest["created_at"])), said))
        passed = [row for row in st.result_trust(latest) if row["check"]["status"] == "passed"]
        if passed:
            for i, row in enumerate(passed):
                label = "  Проверено:        " if i == 0 else "                    "
                chk = row["check"]
                lines.append("%s%s — %s%s, %s, версия r%d" % (
                    label, ru.lc_first(row["criterion"]["text"]), METHOD_WORDS.get(chk["method"], ""),
                    (" " + chk["environment"]) if chk.get("environment") else "",
                    ru.short_date(ts(chk["at"])), latest["version"]))
        else:
            lines.append("  Проверено:        нет")
        acc = acceptance_summary(st, latest)
        lines.append("  Принято вами:     %s" % ("нет" if acc == "не принято" else "%s (версия r%d)" % (acc, latest["version"])))
    crits = st.thread_criteria(t)
    if crits:
        lines.append("")
        lines.append("УСЛОВИЯ ГОТОВНОСТИ%s" % ((" (%s)" % terms.hint("условие готовности", sep="").strip()) if terms and "условие готовности" not in terms.seen else ""))
        for c in crits:
            lines.append("  %s — %s" % (c["text"], criterion_status(st, t, c, latest)))
    events = history_lines(st, t)
    if events:
        lines.append("")
        lines.append("ИСТОРИЯ (по сессиям, не по чатам)")
        shown = events if full else events[-10:]
        if len(events) > len(shown):
            lines.append("  … ещё %d раньше — «покажи всю историю темы»" % (len(events) - len(shown)))
        lines += ["  " + e for e in shown]
    decs = st.thread_decisions(t["id"])
    decided = [d for d in decs if d["status"] == "decided"]
    if decided:
        lines.append("")
        lines.append("РЕШЕНИЯ, КОТОРЫЕ КАСАЮТСЯ ТЕМЫ")
        for d in decided:
            who = "ваше решение" if (d.get("decided_source") or {}).get("type") == "owner_message" else \
                "решил агент по вашему «реши сам»" if d.get("delegated") else "записано агентом"
            lines.append("  %s (%s, %s)" % (d["text"], ru.short_date(ts(d["decided_at"])), who))
    learned = [n for n in st.notes.values() if n.get("thread_id") == t["id"] and n["kind"] == "learned" and not n.get("error")]
    if learned:
        lines.append("")
        lines.append("УЗНАЛИ")
        for n in learned[-5:]:
            lines.append("  %s (%s)" % (n["text"], ru.short_date(ts(n["created_at"]))))
    debts = [n for n in st.notes.values() if n.get("thread_id") == t["id"] and n["kind"] == "debt"
             and not n.get("error") and not n.get("resolved")]
    if debts:
        lines.append("")
        lines.append("КОСТЫЛИ, КОТОРЫЕ ОСТАВИЛ АГЕНТ")
        for n in debts:
            lines.append("  %s" % n["text"])
    lines.append("")
    if vstate == "presented" and latest:
        dur = (latest.get("check") or {}).get("duration")
        lines.append("ДАЛЬШЕ   Проверить %s самому%s → «покажи, что готово»" % (
            title_lc(t), (", " + dur) if dur else ""))
    elif vstate == "active":
        lines.append("ДАЛЬШЕ   Продолжить работу; где остановились — в истории выше.")
    elif vstate == "planned":
        lines.append("ДАЛЬШЕ   Взять в работу — скажите «берём %s»." % title_lc(t))
    elif vstate == "candidate":
        lines.append("ДАЛЬШЕ   Это ваша тема? Скажите «да, это тема» или «это не тема».")
    else:
        lines.append("ДАЛЬШЕ   Вернуть можно фразой «возвращаем %s» — история сохранена." % title_lc(t))
    return "\n".join(lines)


def history_lines(st, t):
    out = []
    for rec in st.thread_events(t["id"]):
        d = rec.get("data", {})
        when = ru.short_date(ts(rec["_at"]))
        agent = ru.agent_name(rec["_agent"])
        text = None
        k = rec["kind"]
        recovered = " (восстановлено задним числом)" if d.get("recovered") else ""
        if k == "note.progress":
            text = d["text"] + recovered
        elif k == "thread.create":
            text = "тема записана" + (" — " + ru.THREAD_STATE[d["state"]] if d.get("state") else "")
        elif k == "thread.state":
            text = "%s%s" % (ru.THREAD_STATE[d["state"]], (" — " + d["reason"]) if d.get("reason") else "")
        elif k == "result.present":
            text = "предъявлен результат (версия r%d)" % d["version"]
        elif k == "verify":
            r = st.results.get(d.get("result_id"))
            v = "r%d" % r["version"] if r else "?"
            word = {"passed": "проверено", "failed": "проверка: не работает",
                    "could_not_check": "проверить не удалось"}[d["outcome"]]
            text = "%s %s (версия %s)" % (word, METHOD_WORDS.get(d["method"], ""), v)
        elif k == "accept":
            r = st.results.get(d.get("result_id"))
            v = "r%d" % r["version"] if r else "?"
            if d["outcome"] == "rejected":
                text = "вы не приняли версию %s: %s" % (v, REJECTION_WORDS.get((d.get("rejection") or {}).get("class"), ""))
            else:
                text = "вы приняли версию %s для: %s" % (v, d.get("purpose") or "—")
        elif k == "attempt":
            words = {"failed": "не вышло", "partial": "частично", "succeeded": "получилось"}
            text = "попытка: %s — %s%s" % (d["approach"], words[d["outcome"]],
                                           "" if d.get("new_approach", True) else " (подход повторяет прежний)")
        elif k == "frame.agree":
            text = "договорённость: %s" % d["doing"]
        elif k == "note.debt":
            text = "оставлен костыль: %s" % d["text"]
        if text:
            out.append("%s %s   %s" % (when, agent.ljust(11), text))
    for g in st.gaps.values():
        if g.get("error"):
            continue
        touched = any(r["_session"] == g.get("session") and (r.get("data") or {}).get("thread_id") == t["id"]
                      for r in st.records)
        if touched:
            out.append("%s %s   записано частично — %s" % (
                ru.short_date(ts(g.get("started_at") or g["created_at"])), ru.agent_name(g.get("agent")).ljust(11),
                "восстановлено" if g["resolved"] else g.get("why", "итога нет")))
    return out


# ----------------------------------------------------------------------
# Э7 — закладка
def bookmark_view(st, bm, ask_mood=True):
    lines = []
    when = ts(bm["created_at"])
    lines.append("Закладка сохранена, запись подтверждена. %s." % ru.capital(ru.words_full(when, relative=False)))
    lines.append("")
    rows = []
    if bm.get("today"):
        rows.append(("Сегодня", "; ".join(bm["today"]) + "."))
    else:
        rows.append(("Сегодня", "новых проверенных результатов нет."))
    if bm.get("learned"):
        rows.append(("Узнали", "; ".join(bm["learned"]) + "."))
    if bm.get("failed"):
        rows.append(("Не получилось", "; ".join(bm["failed"]) + "."))
    if bm.get("started"):
        rows.append(("Начали", "; ".join(bm["started"]) + "."))
    if not bm.get("today"):
        rows.append(("Остановились", bm["stopped_at"]))
    needs = st.needs()
    if needs:
        shown = [need_short(st, n) for n in needs[:2]]
        shown = [s + (";" if i < len(shown) - 1 else "") for i, s in enumerate(shown)]
        rows.append(("Ждёт вас", shown))
    step = next_step(st)
    rows.append(("Дальше", step["text"].rstrip(".") + "."))
    lines += align(rows)
    if ask_mood:
        lines.append("")
        lines.append("Как прошло? тяжело · нормально · в кайф · пропустить")
    return "\n".join(lines)


def bookmark_auto_view(st):
    """No bookmark after the last work: assemble from records (Э7, «собрано автоматически»)."""
    t = _last_touched_thread(st)
    if not t:
        return None
    last = _last_progress(st, t)
    return "%s%s" % (t["title"], (" — " + last) if last else "")


# ----------------------------------------------------------------------
# Э8 — парковка (порция в чате)
def parking_view(st, portion=7, show_all=False):
    items = st.parked()
    if not items:
        return "Парковка пуста. Идеи на потом можно записывать фразой «в сторону: …»."
    ideas = sum(1 for p in items if p["kind"] == "idea")
    questions = len(items) - ideas
    stale_days = int(st.settings.get("stale_parking_days", 14))
    today = ru.now()
    groups = {"related": [], "unrelated": [], "unknown": [], "old": []}
    for p in items:
        age = (today - ts(p["created_at"])).days
        if age >= stale_days and not p.get("goal_related"):
            groups["old"].append(p)
        elif p.get("goal_related") is True:
            groups["related"].append(p)
        elif p.get("goal_related") is False:
            groups["unrelated"].append(p)
        else:
            groups["unknown"].append(p)
    ordered = groups["related"] + groups["unrelated"] + groups["unknown"] + groups["old"]
    limit = len(ordered) if show_all else portion
    shown = ordered[:limit]
    head = "ПАРКОВКА · %s" % ru.count_words(len(items), "запись", "записи", "записей")
    if ideas and questions:
        head += " (%s и %s)" % (ru.count_words(ideas, "идея", "идеи", "идей"),
                                ru.count_words(questions, "вопрос", "вопроса", "вопросов"))
    if len(shown) == len(items):
        head += " · показаны все" if len(items) > 1 else ""
    else:
        head += " · показаны первые %d" % len(shown)
    head += " · порция до %d" % portion
    lines = [head, ""]
    titles = {
        "related": "Связаны с целью (агент так думает — проверьте):",
        "unrelated": "Не связаны с целью:",
        "unknown": "Связь с целью не оценивалась:",
        "old": "Лежат давно:",
    }
    n = 0
    shown_ids = {p["id"] for p in shown}
    for key in ("related", "unrelated", "unknown", "old"):
        grp = [p for p in groups[key] if p["id"] in shown_ids]
        if not grp:
            continue
        lines.append(titles[key])
        for p in grp:
            n += 1
            when = ru.short_date(ts(p["created_at"]))
            ctx = p.get("context") or ""
            for lead in ("сказано ", "сказала ", "сказал "):
                if ctx.lower().startswith(lead):
                    ctx = ctx[len(lead):]
            ctx = (" " + ctx) if ctx else ""
            if p["kind"] == "question":
                extra = " — записано как вопрос (%s)%s" % (when, (": «%s»" % p["quote"]) if p.get("quote") else "")
            else:
                extra = " — сказано %s%s" % (when, ctx)
            lines.append("  %d. %s%s" % (n, ru.capital(p["text"]), extra))
    if len(items) > len(shown):
        lines.append("  … ещё %d — не пропадут; «покажи всю парковку»" % (len(items) - len(shown)))
    lines.append("")
    lines.append("Скажите, например: «первое и второе в следующий спринт, третье забрось, остальное оставь».")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# «покажи всё» — главная в чате (Э1 без HTML; страницы — этап M1)
def overview(st, gaps_text=None, snapshot=None):
    lines = [st.project_name or "Проект"]
    if snapshot is not None:
        lines.append("По записям на %s (снимок %d)" % (ru.words_full(ru.now()), snapshot))
    g = st.goal()
    lines.append("")
    lines.append("ЦЕЛЬ  %s" % (g["text"] if g else "не записана"))
    needs = st.needs()
    lines.append("")
    if needs:
        lines.append("НУЖНО ОТ ВАС (%d)" % len(needs))
        for n in needs[:5]:
            lines.append("  " + ru.capital(need_short(st, n)))
    else:
        lines.append("НУЖНО ОТ ВАС  ничего")
    step = next_step(st)
    lines.append("")
    lines.append("ДАЛЬШЕ   %s" % ru.capital(step["text"]))
    threads = st.live_threads()
    goal_crits = [c for t in threads if t.get("leads_to_goal") for c in st.thread_criteria(t)]
    head = "ТЕМЫ"
    if goal_crits:
        passed = accepted = 0
        for t in threads:
            if not t.get("leads_to_goal"):
                continue
            r = st.latest_result(t["id"])
            if not r:
                continue
            for row in st.result_trust(r):
                if row["check"]["status"] == "passed":
                    passed += 1
                if row["acceptance"] and row["acceptance"]["outcome"] != "rejected":
                    accepted += 1
        head += " · условия цели: проверено %d из %d · принято %d" % (passed, len(goal_crits), accepted)
    lines.append("")
    lines.append(head)
    order = ("presented", "active", "planned", "paused", "frozen", "candidate", "released")
    limit = int(st.settings.get("wip_limit", 3))
    for state in order:
        group = [t for t in threads if st.view_state(t) == state]
        if not group:
            continue
        label = ru.capital(ru.THREAD_STATE[state])
        if state == "active":
            label += " (%d из %d)" % (len(group), limit)
        else:
            label += " (%d)" % len(group)
        if state == "released":
            lines.append("%s   «покажи отпущенные»" % label)
            continue
        lines.append(label)
        for t in group:
            lines.append("  %s   %s" % (t["title"], trust_line_thread(st, t)))
            last = _last_progress(st, t)
            if state == "paused" and t.get("state_reason"):
                lines.append("    %s" % t["state_reason"])
            elif last:
                lines.append("    последнее: %s" % last)
    parked = st.parked()
    lines.append("")
    lines.append("ПАРКОВКА (%s)   «давай разберём»" % ru.count_words(len(parked), "запись", "записи", "записей"))
    unknown = list(gaps_text or [])
    unknown.append("Карту с кодом не сверяли — сверка появится на этапе M1; блок «противоречит решениям» пока не строится")
    lines.append("")
    lines.append("ЧЕГО АТЛАС НЕ ЗНАЕТ")
    for u in unknown:
        lines.append("  " + u)
    return "\n".join(lines)


def decisions_view(st, include_closed=False):
    decs = sorted([d for d in st.decisions.values() if not d.get("error")], key=lambda d: d["number"])
    lines = []
    active = [d for d in decs if d["status"] == "decided"]
    if active:
        lines.append("ДЕЙСТВУЮЩИЕ РЕШЕНИЯ")
        for d in active:
            src = d.get("decided_source") or {}
            who = "ваше" if src.get("type") == "owner_message" else ("агент по «реши сам»" if d.get("delegated") else "агент")
            lines.append("  %s (решение %d, %s, %s)" % (d["text"], d["number"], ru.short_date(ts(d["decided_at"])), who))
    open_ = [d for d in decs if d["status"] == "proposed"]
    if open_:
        lines.append("ОТКРЫТЫЕ ВОПРОСЫ")
        for d in open_:
            lines.append("  %s" % decision_name(d))
    if include_closed:
        closed = [d for d in decs if d["status"] in ("superseded", "withdrawn")]
        if closed:
            lines.append("ЗАМЕНЁННЫЕ И СНЯТЫЕ")
            for d in closed:
                lines.append("  %s (%s %d: %s)" % (d.get("text") or d.get("question"), "решение", d["number"],
                                                   "заменено" if d["status"] == "superseded" else "снято"))
    if not lines:
        return "Решений в записях нет."
    return "\n".join(lines)
