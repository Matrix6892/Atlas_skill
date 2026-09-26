"""The single write point (spec E.1–E.2): validate a receipt, apply it.

A receipt is JSON from the agent:

    {"key": "...", "base_snapshot": 41, "all_or_nothing": false,
     "ops": [{"op": "thread.create", "ref": "t", ...}, ...]}

* Each op is an observation (a fact: «сделана кнопка», «тест прошёл») or a
  command (a change: «тему — на паузу»). Observations survive a conflicting
  command from the same receipt; commands need valid preconditions (E.1).
* Ops sharing a `group` are applied all-or-nothing; ungrouped ops stand
  alone. `all_or_nothing: true` makes the whole receipt one group.
* A repeated `key` with the same content returns the earlier confirmation;
  the same key with different content is a protocol conflict (E.2, A60).
* Owner consent is never inferred: ops that need it require `source.quote`
  with the owner's words (rule 1, B.4). This is protocol protection, not
  proof of identity (B.4, last paragraph).
"""

import re

from . import ru
from .state import State, WORK_STATES
from .store import AtlasError, canonical_json, new_id, sha256_text, worktree_fingerprint


class OpError(Exception):
    pass


OBSERVATIONS = {
    "note.progress", "note.learned", "note.debt", "attempt", "park.add", "park.classify",
    "verify", "capture.recovered", "decision.ack", "decision.applied",
}

RISKS = ("normal", "money", "publish", "real_data", "external", "irreversible", "unknown")
RISK_WORDS = {
    "money": "деньги", "publish": "публикация", "real_data": "настоящие данные",
    "external": "внешние обязательства", "irreversible": "трудно обратимое изменение",
    "unknown": "риск неизвестен",
}
REJECTION_CLASSES = {
    "defect": "дефект согласованного",
    "new_wish": "новое пожелание",
    "environment": "несоответствие среде",
    "check_error": "ошибка инструкции проверки",
}
RECOVERED_FROM = ("git", "transcript", "chat")

SECRET_PATTERNS = [
    re.compile(r"sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|пароль|secret|api[_-]?key|token)\b\s*[:=]\s*\S{6,}"),
]


def looks_secret(value):
    if isinstance(value, str):
        return any(p.search(value) for p in SECRET_PATTERNS)
    if isinstance(value, dict):
        return any(looks_secret(v) for v in value.values())
    if isinstance(value, list):
        return any(looks_secret(v) for v in value)
    return False


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е")).strip(" .!?")


# ----------------------------------------------------------------------
class Context:
    def __init__(self, project, agent, session, base_snapshot=None):
        self.project = project
        self.agent = agent or "agent"
        self.session = session
        self.base_snapshot = base_snapshot
        self.now = ru.now_iso()
        self._fp = False

    def fingerprint(self):
        if self._fp is False:
            self._fp = worktree_fingerprint(self.project)
        return self._fp


