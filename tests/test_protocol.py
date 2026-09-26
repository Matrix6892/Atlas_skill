"""Protocol checks from Appendix G of the spec (type «Протокол»/«Полномочия»)."""

import json
import os
import sys
import unittest

from helpers import ProjectCase, OWNER, views
from atlaskit.store import worktree_fingerprint


class WritePointTests(ProjectCase):
    def test_a09_repeat_key_does_not_duplicate(self):
        ops = [{"op": "note.learned", "text": "для входа нужен домен"}]
        first = self.ok(ops, key="k1")
        again = self.write(ops, key="k1")
        self.assertTrue(again["repeat"])
        self.assertEqual(again["tx"], first["tx"])
        st = self.state()
        self.assertEqual(sum(1 for n in st.notes.values() if n["text"] == "для входа нужен домен"), 1)

    def test_a60_same_key_other_content_is_conflict(self):
        self.ok([{"op": "note.learned", "text": "одно"}], key="k2")
        with self.assertRaises(Exception) as ctx:
            self.write([{"op": "note.learned", "text": "другое"}], key="k2")
        self.assertIn("Конфликт протокола", str(ctx.exception))

    def test_a10_damaged_transaction_stops_loading_and_writing(self):
        self.ok([{"op": "note.learned", "text": "раз"}])
        self.ok([{"op": "note.learned", "text": "два"}])
        files = self.store.tx_files()
        path = os.path.join(self.store.records_dir, files[-1])
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["records"][0]["data"]["text"] = "подменено"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        txs, problem = self.store.load_transactions()
        self.assertEqual(problem["file"], files[-1])
        self.assertEqual(len(txs), len(files) - 1)
        with self.assertRaises(Exception):
            self.write([{"op": "note.learned", "text": "три"}])

    def test_e3_gap_in_numbering_is_reported(self):
        self.ok([{"op": "note.learned", "text": "раз"}])
        self.ok([{"op": "note.learned", "text": "два"}])
        os.remove(os.path.join(self.store.records_dir, self.store.tx_files()[-2]))
        _, problem = self.store.load_transactions()
        self.assertIn("пропущен", problem["why"])

    def test_a61_observation_survives_conflicting_command(self):
        refs = self.login_topic()
        res = self.write([
            {"op": "note.progress", "thread": refs["t"], "text": "сделана кнопка", "group": "g"},
            {"op": "thread.state", "thread": refs["t"], "state": "released", "group": "g"},  # no reason → fails
        ])
        self.assertTrue(res["failed"])
        st = self.state()
        self.assertTrue(any(n["text"] == "сделана кнопка" for n in st.notes.values()))
        self.assertEqual(st.threads[refs["t"]]["state"], "active")

    def test_a37_all_or_nothing_mode(self):
        res = self.write([
            {"op": "note.learned", "text": "наблюдение"},
            {"op": "thread.state", "thread": "нет такой темы", "state": "paused"},
        ], all_or_nothing=True)
        self.assertIsNone(res["tx"])
        self.assertFalse(any(n["text"] == "наблюдение" for n in self.state().notes.values()))

    def test_a95_batch_independent_items(self):
        self.ok([{"op": "park.add", "text": "онбординг"}, {"op": "park.add", "text": "тёмная тема"}])
        res = self.write([
            {"op": "park.promote", "item": "онбординг", "source": OWNER},
            {"op": "park.release", "item": "нет такой идеи", "source": OWNER},
            {"op": "park.release", "item": "тёмная тема", "source": OWNER},
        ])
        self.assertEqual(len(res["failed"]), 1)
        st = self.state()
        statuses = {p["text"]: p["status"] for p in st.parking.values()}
        self.assertEqual(statuses, {"онбординг": "promoted", "тёмная тема": "released"})

    def test_a08_expect_revision_conflict(self):
        refs = self.login_topic()
        rev = self.state().threads[refs["t"]]["rev"]
        self.ok([{"op": "thread.update", "thread": refs["t"], "why": "без пароля"}])
        self.fails([{"op": "thread.state", "thread": refs["t"], "state": "paused", "expect": {refs["t"]: rev}}],
                   "изменился")

    def test_a57_dependency_changed_in_other_session(self):
        refs = self.login_topic()
        base = self.state().last_tx
        self.ok([{"op": "decision.decide", "text": "Телефон не делаем", "thread": refs["t"], "source": OWNER}],
                session="other")
        self.fails([{"op": "thread.state", "thread": refs["t"], "state": "paused"}],
                   "изменилось в другой сессии", base_snapshot=base, session="s1")

    def test_secrets_are_rejected(self):
        self.fails([{"op": "note.learned", "text": "ключ sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}], "секрет")


class ConsentTests(ProjectCase):
    def test_rule1_decision_needs_owner_words(self):
        self.fails([{"op": "decision.decide", "text": "Делаем оплату картой"}], "только по словам владельца")
        self.fails([{"op": "decision.decide", "text": "Делаем оплату картой", "source": {"quote": "  "}}],
                   "только по словам владельца")

    def test_a47_a91_reference_to_past_is_not_a_new_decision(self):
        self.ok([{"op": "decision.decide", "text": "Телефонную регистрацию не делаем", "source": OWNER}])
        self.fails([{"op": "decision.decide", "text": "телефонную регистрацию не делаем.",
                     "source": {"quote": "мы же решили не делать регистрацию по телефону"}}], "отсылка к прошлому")

    def test_a46_one_decision_one_card(self):
        res = self.ok([{"op": "decision.ask", "ref": "q", "question": "Куда вести после входа?",
                        "options": ["в профиль", "на главную"], "provisional": {"option": "в профиль"}}])
        self.fails([{"op": "decision.ask", "question": "Куда вести после входа?"}], "одна карточка")
        st = self.state()
        cards = [n for n in st.needs() if n["type"] == "decision"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["obj"]["provisional"]["option"], "в профиль")

    def test_a32_a52_no_provisional_or_delegation_with_risk(self):
        self.fails([{"op": "decision.ask", "question": "Платный сервис за 10 $?", "risk": "money",
                     "provisional": {"option": "да"}}], "временный выбор")
        res = self.ok([{"op": "decision.ask", "ref": "q", "question": "Платный сервис за 10 $?", "risk": "money",
                        "options": ["да", "нет"]}])
        q = res["refs"]["q"]
        self.fails([{"op": "decision.delegate", "decision": q, "source": {"quote": "реши сам"}}], "не распространяется")
        self.fails([{"op": "decision.decide", "decision": q, "option": "да", "delegated": True}])
        st = self.state()
        self.assertEqual(st.needs()[0]["prio"], 0)  # risky first in the queue (C.3)

    def test_delegation_within_normal_risk(self):
        res = self.ok([{"op": "decision.ask", "ref": "q", "question": "Как назвать кнопку?", "options": ["Войти", "Вход"]}])
        q = res["refs"]["q"]
        self.fails([{"op": "decision.decide", "decision": q, "option": "Войти", "delegated": True}], "не передавал")
        self.ok([{"op": "decision.delegate", "decision": q, "source": {"quote": "реши сам"}}])
        self.ok([{"op": "decision.decide", "decision": "вопрос 1", "option": 1, "delegated": True}])
        d = self.state().decisions[q]
        self.assertEqual(d["status"], "decided")
        self.assertEqual(d["decided_source"]["type"], "delegated")

    def test_numbers_are_not_reused(self):
        res = self.ok([{"op": "decision.ask", "ref": "a", "question": "Первый?"}])
        self.ok([{"op": "decision.withdraw", "decision": res["refs"]["a"], "reason": "не нужен"}])
        res2 = self.ok([{"op": "decision.ask", "ref": "b", "question": "Второй?"}])
        self.assertEqual(self.state().decisions[res2["refs"]["b"]]["number"], 2)

    def test_a88_rule_not_active_until_confirmed(self):
        res = self.ok([{"op": "rule.propose", "ref": "r", "text": "названия кнопок решаю сам"}])
        st = self.state()
        self.assertEqual(st.active_rules(), [])
        self.assertEqual(st.proposed_rules()[0]["id"], res["refs"]["r"])
        self.fails([{"op": "rule.confirm", "rule": res["refs"]["r"]}], "только по словам владельца")
        self.ok([{"op": "rule.confirm", "rule": res["refs"]["r"], "source": {"quote": "да, так всегда"}}])
        self.assertEqual(len(self.state().active_rules()), 1)

    def test_frame_needs_owner_ok(self):
        self.fails([{"op": "frame.agree", "doing": "сохранение", "stop_when": "после двух подходов"}],
                   "только по словам владельца")
        self.ok([{"op": "frame.agree", "doing": "сохранение", "stop_when": "после двух подходов",
                  "source": {"quote": "ок"}}])


class TrustTests(ProjectCase):
    def test_a11_said_is_not_verified(self):
        refs = self.login_topic()
        self.present(refs)
        st = self.state()
        t = st.threads[refs["t"]]
        self.assertEqual(views.trust_line_thread(st, t), "сказано · проверено 0 из 3 · не принято")
        self.assertEqual(st.view_state(t), "presented")

    def test_a44_agent_cannot_claim_observed_run(self):
        refs = self.login_topic()
        self.present(refs)
        res = self.ok([{"op": "verify", "thread": refs["t"], "criteria": [refs["c1"]], "method": "automated_run",
                        "outcome": "passed", "trusted": True}])
        st = self.state()
        v = list(st.verifications.values())[0]
        self.assertEqual(v["method"], "agent_report")
        self.assertTrue(v["downgraded"])
        self.assertIn("проверено 0 из", views.trust_line_thread(st, st.threads[refs["t"]]))

    def test_observed_run_counts_as_verified(self):
        refs = self.login_topic()
        self.present(refs)
        code, out = self.cli("--session", "s1", "check", "--thread", refs["t"], "--criteria", refs["c1"],
                             "--", sys.executable, "-c", "print('ok')")
        self.assertEqual(code, 0, out)
        st = self.state()
        self.assertEqual(views.trust_line_thread(st, st.threads[refs["t"]]), "сказано · проверено 1 из 3 · не принято")
        r = st.latest_result(refs["t"])
        self.assertIn("проверено 1 из 2, тестом на компьютере", views.trust_line_result(st, r))

    def test_observed_failure_returns_topic_to_work(self):
        refs = self.login_topic()
        self.present(refs)
        code, _ = self.cli("check", "--thread", refs["t"], "--criteria", refs["c1"], "--",
                           sys.executable, "-c", "import sys; sys.exit(3)")
        self.assertEqual(code, 3)
        st = self.state()
        self.assertEqual(st.view_state(st.threads[refs["t"]]), "active")

    def test_a12_partial_verification_visible(self):
        refs = self.login_topic()
        self.present(refs, criteria=("c1", "c2", "c3"))
        self.ok([{"op": "verify", "thread": refs["t"], "criteria": [refs["c1"], refs["c3"]], "method": "owner_manual",
                  "outcome": "passed", "source": {"quote": "проверил, работает"}}])
        st = self.state()
        self.assertEqual(views.trust_line_thread(st, st.threads[refs["t"]]), "сказано · проверено 2 из 3 · не принято")

    def test_a13_acceptance_without_check_does_not_raise_verification(self):
        refs = self.login_topic()
        self.present(refs)
        self.ok([{"op": "accept", "thread": refs["t"], "purpose": "показа", "outcome": "accepted",
                  "source": {"quote": "принимаю для показа без проверки"}}])
        st = self.state()
        line = views.trust_line_thread(st, st.threads[refs["t"]])
        self.assertIn("проверено 0 из 3", line)
        self.assertIn("принято для показа", line)
        self.assertEqual(st.view_state(st.threads[refs["t"]]), "active")

    def test_a42_a89_verified_but_not_accepted(self):
        refs = self.login_topic()
        self.present(refs)
        self.ok([
            {"op": "verify", "thread": refs["t"], "method": "owner_manual", "outcome": "passed",
             "source": {"quote": "проверил, работает, но дизайн не принимаю"}},
            {"op": "accept", "thread": refs["t"], "outcome": "rejected", "rejection": {"class": "new_wish", "reason": "дизайн"},
             "source": {"quote": "проверил, работает, но дизайн не принимаю"}},
        ])
        st = self.state()
        self.assertEqual(len(st.verifications), 1)
        self.assertEqual(list(st.acceptances.values())[0]["outcome"], "rejected")
        self.assertIn("не принято: новое пожелание", views.trust_line_thread(st, st.threads[refs["t"]]))

    def test_a43_acceptance_scope_is_limited_to_version(self):
        refs = self.login_topic()
        self.present(refs)  # c1, c3
        self.fails([{"op": "accept", "thread": refs["t"], "criteria": [refs["c2"]], "purpose": "показа",
                     "outcome": "accepted", "source": OWNER}], "не входит в эту версию")
        self.ok([{"op": "accept", "thread": refs["t"], "criteria": [refs["c1"]], "purpose": "показа",
                  "outcome": "accepted", "source": OWNER}])
        st = self.state()
        self.assertIn("принято частично (1 из 2)", views.trust_line_thread(st, st.threads[refs["t"]]))
        self.assertEqual(st.view_state(st.threads[refs["t"]]), "presented")

    def test_a14_new_version_does_not_inherit_checks(self):
        refs = self.login_topic()
        self.present(refs)
        self.ok([{"op": "verify", "thread": refs["t"], "method": "owner_manual", "outcome": "passed", "source": OWNER},
                 {"op": "accept", "thread": refs["t"], "purpose": "показа", "outcome": "accepted", "source": OWNER}])
        self.touch("app.py", "print('r2')\n")
        r2 = self.present(refs)
        st = self.state()
        r = st.results[r2]
        self.assertEqual(r["version"], 2)
        self.assertEqual(views.trust_line_result(st, r), "сказано агентом · проверено 0 из 2 · не принято")
        old = st.results_of(refs["t"])[0]
        self.fails([{"op": "accept", "result": old["id"], "purpose": "показа", "outcome": "accepted", "source": OWNER}],
                   "сейчас уже r2")

    def test_a90_could_not_check_is_not_failure(self):
        refs = self.login_topic()
        self.present(refs)
        self.ok([{"op": "verify", "thread": refs["t"], "method": "owner_manual", "outcome": "could_not_check",
                  "source": {"quote": "не смог запустить"}}])
        st = self.state()
        self.assertEqual(st.view_state(st.threads[refs["t"]]), "presented")
        self.assertFalse(st.result_failed(st.latest_result(refs["t"])))

    def test_b5_code_changed_after_presenting(self):
        refs = self.login_topic()
        self.present(refs)
        self.touch("app.py", "changed\n")
        self.ok([{"op": "verify", "thread": refs["t"], "criteria": [refs["c1"]], "method": "owner_manual",
                  "outcome": "passed", "source": OWNER}])
        st = self.state()
        chk = st.criterion_check(st.latest_result(refs["t"]), {"id": refs["c1"], "scope_rev": 1})
        self.assertEqual(chk["status"], "applicability_unknown")

    def test_substantive_criterion_revision_invalidates_check(self):
        refs = self.login_topic()
        self.present(refs)
        self.ok([{"op": "verify", "thread": refs["t"], "criteria": [refs["c1"]], "method": "owner_manual",
                  "outcome": "passed", "source": OWNER}])
        self.ok([{"op": "criterion.revise", "criterion": refs["c1"], "text": "Входит и видит имя и фото",
                  "substantive": True}])
        st = self.state()
        r = st.latest_result(refs["t"])
        self.assertEqual(st.criterion_check(r, r["criteria"][0])["status"], "changed")

    def test_a45_two_dirty_states_differ(self):
        self.touch("a.txt", "one\n")
        fp1 = worktree_fingerprint(self.project)
        self.touch("a.txt", "two\n")
        fp2 = worktree_fingerprint(self.project)
        self.assertNotEqual(fp1["hash"], fp2["hash"])
        self.sh("git", "add", "-A")
        self.sh("git", "commit", "-qm", "x")
        self.assertEqual(worktree_fingerprint(self.project)["hash"], fp2["hash"])  # same content, new commit
        self.touch("__pycache__/app.cpython-311.pyc", "bytecode")  # a test run leaves caches behind
        self.touch(".pytest_cache/v/cache/lastfailed", "{}")
        self.assertEqual(worktree_fingerprint(self.project)["hash"], fp2["hash"])


class ThreadTests(ProjectCase):
    def test_wip_limit_and_override(self):
        for name in ("А", "Б", "В"):
            self.ok([{"op": "thread.create", "title": "Тема " + name}])
        self.fails([{"op": "thread.create", "title": "Тема Г"}], "в работе уже 3 из 3")
        self.fails([{"op": "thread.create", "title": "Тема Г", "over_limit": True}], "только по словам владельца")
        self.ok([{"op": "thread.create", "title": "Тема Г", "over_limit": True, "source": {"quote": "бери четвёртую"}}])

    def test_a84_presented_topic_not_counted_in_wip(self):
        refs = self.login_topic()
        self.present(refs)
        for name in ("А", "Б", "В"):
            self.ok([{"op": "thread.create", "title": "Тема " + name}])
        self.assertEqual(self.state().wip_count(), 3)

    def test_a21_release_needs_reason_and_keeps_history(self):
        refs = self.login_topic()
        self.fails([{"op": "thread.state", "thread": refs["t"], "state": "released", "source": OWNER}], "причин")
        self.ok([{"op": "thread.state", "thread": refs["t"], "state": "released", "reason": "не для беты",
                  "source": OWNER}])
        self.fails([{"op": "thread.state", "thread": refs["t"], "state": "active"}])
        self.ok([{"op": "thread.state", "thread": refs["t"], "state": "planned", "reason": "передумали",
                  "source": OWNER}])
        st = self.state()
        kinds = [e["kind"] for e in st.thread_events(refs["t"])]
        self.assertEqual(kinds.count("thread.state"), 2)

    def test_candidate_needs_owner_confirmation(self):
        res = self.ok([{"op": "thread.create", "ref": "c", "title": "Оплата", "state": "candidate"}])
        self.fails([{"op": "thread.state", "thread": res["refs"]["c"], "state": "planned"}], "владельца")

    def test_ambiguous_title_is_not_guessed(self):
        self.ok([{"op": "thread.create", "title": "Вход"}, {"op": "thread.create", "title": "Вход с телефона",
                                                              "state": "planned"}])
        self.ok([{"op": "note.progress", "thread": "Вход", "text": "точное совпадение"}])
        self.fails([{"op": "note.progress", "thread": "Выход", "text": "нет такой"}], "не найдена")


class CorrectionTests(ProjectCase):
    def test_a98_a63_corrected_decision_disappears_from_views(self):
        self.ok([{"op": "decision.decide", "text": "Телефонную регистрацию не делаем", "source": OWNER}])
        res = self.ok([{"op": "decision.decide", "ref": "bad", "text": "Телефон не нужен вообще",
                        "source": {"quote": "мы же решили без телефона?"}}])
        bad = res["refs"]["bad"]
        self.ok([{"op": "correct", "target": bad, "action": "link", "link_to": 1,
                  "was": "новое решение", "becomes": "ссылка на решение 1",
                  "source": {"quote": "ты неверно понял"}}])
        st = self.state()
        self.assertEqual([d["number"] for d in st.decided()], [1])
        code, out = self.cli("brief")
        self.assertNotIn("Телефон не нужен вообще", out)
        self.assertIn(bad, st.errors)
        self.fails([{"op": "correct", "target": bad, "action": "withdraw", "was": "x", "becomes": "y"}], "уже исправлено")

    def test_correction_edit_keeps_history(self):
        res = self.ok([{"op": "park.add", "ref": "p", "text": "тёмна тема"}])
        self.ok([{"op": "correct", "target": res["refs"]["p"], "action": "edit", "edit": {"text": "тёмная тема"},
                  "was": "тёмна тема", "becomes": "тёмная тема"}])
        st = self.state()
        self.assertEqual(st.parking[res["refs"]["p"]]["text"], "тёмная тема")
        self.assertEqual(len(st.corrections), 1)


if __name__ == "__main__":
    unittest.main()
