"""Fold accepted transactions into the current state and derive views.

One recorded state, several projections (spec E.5). Nothing here writes.
Derived notions follow the spec:
  * `presented` is derived from the presentation axis, never stored (A.3);
  * verification, acceptance and presentation are independent axes (B.1);
  * a verification counts only for the result version it was bound to (B.5);
  * the "Нужно от вас" queue keeps no copy of choices (C.3).
"""

import copy

from . import ru

TRUSTED_METHODS = ("automated_run", "owner_manual", "external")
WORK_STATES = ("candidate", "planned", "active", "paused", "frozen", "released")
ACCEPT_DONE = ("accepted", "accepted_with_caveat", "rejected")


class State:
    def __init__(self):
        self.project_name = None
        self.goals = []
        self.settings = {"wip_limit": 3, "stale_parking_days": 14}
        self.threads = {}
        self.criteria = {}
        self.decisions = {}
        self.parking = {}
        self.results = {}
        self.verifications = {}
        self.acceptances = {}
        self.frames = {}
        self.rules = {}
        self.notes = {}
        self.attempts = {}
        self.bookmarks = []
        self.corrections = []
        self.sessions = {}
        self.gaps = {}
        self.recoveries = []
        self.redactions = []
        self.receipts = {}
        self.checkpoints = {}
        self.records = []
        self.errors = set()
        self.last_tx = 0
        self.decision_counter = 0

    # ------------------------------------------------------------------
    @classmethod
    def from_transactions(cls, txs):
        st = cls()
        for tx in txs:
            st.apply_tx(tx)
        return st

    def clone(self):
        return copy.deepcopy(self)

    def apply_tx(self, tx):
        origin = tx.get("origin", {})
        key = origin.get("receipt_key")
        if key:
            self.receipts[key] = {"hash": origin.get("receipt_hash"), "tx": tx["tx"],
                                  "outcome": origin.get("outcome")}
        sid = origin.get("session")
        if sid and origin.get("via") in ("write", "check"):
            # Last confirmed checkpoint of a session and the files it covered
            # (capture boundary, R09): a later change without a record is a tail.
            self.checkpoints[sid] = {"tx": tx["tx"], "fingerprint": origin.get("fingerprint")}
        for rec in tx.get("records", []):
            meta = {
                "tx": tx["tx"],
                "at": rec.get("at") or tx.get("at"),
                "agent": origin.get("agent"),
                "session": origin.get("session"),
            }
            self.apply_record(rec, meta)
        self.last_tx = max(self.last_tx, tx["tx"])

    def apply_record(self, rec, meta):
        entry = dict(rec)
        entry.update({"_tx": meta["tx"], "_at": meta["at"], "_agent": meta["agent"], "_session": meta["session"]})
        self.records.append(entry)
        handler = getattr(self, "_on_" + rec["kind"].replace(".", "_"), None)
        if handler:
            handler(rec.get("data", {}), rec, meta)

    # ----- helpers ------------------------------------------------------
    def _touch(self, obj, meta):
        obj["rev"] = obj.get("rev", 0) + 1
        obj["last_tx"] = meta["tx"]
        obj["last_session"] = meta["session"]
        obj["updated_at"] = meta["at"]

    def _new(self, obj, rec, meta):
        obj.update({
            "rev": 1,
            "created_at": meta["at"],
            "created_tx": meta["tx"],
            "created_by": meta["agent"],
            "created_session": meta["session"],
            "last_tx": meta["tx"],
            "last_session": meta["session"],
            "updated_at": meta["at"],
            "source": rec.get("source") or {"type": "agent"},
            "record_id": rec.get("id"),
        })
        return obj

    # ----- project ------------------------------------------------------
    def _on_project_init(self, d, rec, meta):
        self.project_name = d.get("name")

    def _on_project_rename(self, d, rec, meta):
        self.project_name = d.get("name")

    def _on_goal_set(self, d, rec, meta):
        for g in self.goals:
            g["current"] = False
        self.goals.append(self._new({
            "id": d["goal_id"], "text": d["text"], "scenario": d.get("scenario"),
            "version": len(self.goals) + 1, "current": True, "reason": d.get("reason"),
        }, rec, meta))

    def _on_settings_set(self, d, rec, meta):
        self.settings[d["key"]] = d["value"]

    # ----- threads ------------------------------------------------------
    def _on_thread_create(self, d, rec, meta):
        self.threads[d["thread_id"]] = self._new({
            "id": d["thread_id"], "title": d["title"], "why": d.get("why"),
            "state": d.get("state", "active"), "leads_to_goal": d.get("leads_to_goal"),
            "parent": d.get("parent"), "from_parking": d.get("from_parking"),
            "state_reason": d.get("reason"), "criteria": [], "progress_at": meta["at"],
        }, rec, meta)
        if d.get("parent") in self.threads:
            self.threads[d["parent"]].setdefault("children", []).append(d["thread_id"])

    def _on_thread_state(self, d, rec, meta):
        t = self.threads.get(d["thread_id"])
        if not t:
            return
        t["prev_state"] = t["state"]
        t["state"] = d["state"]
        t["state_reason"] = d.get("reason")
        t["state_at"] = meta["at"]
        if d.get("over_limit"):
            t["over_limit"] = True
        self._touch(t, meta)

    def _on_thread_update(self, d, rec, meta):
        t = self.threads.get(d["thread_id"])
        if not t:
            return
        for k in ("title", "why", "leads_to_goal"):
            if k in d:
                t[k] = d[k]
        self._touch(t, meta)

    # ----- criteria -----------------------------------------------------
    def _on_criterion_add(self, d, rec, meta):
        self.criteria[d["criterion_id"]] = self._new({
            "id": d["criterion_id"], "thread_id": d["thread_id"], "text": d["text"],
            "observable": d.get("observable", True), "how": d.get("how"),
            "scope_rev": 1, "withdrawn": False,
        }, rec, meta)
        t = self.threads.get(d["thread_id"])
        if t is not None:
            t["criteria"].append(d["criterion_id"])

    def _on_criterion_revise(self, d, rec, meta):
        c = self.criteria.get(d["criterion_id"])
        if not c:
            return
        c["text"] = d.get("text", c["text"])
        if d.get("substantive"):
            c["scope_rev"] += 1
        self._touch(c, meta)

    def _on_criterion_withdraw(self, d, rec, meta):
        c = self.criteria.get(d["criterion_id"])
        if c:
            c["withdrawn"] = True
            c["withdraw_reason"] = d.get("reason")
            self._touch(c, meta)

    # ----- notes and attempts (observations) ---------------------------
    def _note(self, kind, d, rec, meta):
        self.notes[d["note_id"]] = self._new({
            "id": d["note_id"], "kind": kind, "thread_id": d.get("thread_id"), "text": d["text"],
            "resolved": False,
        }, rec, meta)
        t = self.threads.get(d.get("thread_id"))
        if t is not None and kind == "progress":
            t["progress_at"] = meta["at"]

    def _on_note_progress(self, d, rec, meta):
        self._note("progress", d, rec, meta)

    def _on_note_learned(self, d, rec, meta):
        self._note("learned", d, rec, meta)

    def _on_note_debt(self, d, rec, meta):
        self._note("debt", d, rec, meta)

    def _on_note_resolve(self, d, rec, meta):
        n = self.notes.get(d["note_id"])
        if n:
            n["resolved"] = True
            n["resolution"] = d.get("text")
            self._touch(n, meta)

    def _on_attempt(self, d, rec, meta):
        self.attempts[d["attempt_id"]] = self._new({
            "id": d["attempt_id"], "thread_id": d["thread_id"], "criterion_id": d.get("criterion_id"),
            "problem": d.get("problem"),
            "approach": d["approach"], "outcome": d["outcome"], "new_approach": d.get("new_approach", True),
            "learned": d.get("learned"),
        }, rec, meta)

    # ----- decisions ----------------------------------------------------
    def _on_decision_ask(self, d, rec, meta):
        self.decision_counter = max(self.decision_counter, d["number"])
        self.decisions[d["decision_id"]] = self._new({
            "id": d["decision_id"], "number": d["number"], "status": "proposed",
            "question": d["question"], "text": None, "thread_id": d.get("thread_id"),
            "criterion_id": d.get("criterion_id"),
            "options": d.get("options", []), "advice": d.get("advice"),
            "provisional": d.get("provisional"), "urgency": d.get("urgency", "normal"),
            "deadline": d.get("deadline"), "can_defer": d.get("can_defer", True),
            "risk": d.get("risk", "normal"), "deferred": False, "delegated": False,
            "acks": {}, "applied": [],
        }, rec, meta)

    def _on_decision_provisional(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["provisional"] = {"option": d.get("option"), "text": d.get("text"), "until": d.get("until")}
            self._touch(dec, meta)

    def _on_decision_decide(self, d, rec, meta):
        did = d["decision_id"]
        if d.get("created"):
            self.decision_counter = max(self.decision_counter, d["number"])
            self.decisions[did] = self._new({
                "id": did, "number": d["number"], "status": "decided", "question": d.get("question"),
                "text": d["text"], "reason": d.get("reason"), "thread_id": d.get("thread_id"),
                "options": [], "risk": d.get("risk", "normal"), "deferred": False,
                "delegated": d.get("delegated", False), "decided_at": meta["at"],
                "decided_source": rec.get("source"), "acks": {}, "applied": [],
                "supersedes": d.get("supersedes"), "decided_agent": meta["agent"],
                "decided_session": meta["session"],
            }, rec, meta)
            return
        dec = self.decisions.get(did)
        if not dec:
            return
        dec["status"] = "decided"
        dec["text"] = d["text"]
        dec["option"] = d.get("option")
        dec["reason"] = d.get("reason")
        dec["decided_at"] = meta["at"]
        dec["decided_source"] = rec.get("source")
        dec["decided_agent"] = meta["agent"]
        dec["decided_session"] = meta["session"]
        dec["delegated"] = d.get("delegated", dec.get("delegated", False))
        dec["deferred"] = False
        self._touch(dec, meta)

    def _on_decision_delegate(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["delegated"] = True
            dec["delegation_source"] = rec.get("source")
            self._touch(dec, meta)

    def _on_decision_defer(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["deferred"] = True
            self._touch(dec, meta)

    def _on_decision_supersede(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["status"] = "superseded"
            dec["superseded_by"] = d["new_decision_id"]
            dec["status_reason"] = d.get("reason")
            self._touch(dec, meta)

    def _on_decision_withdraw(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["status"] = "withdrawn"
            dec["status_reason"] = d.get("reason")
            self._touch(dec, meta)

    def _on_decision_ack(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["acks"][meta["session"] or "?"] = {"rev": d.get("rev"), "agent": meta["agent"], "at": meta["at"]}

    def _on_decision_applied(self, d, rec, meta):
        dec = self.decisions.get(d["decision_id"])
        if dec:
            dec["applied"].append({"text": d.get("text"), "agent": meta["agent"], "session": meta["session"], "at": meta["at"]})

    # ----- parking ------------------------------------------------------
    def _on_park_add(self, d, rec, meta):
        self.parking[d["item_id"]] = self._new({
            "id": d["item_id"], "text": d["text"], "kind": d.get("kind", "idea"),
            "context": d.get("context"), "quote": d.get("quote"),
            "goal_related": d.get("goal_related"), "status": "parked",
        }, rec, meta)

    def _on_park_classify(self, d, rec, meta):
        p = self.parking.get(d["item_id"])
        if p:
            p["goal_related"] = d.get("goal_related")
            self._touch(p, meta)

    def _on_park_promote(self, d, rec, meta):
        p = self.parking.get(d["item_id"])
        if p:
            p["status"] = "promoted"
            p["thread_id"] = d["thread_id"]
            self._touch(p, meta)

    def _on_park_release(self, d, rec, meta):
        p = self.parking.get(d["item_id"])
        if p:
            p["status"] = "released"
            p["status_reason"] = d.get("reason")
            self._touch(p, meta)

    def _on_park_keep(self, d, rec, meta):
        p = self.parking.get(d["item_id"])
        if p:
            p["kept_at"] = meta["at"]
            self._touch(p, meta)

    def _on_park_restore(self, d, rec, meta):
        p = self.parking.get(d["item_id"])
        if p:
            p["status"] = "parked"
            self._touch(p, meta)

    # ----- results, verification, acceptance ---------------------------
    def _on_result_present(self, d, rec, meta):
        self.results[d["result_id"]] = self._new({
            "id": d["result_id"], "thread_id": d["thread_id"], "version": d["version"],
            "criteria": d["criteria"], "summary": d["summary"], "excluded": d.get("excluded", []),
            "fit_for": d.get("fit_for"), "not_fit_for": d.get("not_fit_for"),
            "caveats": d.get("caveats", []), "claim": d.get("claim"), "check": d.get("check"),
            "fingerprint": d.get("fingerprint"), "withdrawn": False,
        }, rec, meta)

    def _on_result_withdraw(self, d, rec, meta):
        r = self.results.get(d["result_id"])
        if r:
            r["withdrawn"] = True
            r["withdraw_reason"] = d.get("reason")
            self._touch(r, meta)

    def _on_verify(self, d, rec, meta):
        self.verifications[d["verification_id"]] = self._new(dict(d, id=d["verification_id"]), rec, meta)

    def _on_accept(self, d, rec, meta):
        self.acceptances[d["acceptance_id"]] = self._new(dict(d, id=d["acceptance_id"]), rec, meta)

    # ----- frames and rules ---------------------------------------------
    def _on_frame_agree(self, d, rec, meta):
        for f in self.frames.values():
            if f["status"] == "active" and f.get("thread_id") == d.get("thread_id") and not f.get("standing"):
                f["status"] = "closed"
                f["close_reason"] = "заменена новой договорённостью"
        self.frames[d["frame_id"]] = self._new(dict(d, id=d["frame_id"], status="active"), rec, meta)

    def _on_frame_close(self, d, rec, meta):
        f = self.frames.get(d["frame_id"])
        if f:
            f["status"] = "closed"
            f["close_reason"] = d.get("reason")
            self._touch(f, meta)

    def _on_rule_propose(self, d, rec, meta):
        self.rules[d["rule_id"]] = self._new({"id": d["rule_id"], "text": d["text"], "status": "proposed"}, rec, meta)

    def _on_rule_confirm(self, d, rec, meta):
        r = self.rules.get(d["rule_id"])
        if r:
            r["status"] = "active"
            r["confirmed_source"] = rec.get("source")
            self._touch(r, meta)

    def _on_rule_revoke(self, d, rec, meta):
        r = self.rules.get(d["rule_id"])
        if r:
            r["status"] = "revoked"
            self._touch(r, meta)

    # ----- bookmark, corrections, sessions -----------------------------
    def _on_bookmark_save(self, d, rec, meta):
        self.bookmarks.append(self._new(dict(d, id=d["bookmark_id"]), rec, meta))

    def _on_correct(self, d, rec, meta):
        self.corrections.append(self._new(dict(d, id=d["correction_id"]), rec, meta))
        target = d["target"]
        action = d["action"]
        if action in ("withdraw", "link"):
            self.errors.add(target)
            obj = self.lookup(target)
            if obj is not None:
                obj["error"] = True
                obj["corrected_by"] = d["correction_id"]
                if action == "link":
                    obj["linked_to"] = d.get("link_to")
        elif action == "edit":
            obj = self.lookup(target)
            if obj is not None:
                for k, v in (d.get("edit") or {}).items():
                    if isinstance(v, str):
                        obj[k] = v
                obj["corrected_by"] = d["correction_id"]
                obj["corrected_source"] = rec.get("source")
                if d.get("substantive") and "scope_rev" in obj:
                    obj["scope_rev"] += 1
                self._touch(obj, meta)

    def _on_session_start(self, d, rec, meta):
        s = self.sessions.setdefault(d["session"], {"id": d["session"], "agent": d.get("agent"), "starts": []})
        s["starts"].append({"at": meta["at"], "source": d.get("source"), "mode": d.get("mode")})

    def _on_capture_gap(self, d, rec, meta):
        self.gaps[d["gap_id"]] = self._new(dict(d, id=d["gap_id"], resolved=False), rec, meta)

    def _on_capture_recovered(self, d, rec, meta):
        self.recoveries.append(self._new(dict(d), rec, meta))
        g = self.gaps.get(d.get("gap_id"))
        if g:
            g["resolved"] = True

    def _on_redaction(self, d, rec, meta):
        self.redactions.append(dict(d, at=meta["at"]))

    # ------------------------------------------------------------------
    # lookups
    def lookup(self, oid):
        for table in (self.threads, self.criteria, self.decisions, self.parking, self.results,
                      self.verifications, self.acceptances, self.frames, self.rules, self.notes,
                      self.attempts, self.gaps):
            if oid in table:
                return table[oid]
        for g in self.goals:
            if g["id"] == oid:
                return g
        for b in self.bookmarks:
            if b["id"] == oid:
                return b
        return None

    def alive(self, obj):
        return obj is not None and not obj.get("error")

    def goal(self):
        for g in reversed(self.goals):
            if g.get("current") and not g.get("error"):
                return g
        return None

    def decision_by_number(self, number):
        for d in self.decisions.values():
            if d["number"] == number:
                return d
        return None

    # ----- threads ------------------------------------------------------
    def live_threads(self):
        return [t for t in self.threads.values() if not t.get("error")]

    def thread_criteria(self, t):
        return [self.criteria[c] for c in t["criteria"]
                if c in self.criteria and not self.criteria[c]["withdrawn"] and not self.criteria[c].get("error")]

    def results_of(self, thread_id):
        rs = [r for r in self.results.values()
              if r["thread_id"] == thread_id and not r.get("withdrawn") and not r.get("error")]
        return sorted(rs, key=lambda r: r["version"])

    def latest_result(self, thread_id):
        rs = self.results_of(thread_id)
        return rs[-1] if rs else None

    def max_version(self, thread_id):
        vs = [r["version"] for r in self.results.values() if r["thread_id"] == thread_id]
        return max(vs) if vs else 0

    def view_state(self, t):
        """Stored state, except two states derived from the latest result:
        `presented` (waiting for the owner) and `closed` (everything agreed
        is accepted — R14). Neither is stored; both come from the axes."""
        if t["state"] == "active":
            r = self.latest_result(t["id"])
            if r and self.result_awaiting(r):
                return "presented"
            if r and self.scope_closed(t, r):
                return "closed"
        return t["state"]

    def scope_closed(self, t, r):
        """The agreed scope is closed: every current readiness condition of
        the topic is in the latest version and accepted (not rejected), and no
        question about the topic is open. A new condition or a rejection
        reopens the work. Acceptance still says nothing about checks (B.1)."""
        crits = self.thread_criteria(t)
        if not crits or r.get("withdrawn") or self.result_failed(r):
            return False
        in_version = {c["id"] for c in r["criteria"]}
        for c in crits:
            if c["id"] not in in_version:
                return False
            acc = self.criterion_acceptance(r, c["id"])
            if not acc or acc["outcome"] == "rejected":
                return False
        return not any(d.get("thread_id") == t["id"] for d in self.open_decisions())

    def wip_count(self, exclude=None):
        return sum(1 for t in self.live_threads()
                   if t["id"] != exclude and self.view_state(t) == "active")

    # ----- trust axes ---------------------------------------------------
    def verifications_for(self, result):
        vs = [v for v in self.verifications.values()
              if v.get("result_id") == result["id"] and not v.get("error")]
        return sorted(vs, key=lambda v: v["created_tx"])

    def acceptances_for(self, result):
        acc = [a for a in self.acceptances.values()
               if a.get("result_id") == result["id"] and not a.get("error")]
        return sorted(acc, key=lambda a: a["created_tx"])

    def criterion_check(self, result, crit_entry):
        """Status of one criterion on one result version.

        Returns dict(status, method, environment, at, detail) where status is
        passed | failed | could_not_check | applicability_unknown |
        agent_only | changed | none.
        """
        cid = crit_entry["id"]
        crit = self.criteria.get(cid)
        if crit and crit["scope_rev"] != crit_entry.get("scope_rev", 1):
            return {"status": "changed"}
        match = None
        mismatch = None
        agent_only = None
        rfp = (result.get("fingerprint") or {}).get("hash")
        for v in self.verifications_for(result):
            if cid not in [c["id"] for c in v.get("criteria", [])]:
                continue
            if v["method"] not in TRUSTED_METHODS:
                agent_only = v
                continue
            vfp = (v.get("fingerprint") or {}).get("hash")
            if v.get("changed_during_run") or (rfp and vfp and rfp != vfp):
                # The code changed after the result was presented: the
                # observation stays with the tested state (B.5).
                mismatch = v
                continue
            match = v
        if match is not None:
            return {"status": match["outcome"], "method": match["method"], "environment": match.get("environment"),
                    "at": match["created_at"], "v": match}
        if mismatch is not None:
            return {"status": "applicability_unknown", "method": mismatch["method"],
                    "environment": mismatch.get("environment"), "at": mismatch["created_at"], "v": mismatch}
        if agent_only is not None:
            return {"status": "agent_only", "method": agent_only["method"], "at": agent_only["created_at"], "v": agent_only}
        return {"status": "none"}

    def criterion_acceptance(self, result, cid):
        state = None
        for a in self.acceptances_for(result):
            if cid in a.get("criteria", []):
                state = a
        return state

    def result_trust(self, result):
        rows = []
        for ce in result["criteria"]:
            crit = self.criteria.get(ce["id"])
            rows.append({
                "criterion": crit,
                "check": self.criterion_check(result, ce),
                "acceptance": self.criterion_acceptance(result, ce["id"]),
            })
        return rows

    def result_failed(self, result):
        return any(r["check"]["status"] == "failed" for r in self.result_trust(result))

    def result_awaiting(self, result):
        if result.get("withdrawn") or result.get("error"):
            return False
        trust = self.result_trust(result)
        if any(r["check"]["status"] == "failed" for r in trust):
            return False
        if any(r["acceptance"] and r["acceptance"]["outcome"] == "rejected" for r in trust):
            return False
        return any(r["acceptance"] is None for r in trust)

    # ----- decisions ----------------------------------------------------
    def open_decisions(self):
        return [d for d in self.decisions.values()
                if d["status"] == "proposed" and not d.get("error")]

    def decided(self):
        return [d for d in self.decisions.values()
                if d["status"] == "decided" and not d.get("error")]

    def thread_decisions(self, thread_id):
        return [d for d in self.decisions.values()
                if d.get("thread_id") == thread_id and not d.get("error") and d["status"] in ("decided", "proposed")]

    # ----- parking ------------------------------------------------------
    def parked(self):
        return sorted([p for p in self.parking.values() if p["status"] == "parked" and not p.get("error")],
                      key=lambda p: p["created_tx"])

    # ----- frames and rules ---------------------------------------------
    def active_frames(self):
        return [f for f in self.frames.values() if f["status"] == "active" and not f.get("error")]

    def active_rules(self):
        return [r for r in self.rules.values() if r["status"] == "active" and not r.get("error")]

    def proposed_rules(self):
        return [r for r in self.rules.values() if r["status"] == "proposed" and not r.get("error")]

    # ----- bookmarks ----------------------------------------------------
    def last_bookmark(self):
        for b in reversed(self.bookmarks):
            if not b.get("error"):
                return b
        return None

    def open_gaps(self):
        return [g for g in self.gaps.values() if not g["resolved"] and not g.get("error")]

    # ----- records by thread (history) ---------------------------------
    def thread_events(self, thread_id):
        """Records that belong to a thread, for its history (Э2)."""
        events = []
        crit_ids = set(self.threads.get(thread_id, {}).get("criteria", []))
        result_ids = {r["id"] for r in self.results.values() if r["thread_id"] == thread_id}
        for rec in self.records:
            d = rec.get("data", {})
            if rec.get("id") in self.errors:
                continue
            related = (
                d.get("thread_id") == thread_id
                or d.get("criterion_id") in crit_ids
                or d.get("result_id") in result_ids
            )
            if related:
                obj_id = next((d[k] for k in ("note_id", "decision_id", "result_id", "verification_id",
                                              "acceptance_id", "attempt_id", "frame_id") if k in d), None)
                if obj_id and obj_id in self.errors:
                    continue
                events.append(rec)
        return events

    def sessions_after(self, tx, exclude_session=None):
        """Sessions that wrote records after transaction `tx`."""
        out = {}
        for rec in self.records:
            if rec["_tx"] <= tx or rec["kind"] in ("session.start", "capture.gap", "redaction"):
                continue
            sid = rec["_session"]
            if not sid or sid == exclude_session:
                continue
            out.setdefault(sid, {"agent": rec["_agent"], "records": []})["records"].append(rec)
        return out

    # ----- needs queue (C.3) -------------------------------------------
    def needs(self):
        """Ordered list of cards for «Нужно от вас»."""
        items = []
        for d in self.open_decisions():
            if d.get("risk", "normal") != "normal":
                prio = 0
            elif d.get("urgency") == "blocking":
                prio = 1
            elif d.get("deadline") or d.get("urgency") == "deadline":
                prio = 3
            else:
                prio = 4
            if d.get("deferred"):
                prio = 6
            items.append({"type": "decision", "prio": prio, "obj": d, "order": d["number"]})
        for t in self.live_threads():
            if self.view_state(t) != "presented":
                continue
            r = self.latest_result(t["id"])
            items.append({"type": "result", "prio": 2, "obj": r, "thread": t, "order": r["created_tx"]})
        for r in self.proposed_rules():
            items.append({"type": "rule", "prio": 5, "obj": r, "order": r["created_tx"]})
        items.sort(key=lambda i: (i["prio"], i["order"]))
        return items