class Applier:
    """Validates ops against a working copy of the state."""

    def __init__(self, st, ctx, tx):
        self.st = st
        self.ctx = ctx
        self.tx = tx
        self.refs = {}

    # ----- field helpers ------------------------------------------------
    @staticmethod
    def text(op, key, required=False, maxlen=2000):
        v = op.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            if required:
                raise OpError("не хватает поля «%s»" % key)
            return None
        if not isinstance(v, str):
            raise OpError("поле «%s» должно быть текстом" % key)
        v = v.strip()
        if len(v) > maxlen:
            raise OpError("поле «%s» длиннее %d знаков — сократите" % (key, maxlen))
        return v

    @staticmethod
    def texts(op, key, maxitems=20, required=False):
        v = op.get(key)
        if v is None:
            if required:
                raise OpError("не хватает поля «%s»" % key)
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not all(isinstance(i, str) for i in v):
            raise OpError("поле «%s» должно быть списком строк" % key)
        v = [i.strip() for i in v if i.strip()]
        if len(v) > maxitems:
            raise OpError("в поле «%s» больше %d пунктов" % (key, maxitems))
        if required and not v:
            raise OpError("поле «%s» пустое" % key)
        return v

    @staticmethod
    def flag(op, key, default=None):
        v = op.get(key, default)
        if v is not None and not isinstance(v, bool):
            raise OpError("поле «%s» должно быть true или false" % key)
        return v

    @staticmethod
    def choice(op, key, options, default=None):
        v = op.get(key, default)
        if v not in options:
            raise OpError("поле «%s»: допустимо %s" % (key, ", ".join(str(o) for o in options)))
        return v

    def source(self, op, required, what):
        src = op.get("source")
        if isinstance(src, str):
            src = {"quote": src}
        if src is not None and not isinstance(src, dict):
            raise OpError("source должен быть объектом {\"quote\": \"слова владельца\"}")
        quote = ((src or {}).get("quote") or "").strip()
        if not quote:
            if required:
                raise OpError("%s — только по словам владельца: нужен source.quote с его фразой. "
                              "Молчание — не согласие (правило 1)" % what)
            return {"type": "agent"}
        if len(quote) > 2000:
            raise OpError("source.quote длиннее 2000 знаков")
        return {"type": "owner_message", "quote": quote, "at": (src or {}).get("at") or self.ctx.now}

    # ----- resolvers ----------------------------------------------------
    def _ref(self, value, prefix):
        if isinstance(value, str) and value.startswith("@"):
            name = value[1:]
            if name not in self.refs:
                raise OpError("ссылка %s не определена выше в этой квитанции (или её операция не записана)" % value)
            oid = self.refs[name]
            if not oid.startswith(prefix):
                raise OpError("ссылка %s указывает не на тот тип объекта" % value)
            return oid
        return None

    def thread(self, value, key="thread"):
        if value is None:
            raise OpError("не указана тема (%s)" % key)
        oid = self._ref(value, "th-")
        if oid:
            return self.st.threads[oid]
        if isinstance(value, str) and value in self.st.threads:
            t = self.st.threads[value]
            if t.get("error"):
                raise OpError("тема %s исправлена как ошибочная" % value)
            return t
        if isinstance(value, str):
            found = [t for t in self.st.live_threads() if norm(t["title"]) == norm(value)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise OpError("под «%s» подходит несколько тем: %s — уточните ID" % (
                    value, ", ".join("%s (%s)" % (t["title"], t["id"]) for t in found)))
        raise OpError("тема «%s» не найдена" % value)

    def criterion(self, value):
        oid = self._ref(value, "cr-")
        if oid:
            return self.st.criteria[oid]
        c = self.st.criteria.get(value)
        if not c or c.get("error"):
            raise OpError("условие готовности «%s» не найдено" % value)
        return c

    def decision(self, value):
        oid = self._ref(value, "dec-")
        if oid:
            return self.st.decisions[oid]
        num = None
        if isinstance(value, int):
            num = value
        elif isinstance(value, str):
            m = re.fullmatch(r"(?:вопрос|q|решение)?\s*№?\s*(\d+)", value.strip().lower())
            if m:
                num = int(m.group(1))
        if num is not None:
            d = self.st.decision_by_number(num)
            if d and not d.get("error"):
                return d
            raise OpError("вопроса или решения №%d нет" % num)
        d = self.st.decisions.get(value)
        if not d or d.get("error"):
            raise OpError("решение «%s» не найдено" % value)
        return d

    def parking_item(self, value):
        oid = self._ref(value, "park-")
        if oid:
            return self.st.parking[oid]
        p = self.st.parking.get(value)
        if p and not p.get("error"):
            return p
        if isinstance(value, str):
            found = [p for p in self.st.parking.values() if norm(p["text"]) == norm(value) and not p.get("error")]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise OpError("в парковке несколько записей «%s» — уточните ID" % value)
        raise OpError("запись парковки «%s» не найдена" % value)

    def result(self, op):
        if op.get("result") is not None:
            oid = self._ref(op["result"], "res-")
            r = self.st.results.get(oid or op["result"])
            if not r or r.get("error"):
                raise OpError("результат «%s» не найден" % op["result"])
            if r.get("withdrawn"):
                raise OpError("результат %s отозван" % r["id"])
            return r
        t = self.thread(op.get("thread"))
        version = op.get("version")
        if version is not None:
            if isinstance(version, str):
                version = int(version.lstrip("rR") or 0)
            for r in self.st.results_of(t["id"]):
                if r["version"] == version:
                    return r
            raise OpError("у темы «%s» нет версии r%s" % (t["title"], version))
        r = self.st.latest_result(t["id"])
        if not r:
            raise OpError("у темы «%s» нет предъявленного результата" % t["title"])
        return r

    def frame(self, value):
        f = self.st.frames.get(value)
        if not f or f.get("error"):
            raise OpError("договорённость «%s» не найдена" % value)
        return f

    def rule(self, value):
        oid = self._ref(value, "rule-")
        r = self.st.rules.get(oid or value)
        if not r or r.get("error"):
            raise OpError("правило «%s» не найдено" % value)
        return r

    # ----- preconditions (E.2) -----------------------------------------
    def check_expect(self, op):
        expect = op.get("expect") or {}
        if not isinstance(expect, dict):
            raise OpError("expect должен быть объектом {\"ID\": ревизия}")
        for oid, rev in expect.items():
            obj = self.st.lookup(oid)
            if obj is None:
                raise OpError("в expect объект %s не найден" % oid)
            if obj.get("rev") != rev:
                raise OpError("объект %s изменился: ожидалась ревизия %s, сейчас %s — перечитайте и повторите"
                              % (oid, rev, obj.get("rev")))

    def check_base(self, objs):
        base = self.ctx.base_snapshot
        if base is None:
            return
        deps = []
        for o in objs:
            if o is None:
                continue
            deps.append(o)
            if o.get("id", "").startswith("th-"):
                deps += [d for d in self.st.decisions.values() if d.get("thread_id") == o["id"]]
                deps += [f for f in self.st.frames.values() if f.get("thread_id") == o["id"]]
        for o in deps:
            if o.get("last_tx", 0) > base and o.get("last_session") != self.ctx.session:
                label = o.get("title") or o.get("question") or o.get("text") or o.get("doing") or o["id"]
                raise OpError("«%s» (%s) изменилось в другой сессии после вашей сводки (снимок %d) — "
                              "перечитайте (atlas.py show %s) и повторите" % (label, o["id"], base, o["id"]))

    def check_wip(self, thread_id, op, src):
        limit = int(self.st.settings.get("wip_limit", 3))
        count = self.st.wip_count(exclude=thread_id)
        if count < limit:
            return False
        if op.get("over_limit") is True:
            if src.get("type") != "owner_message":
                raise OpError("превысить лимит тем в работе можно только по словам владельца (source.quote)")
            return True
        active = [t["title"] for t in self.st.live_threads() if self.st.view_state(t) == "active" and t["id"] != thread_id]
        raise OpError("в работе уже %d из %d тем (%s). Взять ещё — только с явного согласия владельца "
                      "(over_limit: true и его слова) или поставив другую тему на паузу (§4.4)"
                      % (count, limit, ", ".join(active)))

    # ----- record builder ----------------------------------------------
    def emit(self, kind, data, source=None):
        rec = {"id": new_id("rec"), "kind": kind, "data": data, "at": self.ctx.now}
        if source:
            rec["source"] = source
        self.st.apply_record(rec, {"tx": self.tx, "at": self.ctx.now, "agent": self.ctx.agent,
                                   "session": self.ctx.session})
        return rec

    def set_ref(self, op, oid):
        name = op.get("ref")
        if name:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", name):
                raise OpError("ref — короткое имя из латиницы и цифр")
            self.refs[name] = oid

    # ==================================================================
    def apply(self, op):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise OpError("каждая операция — объект с полем \"op\"")
        name = op["op"]
        handler = getattr(self, "op_" + name.replace(".", "_"), None)
        if handler is None:
            raise OpError("неизвестная операция «%s» (список — references/receipts.md)" % name)
        if looks_secret({k: v for k, v in op.items() if k not in ("op", "ref", "group", "expect")}):
            raise OpError("в тексте похоже на секрет (ключ, токен или пароль) — не записываю. "
                          "Уберите его; Атлас хранит только описание, не сами секреты (F.2)")
        self.check_expect(op)
        return handler(op)

    # ----- project --------------------------------------------------------
    def op_goal_set(self, op):
        text = self.text(op, "text", True, 500)
        src = self.source(op, True, "цель проекта задаёт владелец")
        old = self.st.goal()
        if old and norm(old["text"]) == norm(text):
            raise OpError("такая цель уже записана")
        gid = new_id("goal")
        self.emit("goal.set", {"goal_id": gid, "text": text, "scenario": self.text(op, "scenario"),
                               "reason": self.text(op, "reason")}, src)
        if old:
            return "цель изменена: «%s» (прежняя сохранена в истории и не считается достигнутой)" % text
        return "цель: «%s»" % text

    def op_project_rename(self, op):
        name = self.text(op, "name", True, 200)
        src = self.source(op, True, "название проекта задаёт владелец")
        self.emit("project.rename", {"name": name}, src)
        return "проект называется «%s»" % name

    def op_settings_set(self, op):
        key = self.choice(op, "key", ("wip_limit", "stale_parking_days"))
        value = op.get("value")
        if not isinstance(value, int) or not 1 <= value <= 365:
            raise OpError("value — целое число от 1")
        src = self.source(op, True, "правила проекта меняет владелец")
        self.emit("settings.set", {"key": key, "value": value}, src)
        return "правило проекта: %s = %s" % (key, value)

    # ----- threads --------------------------------------------------------
    def op_thread_create(self, op):
        title = self.text(op, "title", True, 200)
        state = self.choice(op, "state", ("candidate", "planned", "active", "paused"), "active")
        src = self.source(op, False, "")
        dup = [t for t in self.st.live_threads() if norm(t["title"]) == norm(title) and t["state"] != "released"]
        if dup:
            raise OpError("тема «%s» уже есть (%s)" % (dup[0]["title"], dup[0]["id"]))
        parent = None
        if op.get("parent") is not None:
            parent = self.thread(op["parent"], "parent")["id"]
        tid = new_id("th")
        over = self.check_wip(tid, op, src) if state == "active" else False
        self.emit("thread.create", {
            "thread_id": tid, "title": title, "why": self.text(op, "why", False, 500), "state": state,
            "leads_to_goal": self.flag(op, "leads_to_goal"), "parent": parent, "reason": self.text(op, "reason"),
            "over_limit": over,
        }, src)
        self.set_ref(op, tid)
        return "тема %s «%s» — %s%s" % (tid, title, ru.THREAD_STATE[state],
                                        " (сверх лимита — решение владельца)" if over else "")

    def op_thread_state(self, op):
        t = self.thread(op.get("thread"))
        state = self.choice(op, "state", WORK_STATES)
        self.check_base([t])
        if state == "candidate":
            raise OpError("в «кандидаты» тему не возвращают; можно поставить на паузу или отпустить")
        if t["state"] == state:
            raise OpError("тема «%s» уже в состоянии «%s»" % (t["title"], ru.THREAD_STATE[state]))
        reason = self.text(op, "reason", False, 500)
        needs_owner = (state in ("released", "frozen") or t["state"] in ("candidate", "released", "frozen")
                       or op.get("over_limit"))
        src = self.source(op, bool(needs_owner), "это решение о теме")
        if state == "released" and not reason:
            raise OpError("«отпущено» — решение с причиной: нужно поле reason")
        if t["state"] in ("released", "frozen") and not reason:
            raise OpError("возврат темы требует основания: нужно поле reason (история сохранится)")
        over = self.check_wip(t["id"], op, src) if state == "active" else False
        self.emit("thread.state", {"thread_id": t["id"], "state": state, "reason": reason, "over_limit": over}, src)
        self.set_ref(op, t["id"])
        return "тема «%s»: %s → %s%s" % (t["title"], ru.THREAD_STATE[t.get("prev_state") or "active"],
                                         ru.THREAD_STATE[state], (" — " + reason) if reason else "")

    def op_thread_update(self, op):
        t = self.thread(op.get("thread"))
        self.check_base([t])
        data = {"thread_id": t["id"]}
        for key, maxlen in (("title", 200), ("why", 500)):
            v = self.text(op, key, False, maxlen)
            if v is not None:
                data[key] = v
        if "leads_to_goal" in op:
            data["leads_to_goal"] = self.flag(op, "leads_to_goal")
        if len(data) == 1:
            raise OpError("нечего менять: title, why или leads_to_goal")
        self.emit("thread.update", data, self.source(op, False, ""))
        self.set_ref(op, t["id"])
        return "тема «%s» обновлена" % data.get("title", t["title"])

    # ----- criteria -------------------------------------------------------
    def op_criterion_add(self, op):
        t = self.thread(op.get("thread"))
        text = self.text(op, "text", True, 300)
        if any(norm(c["text"]) == norm(text) for c in self.st.thread_criteria(t)):
            raise OpError("такое условие у темы уже есть")
        cid = new_id("cr")
        self.emit("criterion.add", {"criterion_id": cid, "thread_id": t["id"], "text": text,
                                    "observable": self.flag(op, "observable", True),
                                    "how": self.text(op, "how", False, 500)}, self.source(op, False, ""))
        self.set_ref(op, cid)
        return "условие готовности %s «%s» (тема «%s»)" % (cid, text, t["title"])

    def op_criterion_revise(self, op):
        c = self.criterion(op.get("criterion"))
        text = self.text(op, "text", True, 300)
        if "substantive" not in op:
            raise OpError("укажите substantive: true (меняется смысл — прежние проверки придётся повторить) "
                          "или false (только написание)")
        sub = self.flag(op, "substantive")
        self.emit("criterion.revise", {"criterion_id": c["id"], "text": text, "substantive": sub},
                  self.source(op, False, ""))
        return "условие %s переписано%s" % (c["id"], " по смыслу — прежние проверки к нему не относятся" if sub else "")

    def op_criterion_withdraw(self, op):
        c = self.criterion(op.get("criterion"))
        reason = self.text(op, "reason", True, 500)
        self.emit("criterion.withdraw", {"criterion_id": c["id"], "reason": reason},
                  self.source(op, True, "снять условие готовности"))
        return "условие %s снято: %s" % (c["id"], reason)

    # ----- notes (observations) ------------------------------------------
    def _note(self, op, kind, thread_required):
        text = self.text(op, "text", True, 1000)
        tid = None
        if op.get("thread") is not None or thread_required:
            tid = self.thread(op.get("thread"))["id"]
        data = {"note_id": new_id("note"), "thread_id": tid, "text": text}
        rec_from = op.get("recovered")
        if rec_from is not None:
            data["recovered"] = self.choice(op, "recovered", RECOVERED_FROM)
        self.emit("note." + kind, data, self.source(op, False, ""))
        self.set_ref(op, data["note_id"])
        return data

    def op_note_progress(self, op):
        self._note(op, "progress", False)
        return "сделано: %s" % op["text"].strip()

    def op_note_learned(self, op):
        self._note(op, "learned", False)
        return "узнали: %s" % op["text"].strip()

    def op_note_debt(self, op):
        self._note(op, "debt", True)
        return "костыль: %s" % op["text"].strip()

    def op_note_resolve(self, op):
        n = self.st.notes.get(op.get("note"))
        if not n or n.get("error"):
            raise OpError("запись «%s» не найдена" % op.get("note"))
        self.emit("note.resolve", {"note_id": n["id"], "text": self.text(op, "text", False, 500)},
                  self.source(op, False, ""))
        return "костыль закрыт: %s" % n["text"]

    def op_attempt(self, op):
        t = self.thread(op.get("thread"))
        approach = self.text(op, "approach", True, 500)
        outcome = self.choice(op, "outcome", ("failed", "partial", "succeeded"))
        new_approach = self.flag(op, "new_approach", True)
        aid = new_id("try")
        crit = self.criterion(op["criterion"])["id"] if op.get("criterion") is not None else None
        data = {"attempt_id": aid, "thread_id": t["id"], "criterion_id": crit,
                "problem": self.text(op, "problem", False, 500),
                "approach": approach, "outcome": outcome, "new_approach": new_approach,
                "learned": self.text(op, "learned", False, 500)}
        if op.get("recovered") is not None:
            data["recovered"] = self.choice(op, "recovered", RECOVERED_FROM)
        self.emit("attempt", data, self.source(op, False, ""))
        self.set_ref(op, aid)
        words = {"failed": "не получилось", "partial": "получилось частично", "succeeded": "получилось"}
        return "попытка (%s): %s — %s%s" % (t["title"], approach, words[outcome],
                                            "" if new_approach else "; подход повторяет прежний")

    # ----- decisions ------------------------------------------------------
    def _options(self, op):
        opts = op.get("options") or []
        if not isinstance(opts, list):
            raise OpError("options — список {label, consequence}")
        out = []
        for o in opts:
            if isinstance(o, str):
                o = {"label": o}
            if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
                raise OpError("у варианта нужен label")
            out.append({"label": o["label"].strip(), "consequence": (o.get("consequence") or "").strip() or None})
        if len(out) == 1:
            raise OpError("один вариант — не выбор: дайте 2+ варианта или ни одного")
        if len(out) > 4:
            raise OpError("больше четырёх вариантов — владельцу тяжело выбирать; оставьте реально разные")
        return out

    def _option_label(self, dec, value):
        if value is None:
            return None
        labels = [o["label"] for o in dec.get("options", [])]
        if not labels:
            return value
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            i = int(value) - 1
            if 0 <= i < len(labels):
                return labels[i]
        if isinstance(value, str):
            v = value.strip()
            letters = "абвгabcd"
            if len(v) == 1 and v.lower() in letters:
                i = letters.index(v.lower()) % 4
                if i < len(labels):
                    return labels[i]
            for lbl in labels:
                if norm(lbl) == norm(v):
                    return lbl
        raise OpError("вариант «%s» не найден среди: %s" % (value, "; ".join(labels)))

    def op_decision_ask(self, op):
        question = self.text(op, "question", True, 300)
        tid = self.thread(op["thread"])["id"] if op.get("thread") is not None else None
        risk = self.choice(op, "risk", RISKS, "normal")
        same = [d for d in self.st.open_decisions() if norm(d["question"]) == norm(question)]
        if same:
            raise OpError("такой вопрос уже открыт (вопрос %d) — одно решение, одна карточка; "
                          "временный выбор добавьте через decision.provisional" % same[0]["number"])
        options = self._options(op)
        advice = op.get("advice")
        if advice is not None:
            if not isinstance(advice, dict) or not (advice.get("text") or advice.get("option")):
                raise OpError("advice — {option, text, wrong_if}")
            if not advice.get("wrong_if"):
                raise OpError("у совета нужно условие, когда он станет неверным (advice.wrong_if)")
        provisional = op.get("provisional")
        if provisional is not None:
            if risk != "normal":
                raise OpError("временный выбор агента недопустим, когда затронуто: %s — нужно явное «да» (C.4)"
                              % RISK_WORDS[risk])
            if not isinstance(provisional, dict):
                raise OpError("provisional — {option, text}")
        crit = self.criterion(op["criterion"])["id"] if op.get("criterion") is not None else None
        number = self.st.decision_counter + 1
        did = new_id("dec")
        self.emit("decision.ask", {
            "decision_id": did, "number": number, "question": question, "thread_id": tid, "criterion_id": crit,
            "options": options, "advice": advice, "provisional": provisional,
            "urgency": self.choice(op, "urgency", ("normal", "blocking", "deadline"), "normal"),
            "deadline": self.text(op, "deadline", False, 100),
            "can_defer": self.flag(op, "can_defer", True), "risk": risk,
        }, {"type": "agent"})
        self.set_ref(op, did)
        return "вопрос %d: %s (%s)" % (number, question, did)

    def op_decision_provisional(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "proposed":
            raise OpError("вопрос %d уже не открыт" % dec["number"])
        if dec.get("risk", "normal") != "normal":
            raise OpError("временный выбор недопустим: %s (C.4)" % RISK_WORDS[dec["risk"]])
        option = self._option_label(dec, op.get("option"))
        text = self.text(op, "text", False, 300)
        if not option and not text:
            raise OpError("нужен option или text")
        self.emit("decision.provisional", {"decision_id": dec["id"], "option": option, "text": text,
                                           "until": self.text(op, "until", False, 200)}, {"type": "agent"})
        return "вопрос %d: пока работаю с вариантом «%s» — временно, до ответа владельца" % (dec["number"], option or text)

    def op_decision_decide(self, op):
        delegated = self.flag(op, "delegated", False)
        reason = self.text(op, "reason", False, 500)
        if op.get("decision") is None:
            text = self.text(op, "text", True, 300)
            for d in self.st.decided():
                if norm(d["text"]) == norm(text):
                    raise OpError("такое решение уже записано (решение %d от %s) — это отсылка к прошлому, "
                                  "новое решение не создаю (правило 4)" % (
                                      d["number"], ru.short_date(ru.parse_ts(d["decided_at"]))))
            if delegated:
                raise OpError("delegated относится к открытому вопросу, которому владелец сказал «реши сам»")
            src = self.source(op, True, "новое решение")
            number = self.st.decision_counter + 1
            did = new_id("dec")
            tid = self.thread(op["thread"])["id"] if op.get("thread") is not None else None
            self.emit("decision.decide", {"decision_id": did, "created": True, "number": number, "text": text,
                                          "reason": reason, "thread_id": tid,
                                          "risk": self.choice(op, "risk", RISKS, "normal")}, src)
            self.set_ref(op, did)
            return "решение %d: %s — записано со словами владельца (%s)" % (number, text, did)
        dec = self.decision(op["decision"])
        self.check_base([dec])
        if dec["status"] != "proposed":
            raise OpError("вопрос %d уже не открыт (%s)" % (dec["number"], dec["status"]))
        option = self._option_label(dec, op.get("option"))
        text = self.text(op, "text", False, 300)
        if not text:
            if not option:
                raise OpError("нужен option (выбранный вариант) или text (решение словами)")
            text = "%s — %s" % (dec["question"].rstrip("?").rstrip(), option)
        if delegated:
            if not dec.get("delegated"):
                raise OpError("владелец не передавал этот вопрос агенту («реши сам» не записано)")
            if dec.get("risk", "normal") != "normal":
                raise OpError("«реши сам» не распространяется на %s" % RISK_WORDS[dec["risk"]])
            src = {"type": "delegated", "delegation": dec.get("delegation_source")}
        else:
            src = self.source(op, True, "ответ на вопрос %d" % dec["number"])
        self.emit("decision.decide", {"decision_id": dec["id"], "text": text, "option": option,
                                      "reason": reason, "delegated": delegated}, src)
        self.set_ref(op, dec["id"])
        who = "агент по поручению «реши сам»" if delegated else "со словами владельца"
        return "вопрос %d решён: %s — %s" % (dec["number"], text, who)

    def op_decision_delegate(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "proposed":
            raise OpError("вопрос %d уже не открыт" % dec["number"])
        if dec.get("risk", "normal") != "normal":
            raise OpError("«реши сам» не распространяется на %s — нужно явное решение владельца" % RISK_WORDS[dec["risk"]])
        src = self.source(op, True, "передать решение агенту")
        self.emit("decision.delegate", {"decision_id": dec["id"]}, src)
        return "вопрос %d передан агенту в рамках договорённости" % dec["number"]

    def op_decision_defer(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "proposed":
            raise OpError("вопрос %d уже не открыт" % dec["number"])
        if not dec.get("can_defer", True):
            raise OpError("этот вопрос помечен как неотложный")
        self.emit("decision.defer", {"decision_id": dec["id"]}, self.source(op, False, ""))
        return "вопрос %d отложен: остаётся в «Нужно от вас»; молчание не станет согласием" % dec["number"]

    def op_decision_supersede(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "decided":
            raise OpError("заменить можно только действующее решение")
        text = self.text(op, "text", True, 300)
        src = self.source(op, True, "замена решения")
        number = self.st.decision_counter + 1
        new = new_id("dec")
        self.emit("decision.decide", {"decision_id": new, "created": True, "number": number, "text": text,
                                      "reason": self.text(op, "reason", False, 500),
                                      "thread_id": dec.get("thread_id"), "supersedes": dec["id"]}, src)
        self.emit("decision.supersede", {"decision_id": dec["id"], "new_decision_id": new,
                                         "reason": self.text(op, "reason", False, 500)}, src)
        self.set_ref(op, new)
        return "решение %d «%s» заменено решением %d «%s»; прежнее осталось в истории" % (
            dec["number"], dec["text"], number, text)

    def op_decision_withdraw(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] not in ("proposed", "decided"):
            raise OpError("решение %d уже не действует" % dec["number"])
        reason = self.text(op, "reason", True, 500)
        src = self.source(op, dec["status"] == "decided", "отмена решения")
        self.emit("decision.withdraw", {"decision_id": dec["id"], "reason": reason}, src)
        return "%s %d снят: %s" % ("вопрос" if dec["status"] == "proposed" else "решение", dec["number"], reason)

    def op_decision_ack(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "decided":
            raise OpError("подтверждать получение можно только действующего решения")
        self.emit("decision.ack", {"decision_id": dec["id"], "rev": dec["rev"]}, {"type": "agent"})
        return "решение %d: агент подтвердил получение (учёт в работе не проверен)" % dec["number"]

    def op_decision_applied(self, op):
        dec = self.decision(op.get("decision"))
        if dec["status"] != "decided":
            raise OpError("решение %d не действует" % dec["number"])
        text = self.text(op, "text", True, 500)
        self.emit("decision.applied", {"decision_id": dec["id"], "text": text}, {"type": "agent"})
        return "решение %d: агент сообщил, что учёл его в работе — %s" % (dec["number"], text)

    # ----- parking --------------------------------------------------------
    def op_park_add(self, op):
        text = self.text(op, "text", True, 300)
        if any(norm(p["text"]) == norm(text) for p in self.st.parked()):
            raise OpError("«%s» уже в парковке" % text)
        pid = new_id("park")
        data = {"item_id": pid, "text": text, "kind": self.choice(op, "kind", ("idea", "question"), "idea"),
                "context": self.text(op, "context", False, 200), "quote": self.text(op, "quote", False, 500),
                "goal_related": self.flag(op, "goal_related")}
        if op.get("recovered") is not None:
            data["recovered"] = self.choice(op, "recovered", RECOVERED_FROM)
        self.emit("park.add", data, self.source(op, False, ""))
        self.set_ref(op, pid)
        return "в парковку: %s%s" % (text, " (вопрос)" if data["kind"] == "question" else "")

    def op_park_classify(self, op):
        p = self.parking_item(op.get("item"))
        self.emit("park.classify", {"item_id": p["id"], "goal_related": self.flag(op, "goal_related")},
                  {"type": "agent"})
        return "парковка: «%s» — связь с целью по мнению агента: %s" % (
            p["text"], {True: "есть", False: "нет", None: "не оценена"}[op.get("goal_related")])

    def op_park_promote(self, op):
        p = self.parking_item(op.get("item"))
        if p["status"] != "parked":
            raise OpError("«%s» уже не в парковке" % p["text"])
        to = self.choice(op, "to", ("planned", "active"), "planned")
        src = self.source(op, True, "взять идею в темы")
        title = self.text(op, "title", False, 200) or p["text"]
        if any(norm(t["title"]) == norm(title) for t in self.st.live_threads() if t["state"] != "released"):
            raise OpError("тема «%s» уже есть" % title)
        tid = new_id("th")
        over = self.check_wip(tid, op, src) if to == "active" else False
        self.emit("thread.create", {"thread_id": tid, "title": title, "why": self.text(op, "why", False, 500),
                                    "state": to, "leads_to_goal": p.get("goal_related"), "from_parking": p["id"],
                                    "over_limit": over}, src)
        self.emit("park.promote", {"item_id": p["id"], "thread_id": tid, "to": to}, src)
        self.set_ref(op, tid)
        return "«%s» → тема %s, %s" % (p["text"], tid, ru.THREAD_STATE[to])

    def op_park_release(self, op):
        p = self.parking_item(op.get("item"))
        if p["status"] != "parked":
            raise OpError("«%s» уже не в парковке" % p["text"])
        reason = self.text(op, "reason", False, 300) or "владелец решил не делать"
        src = self.source(op, True, "отпустить идею")
        self.emit("park.release", {"item_id": p["id"], "reason": reason}, src)
        return "отпущено: %s — в архиве идей, можно вернуть" % p["text"]

    def op_park_keep(self, op):
        p = self.parking_item(op.get("item"))
        self.emit("park.keep", {"item_id": p["id"]}, self.source(op, False, ""))
        return "оставлено в парковке: %s" % p["text"]

    def op_park_restore(self, op):
        p = self.parking_item(op.get("item"))
        if p["status"] != "released":
            raise OpError("«%s» не в архиве идей" % p["text"])
        self.emit("park.restore", {"item_id": p["id"]}, self.source(op, True, "вернуть идею"))
        return "возвращено в парковку: %s" % p["text"]

    # ----- results and trust ---------------------------------------------
    def _criteria_list(self, values, thread_id=None, allowed=None):
        if not isinstance(values, list) or not values:
            raise OpError("criteria — непустой список условий готовности")
        out = []
        for v in values:
            c = self.criterion(v)
            if c["withdrawn"]:
                raise OpError("условие %s снято" % c["id"])
            if thread_id and c["thread_id"] != thread_id:
                raise OpError("условие %s относится к другой теме" % c["id"])
            if allowed is not None and c["id"] not in allowed:
                raise OpError("условие %s не входит в эту версию результата — согласие и проверка "
                              "не переносятся на соседние условия (B.3)" % c["id"])
            if c["id"] not in [o["id"] for o in out]:
                out.append({"id": c["id"], "scope_rev": c["scope_rev"]})
        return out

    def op_result_present(self, op):
        t = self.thread(op.get("thread"))
        self.check_base([t])
        criteria = self._criteria_list(op.get("criteria"), t["id"])
        summary = self.text(op, "summary", True, 500)
        check = op.get("check") or {}
        if not isinstance(check, dict):
            raise OpError("check — {run, steps, expect, failure, duration}")
        steps = self.texts(check, "steps", 4)
        if isinstance(check.get("steps"), list) and len(check["steps"]) > 4:
            raise OpError("инструкция проверки — не больше четырёх шагов (§7а, правило 12)")
        version = self.st.max_version(t["id"]) + 1
        rid = new_id("res")
        fp = self.ctx.fingerprint()
        self.emit("result.present", {
            "result_id": rid, "thread_id": t["id"], "version": version, "criteria": criteria,
            "summary": summary, "excluded": self.texts(op, "excluded", 10),
            "fit_for": self.text(op, "fit_for", False, 300), "not_fit_for": self.text(op, "not_fit_for", False, 300),
            "caveats": self.texts(op, "caveats", 6), "claim": self.text(op, "claim", False, 500),
            "check": {"run": self.text(check, "run", False, 300), "steps": steps,
                      "expect": self.text(check, "expect", False, 300),
                      "failure": self.text(check, "failure", False, 300),
                      "duration": self.text(check, "duration", False, 100)},
            "fingerprint": fp,
        }, {"type": "agent"})
        self.set_ref(op, rid)
        note = "" if fp else " (git нет — версию нельзя привязать к состоянию кода)"
        return "результат %s: «%s», версия r%d, условий в составе: %d%s" % (rid, t["title"], version, len(criteria), note)

    def op_result_withdraw(self, op):
        r = self.result(op)
        reason = self.text(op, "reason", True, 300)
        self.emit("result.withdraw", {"result_id": r["id"], "reason": reason}, self.source(op, False, ""))
        return "результат r%d отозван: %s" % (r["version"], reason)

    def op_verify(self, op):
        r = self.result(op)
        allowed = [c["id"] for c in r["criteria"]]
        criteria = self._criteria_list(op.get("criteria") or allowed, r["thread_id"], allowed)
        method = self.choice(op, "method", ("owner_manual", "agent_report", "external", "automated_run"))
        outcome = self.choice(op, "outcome", ("passed", "failed", "could_not_check"))
        downgraded = False
        if method == "automated_run":
            # B.2 / A44: the agent cannot raise provenance by a field. Only a
            # run observed by the core (`atlas.py check`) is automated_run.
            method = "agent_report"
            downgraded = True
        src = self.source(op, method == "owner_manual", "ручная проверка владельца")
        if method == "external" and not self.text(op, "detail"):
            raise OpError("для независимой проверки укажите detail: кто проверял и в какой области")
        fp = self.ctx.fingerprint() if method in ("owner_manual", "external") else None
        vid = new_id("ver")
        self.emit("verify", {
            "verification_id": vid, "result_id": r["id"], "thread_id": r["thread_id"], "criteria": criteria,
            "method": method, "outcome": outcome, "environment": self.text(op, "environment", False, 200),
            "limitations": self.texts(op, "limitations", 6), "detail": self.text(op, "detail", False, 500),
            "fingerprint": fp, "downgraded": downgraded,
        }, src)
        self.set_ref(op, vid)
        t = self.st.threads[r["thread_id"]]
        what = {"passed": "проверка пройдена", "failed": "провал записан — тема возвращается в работу",
                "could_not_check": "не удалось проверить (провалом не считается)"}[outcome]
        line = "«%s» r%d: %s (%s)" % (t["title"], r["version"], what, {
            "owner_manual": "вами вручную", "agent_report": "со слов агента — «проверено» от этого не растёт",
            "external": "независимая проверка"}[method])
        if downgraded:
            line += "; запуск не наблюдался ядром — для «проверено тестом» используйте atlas.py check"
        rfp = (r.get("fingerprint") or {}).get("hash")
        if fp and rfp and fp.get("hash") != rfp:
            line += "; код менялся после предъявления r%d — проверка записана для текущего состояния, " \
                    "к r%d она может не относиться" % (r["version"], r["version"])
        return line

    def op_accept(self, op):
        r = self.result(op)
        self.check_base([r])
        latest = self.st.latest_result(r["thread_id"])
        if latest and latest["id"] != r["id"] and not op.get("old_version_ok"):
            raise OpError("вы принимаете версию r%d, а сейчас уже r%d — покажите владельцу разницу; "
                          "если он принимает именно старую версию, добавьте old_version_ok: true"
                          % (r["version"], latest["version"]))
        allowed = [c["id"] for c in r["criteria"]]
        criteria = [c["id"] for c in self._criteria_list(op.get("criteria") or allowed, r["thread_id"], allowed)]
        outcome = self.choice(op, "outcome", ("accepted", "accepted_with_caveat", "rejected"))
        purpose = self.text(op, "purpose", outcome != "rejected", 200)
        caveat = self.text(op, "caveat", outcome == "accepted_with_caveat", 300)
        rejection = None
        if outcome == "rejected":
            rj = op.get("rejection") or {}
            if not isinstance(rj, dict) or rj.get("class") not in REJECTION_CLASSES:
                raise OpError("отказ классифицируется: rejection.class = %s" % ", ".join(REJECTION_CLASSES))
            rejection = {"class": rj["class"], "reason": (rj.get("reason") or "").strip() or None}
        src = self.source(op, True, "приёмка результата")
        fp = self.ctx.fingerprint()
        aid = new_id("acc")
        self.emit("accept", {"acceptance_id": aid, "result_id": r["id"], "thread_id": r["thread_id"],
                             "criteria": criteria, "purpose": purpose, "outcome": outcome, "caveat": caveat,
                             "rejection": rejection, "fingerprint": fp}, src)
        self.set_ref(op, aid)
        t = self.st.threads[r["thread_id"]]
        part = "" if len(criteria) == len(allowed) else " (условий %d из %d)" % (len(criteria), len(allowed))
        if outcome == "rejected":
            line = "«%s» r%d не принят%s: %s%s; условия не переписаны, тема возвращается в работу" % (
                t["title"], r["version"], part, REJECTION_CLASSES[rejection["class"]],
                (" — " + rejection["reason"]) if rejection["reason"] else "")
        else:
            line = "«%s» r%d принят%s для: %s%s. Это не разрешение на публикацию и настоящих пользователей" % (
                t["title"], r["version"], part, purpose, (" — с оговоркой: " + caveat) if caveat else "")
        checked = [row for row in self.st.result_trust(r) if row["check"]["status"] == "passed"]
        if not checked:
            line += "; проверки нет — «проверено» от приёмки не растёт"
        rfp = (r.get("fingerprint") or {}).get("hash")
        if fp and rfp and fp.get("hash") != rfp:
            line += "; код менялся после предъявления — принята именно r%d" % r["version"]
        return line

    # ----- frames and rules ---------------------------------------------
    def op_frame_agree(self, op):
        doing = self.text(op, "doing", True, 300)
        stop_when = self.text(op, "stop_when", True, 300)
        tid = self.thread(op["thread"])["id"] if op.get("thread") is not None else None
        src = self.source(op, True, "договорённость о задании")
        perms = op.get("permissions") or []
        if not isinstance(perms, list) or not all(isinstance(p, dict) and p.get("what") for p in perms):
            raise OpError("permissions — список {what, limit}")
        fid = new_id("fr")
        self.emit("frame.agree", {
            "frame_id": fid, "thread_id": tid, "doing": doing, "where": self.text(op, "where", False, 300),
            "not_touching": self.text(op, "not_touching", False, 300),
            "decide_myself": self.text(op, "decide_myself", False, 300),
            "ask_you": self.text(op, "ask_you", False, 300), "stop_when": stop_when,
            "budget": self.text(op, "budget", False, 200), "data_access": self.text(op, "data_access", False, 200),
            "recovery": self.text(op, "recovery", False, 300), "permissions": perms,
            "standing": self.flag(op, "standing", False), "mode": "advisory",
        }, src)
        self.set_ref(op, fid)
        return "договорённость %s записана: %s%s" % (fid, doing, " — правило проекта по умолчанию" if op.get("standing") else "")

    def op_frame_close(self, op):
        f = self.frame(op.get("frame"))
        if f["status"] != "active":
            raise OpError("договорённость уже закрыта")
        reason = self.text(op, "reason", True, 300)
        self.emit("frame.close", {"frame_id": f["id"], "reason": reason}, self.source(op, bool(f.get("standing")), "отмена правила"))
        return "договорённость «%s» закрыта: %s" % (f["doing"], reason)

    def op_rule_propose(self, op):
        text = self.text(op, "text", True, 300)
        rid = new_id("rule")
        self.emit("rule.propose", {"rule_id": rid, "text": text}, {"type": "agent"})
        self.set_ref(op, rid)
        return "предложено правило %s «%s» — не действует, пока владелец не подтвердит" % (rid, text)

    def op_rule_confirm(self, op):
        r = self.rule(op.get("rule"))
        if r["status"] != "proposed":
            raise OpError("правило не ждёт подтверждения")
        self.emit("rule.confirm", {"rule_id": r["id"]}, self.source(op, True, "подтверждение правила"))
        return "правило действует: %s" % r["text"]

    def op_rule_revoke(self, op):
        r = self.rule(op.get("rule"))
        if r["status"] == "revoked":
            raise OpError("правило уже отменено")
        self.emit("rule.revoke", {"rule_id": r["id"]}, self.source(op, True, "отмена правила"))
        return "правило отменено: %s" % r["text"]

    # ----- bookmark -------------------------------------------------------
    def op_bookmark_save(self, op):
        stopped_at = self.text(op, "stopped_at", True, 400)
        nxt = op.get("next")
        if nxt is not None:
            if isinstance(nxt, str):
                nxt = {"text": nxt}
            if not isinstance(nxt, dict) or not nxt.get("text"):
                raise OpError("next — {text, kind, ref, why}")
            kind = nxt.get("kind", "other")
            if kind not in ("check_result", "answer_decision", "continue_thread", "other"):
                raise OpError("next.kind: check_result, answer_decision, continue_thread или other")
            ref = nxt.get("ref")
            if kind != "other" and ref is None:
                raise OpError("для next.kind=%s нужен ref (ID результата, вопроса или темы)" % kind)
            if kind == "continue_thread":
                ref = self.thread(ref, "next.ref")["id"]
            elif kind == "answer_decision":
                ref = self.decision(ref)["id"]
            elif kind == "check_result":
                ref = self.result({"result": ref} if str(ref).startswith(("res-", "@")) else {"thread": ref})["id"]
            nxt = {"text": nxt["text"].strip(), "kind": kind, "ref": ref, "why": nxt.get("why")}
        bid = new_id("bm")
        self.emit("bookmark.save", {
            "bookmark_id": bid, "stopped_at": stopped_at, "today": self.texts(op, "today", 5),
            "learned": self.texts(op, "learned", 5), "failed": self.texts(op, "failed", 5),
            "started": self.texts(op, "started", 5), "next": nxt,
            "thread_id": self.thread(op["thread"])["id"] if op.get("thread") is not None else None,
            "auto": False,
        }, self.source(op, False, ""))
        self.set_ref(op, bid)
        return "закладка %s" % bid

    # ----- corrections (Э11, B.6) ---------------------------------------
    EDITABLE = {"title", "why", "text", "question", "kind", "stopped_at"}

    def op_correct(self, op):
        target = op.get("target")
        if isinstance(target, int) or (isinstance(target, str) and re.fullmatch(r"(?:вопрос|решение)?\s*\d+", target.strip())):
            target = self.decision(target)["id"]
        oid = self._ref(target, "") or target
        obj = self.st.lookup(oid) if isinstance(oid, str) else None
        rec = None
        if obj is None:
            rec = next((r for r in self.st.records if r.get("id") == oid), None)
            if rec is None:
                raise OpError("исправлять нечего: объект или запись «%s» не найдены" % target)
        if oid in self.st.errors:
            raise OpError("«%s» уже исправлено" % oid)
        action = self.choice(op, "action", ("withdraw", "edit", "link"))
        was = self.text(op, "was", True, 500)
        becomes = self.text(op, "becomes", True, 500)
        data = {"correction_id": new_id("fix"), "target": oid, "action": action, "was": was, "becomes": becomes,
                "code_changed": False}
        if action == "edit":
            edit = op.get("edit")
            if obj is None or not isinstance(edit, dict) or not edit:
                raise OpError("для edit нужен объект и поле edit {поле: значение}")
            bad = set(edit) - self.EDITABLE
            if bad:
                raise OpError("исправлять можно поля: %s" % ", ".join(sorted(self.EDITABLE)))
            data["edit"] = edit
        if action == "link":
            link = op.get("link_to")
            link_obj = None
            if link is not None:
                link_id = self._ref(link, "") or link
                if isinstance(link, int) or (isinstance(link, str) and link.strip().isdigit()):
                    link_obj = self.decision(link)
                else:
                    link_obj = self.st.lookup(link_id)
            if not link_obj or link_obj.get("error"):
                raise OpError("link_to — существующий объект, на который нужно сослаться")
            data["link_to"] = link_obj["id"]
        data["affected"] = self._affected(obj, rec)
        self.emit("correct", data, self.source(op, False, ""))
        return "исправлено: было «%s» → стало «%s». Затронуто: %s. Код это не меняет" % (
            was, becomes, ", ".join(data["affected"]) or "только эта запись")

    def _affected(self, obj, rec):
        out = ["доклад"]
        tid = (obj or {}).get("thread_id") or ((rec or {}).get("data") or {}).get("thread_id")
        if obj is not None and obj.get("id", "").startswith("th-"):
            tid = obj["id"]
        if tid and tid in self.st.threads:
            out.append("тема «%s»" % self.st.threads[tid]["title"])
        if obj is not None and obj.get("id", "").startswith("dec-"):
            out.append("«Нужно от вас»" if obj["status"] == "proposed" else "список решений")
        if obj is not None and obj.get("id", "").startswith("park-"):
            out.append("парковка")
        return out

    # ----- capture ------------------------------------------------------
    def op_capture_recovered(self, op):
        text = self.text(op, "text", True, 1000)
        src_kind = self.choice(op, "from", RECOVERED_FROM)
        gap = op.get("gap")
        if gap is not None and gap not in self.st.gaps:
            raise OpError("пробел записи «%s» не найден" % gap)
        self.emit("capture.recovered", {"gap_id": gap, "text": text, "from": src_kind,
                                        "session": op.get("session")}, {"type": "recovered"})
        return "восстановлено задним числом (источник: %s): %s" % (
            {"git": "изменения файлов", "transcript": "журнал сессии", "chat": "чат"}[src_kind], text)


# ----------------------------------------------------------------------
def load_state(store):
    txs, problem = store.load_transactions()
    return State.from_transactions(txs), problem


def apply_receipt(store, receipt, ctx, dry_run=False):
    """Validate and write a receipt. Returns a result dict for rendering."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("ops"), list) or not receipt["ops"]:
        raise AtlasError("квитанция — JSON-объект с непустым списком \"ops\"")
    if len(receipt["ops"]) > 100:
        raise AtlasError("в одной квитанции больше 100 операций — разбейте")
    receipt_hash = sha256_text(canonical_json(receipt["ops"]))
    key = receipt.get("key")
    if key is not None and (not isinstance(key, str) or not key.strip() or len(key) > 200):
        raise AtlasError("key — непустая строка до 200 знаков")
    base = receipt.get("base_snapshot")
    if base is not None and not isinstance(base, int):
        raise AtlasError("base_snapshot — номер снимка из сводки (целое)")
    ctx.base_snapshot = base

    with store.lock():
        st, problem = load_state(store)
        if problem:
            raise AtlasError("Записи повреждены начиная с %s: %s. Новые записи поверх не делаю — "
                             "скажите владельцу и запустите atlas.py doctor." % (problem["file"], problem["why"]))
        if key and key in st.receipts:
            prev = st.receipts[key]
            if prev["hash"] == receipt_hash:
                return {"repeat": True, "tx": prev["tx"], "applied": [], "failed": [], "refs": {}}
            raise AtlasError("Конфликт протокола: ключ «%s» уже использован для другого содержимого "
                             "(запись №%d). Ничего не изменено." % (key, prev["tx"]))
        tx_no = st.last_tx + 1
        groups = []
        index = {}
        whole = bool(receipt.get("all_or_nothing"))
        for i, op in enumerate(receipt["ops"]):
            gname = "__all__" if whole else ((op.get("group") if isinstance(op, dict) else None) or "__op%d" % i)
            if gname not in index:
                index[gname] = len(groups)
                groups.append((gname, []))
            groups[index[gname]][1].append((i, op))

        working = st
        refs = {}
        applied, failed, records_total = [], [], 0
        for gname, ops in groups:
            trial = working.clone()
            ap = Applier(trial, ctx, tx_no)
            ap.refs = dict(refs)
            lines = []
            error = None
            for i, op in ops:
                try:
                    lines.append((i, ap.apply(op)))
                except OpError as exc:
                    error = (i, op, str(exc))
                    break
            if error is None:
                working, refs = trial, ap.refs
                applied.extend(lines)
                continue
            i, op, msg = error
            failed.append({"index": i, "op": op.get("op") if isinstance(op, dict) else "?", "why": msg,
                           "group": None if gname.startswith("__") else gname})
            if whole:
                return {"repeat": False, "tx": None, "applied": [], "failed": failed, "refs": {}, "whole": True}
            if len(ops) > 1:
                # Keep independent observations from the failed group (E.1, A61).
                salvage = working.clone()
                sap = Applier(salvage, ctx, tx_no)
                sap.refs = dict(refs)
                kept = []
                for j, sop in ops:
                    if not isinstance(sop, dict) or sop.get("op") not in OBSERVATIONS:
                        if j != i:
                            failed.append({"index": j, "op": sop.get("op") if isinstance(sop, dict) else "?",
                                           "why": "не применено: группа «%s» применяется целиком" % gname.strip("_"),
                                           "group": gname})
                        continue
                    if any(isinstance(v, str) and v.startswith("@") and v[1:] not in refs for v in sop.values()):
                        continue
                    try:
                        kept.append((j, sap.apply(sop) + " (наблюдение сохранено отдельно)"))
                    except OpError as exc:
                        if j != i:
                            failed.append({"index": j, "op": sop.get("op"), "why": str(exc), "group": gname})
                if kept:
                    working, refs = salvage, sap.refs
                    applied.extend(kept)

        new_records = [r for r in working.records if r["_tx"] == tx_no]
        result = {"repeat": False, "tx": None, "applied": sorted(applied), "failed": failed, "refs": refs,
                  "snapshot": st.last_tx}
        if not new_records:
            return result
        if dry_run:
            result["dry_run"] = True
            return result
        clean = []
        for r in new_records:
            clean.append({k: v for k, v in r.items() if not k.startswith("_")})
        tx = {"tx": tx_no, "id": new_id("tx"), "at": ctx.now,
              "origin": {"agent": ctx.agent, "session": ctx.session, "receipt_key": key,
                         "receipt_hash": receipt_hash, "via": "write"},
              "records": clean}
        store.publish_transaction(tx)
        result["tx"] = tx_no
        result["snapshot"] = tx_no
        return result


def write_core(store, records, ctx, via="core"):
    """Records produced by the core itself (hooks, checks). Same write path."""
    with store.lock():
        st, problem = load_state(store)
        if problem:
            raise AtlasError("Записи повреждены (%s: %s)." % (problem["file"], problem["why"]))
        tx_no = st.last_tx + 1
        clean = []
        for kind, data, source in records:
            rec = {"id": new_id("rec"), "kind": kind, "data": data, "at": ctx.now,
                   "source": source or {"type": "core"}}
            clean.append(rec)
        tx = {"tx": tx_no, "id": new_id("tx"), "at": ctx.now,
              "origin": {"agent": ctx.agent, "session": ctx.session, "receipt_key": None,
                         "receipt_hash": None, "via": via},
              "records": clean}
        store.publish_transaction(tx)
        return tx_no
