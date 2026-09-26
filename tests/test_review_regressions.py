"""Regressions from the external audit of commit ca49301 (Atlas_M0_audit_ca49301.md).

Test bodies are taken from the auditor's test_atlas_review_regressions.py
(R01–R13 and R11 of the audit); only the import scaffolding is adapted to
this repository. Extra tests for R14–R16 and a correction side door follow.
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import ProjectCase, OWNER
from atlaskit import views
from atlaskit.brief import build
from atlaskit.store import WriterLock, find_project, worktree_fingerprint


class ConsentAndEvidenceRegressionTests(ProjectCase):
    def test_01_correct_cannot_reverse_owner_decision_without_owner_source(self):
        original = "Публичную публикацию не делаем"
        refs = self.ok([{"op": "decision.decide", "ref": "d", "text": original, "source": OWNER}])["refs"]
        self.write([{"op": "correct", "target": refs["d"], "action": "edit",
                     "edit": {"text": "Публичная публикация разрешена"},
                     "was": original, "becomes": "Публичная публикация разрешена"}])
        self.assertEqual(self.state().decisions[refs["d"]]["text"], original,
                         "Generic correction must not bypass decision consent")

    def test_02_material_correction_cannot_inherit_old_passed_check(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        self.ok([{"op": "verify", "result": rid, "criteria": [refs["c1"]],
                  "method": "owner_manual", "outcome": "passed", "source": OWNER}])
        result = self.write([{"op": "correct", "target": refs["c1"], "action": "edit",
                              "edit": {"text": "Работает на всех поддерживаемых телефонах"},
                              "was": "Вход на компьютере", "becomes": "Вход на телефонах"}])
        st = self.state()
        if not result["failed"]:
            self.assertNotEqual(st.criterion_check(st.results[rid], st.results[rid]["criteria"][0])["status"],
                                "passed", "Accepted material correction must invalidate dependent evidence")

    def test_03_external_label_alone_cannot_raise_provenance(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        result = self.write([{"op": "verify", "result": rid, "criteria": [refs["c1"]],
                              "method": "external", "outcome": "passed",
                              "detail": "Сообщаю, что независимый проверяющий всё проверил"}])
        st = self.state()
        if not result["failed"]:
            self.assertNotEqual(st.criterion_check(st.results[rid], st.results[rid]["criteria"][0])["status"],
                                "passed", "Agent-authored external claim is not an independently acquired source")

    def test_04_correction_rejects_non_text_text(self):
        refs = self.ok([{"op": "park.add", "ref": "p", "text": "Тёмная тема"}])["refs"]
        result = self.write([{"op": "correct", "target": refs["p"], "action": "edit",
                              "edit": {"text": {"unexpected": "object"}},
                              "was": "Текст", "becomes": "Исправленный текст"}])
        self.assertTrue(result["failed"], "Correction must validate field types before persistence")
        self.assertIsInstance(self.state().parking[refs["p"]]["text"], str)


class PrivacyAndCheckRegressionTests(ProjectCase):
    def test_05_check_does_not_persist_or_echo_secret_argument(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        canary = "sk-ant-api03-" + "a" * 40  # synthetic; not a credential
        _, output = self.cli("check", "--result", rid, "--criteria", refs["c1"],
                             "--", sys.executable, "-c", "pass", canary)
        records = "\n".join(p.read_text(encoding="utf-8") for p in Path(self.store.records_dir).glob("*.json"))
        self.assertNotIn(canary, records, "Core-origin records need the same privacy gate as agent receipts")
        self.assertNotIn(canary, output, "Human/agent output is also a privacy sink")

    def test_06_redact_removes_nested_result_text(self):
        refs = self.login_topic()
        canary = "PRIVATE_NESTED_REVIEW_CANARY"
        rid = self.ok([{"op": "result.present", "ref": "r", "thread": refs["t"],
                        "criteria": [refs["c1"]], "summary": "Результат для проверки",
                        "check": {"steps": [canary], "expect": "видно имя", "duration": "около пяти минут"}}])["refs"]["r"]
        code, output = self.cli("redact", rid, "--confirm")
        self.assertEqual(code, 0, output)
        records = "\n".join(p.read_text(encoding="utf-8") for p in Path(self.store.records_dir).glob("*.json"))
        self.assertNotIn(canary, records, "Deletion confirmation must cover nested fields")

    def test_07_redact_preserves_structural_criterion_references(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        aid = self.ok([{"op": "accept", "ref": "a", "result": rid,
                        "criteria": [refs["c1"]], "outcome": "accepted",
                        "purpose": "внутреннего показа", "source": OWNER}])["refs"]["a"]
        code, output = self.cli("redact", aid, "--confirm")
        self.assertEqual(code, 0, output)
        st = self.state()
        for acceptance in st.acceptances.values():
            for cid in acceptance.get("criteria", []):
                self.assertIn(cid, st.criteria, "Redaction must not replace structural IDs with a text marker")

    def test_08_invalid_check_target_has_no_process_side_effect(self):
        refs = self.login_topic()
        rid = self.present(refs)
        marker = Path(self.project) / "must_not_be_created.txt"
        command = "from pathlib import Path; Path(%r).write_text('ran')" % str(marker)
        code, output = self.cli("check", "--result", rid, "--criteria", "cr-not-in-result",
                                "--", sys.executable, "-c", command)
        self.assertNotEqual(code, 0, output)
        self.assertFalse(marker.exists(), "Validate criterion membership BEFORE running any command")

    def test_09_disconnected_check_does_not_execute_or_append(self):
        refs = self.login_topic()
        rid = self.present(refs)
        self.cli("disconnect")
        before = self.state().last_tx
        marker = Path(self.project) / "disabled_check.txt"
        command = "from pathlib import Path; Path(%r).write_text('ran')" % str(marker)
        self.cli("check", "--result", rid, "--criteria", refs["c1"],
                 "--", sys.executable, "-c", command)
        self.assertFalse(marker.exists(), "Disabled Atlas must not perform a new observed check implicitly")
        self.assertEqual(self.state().last_tx, before, "Disabled Atlas must not append a check receipt")


class DeliveryAndRecoveryRegressionTests(ProjectCase):
    def test_10_delivery_manifest_contains_only_rendered_decisions(self):
        for i in range(24):
            self.ok([{"op": "thread.create", "title": "Тема %02d %s" % (i, "я" * 170), "state": "planned"}])
        did = self.ok([{"op": "decision.decide", "ref": "d", "text": "Публикацию не разрешаю", "source": OWNER}])["refs"]["d"]
        st = self.state()
        text, manifest = build(st, "python3 atlas.py", session="receiver", snapshot=st.last_tx)
        if did in manifest["decisions"]:
            self.assertIn(did, text, "A hidden decision must not be recorded as delivered")

    def test_11_partial_receipt_replay_preserves_partial_outcome_and_refs(self):
        ops = [{"op": "note.learned", "ref": "n", "text": "Часть записи сохранена"},
               {"op": "thread.state", "thread": "несуществующая тема", "state": "paused"}]
        first = self.write(ops, key="review-partial-receipt")
        self.assertTrue(first["failed"])
        self.assertTrue(first["applied"])
        again = self.write(ops, key="review-partial-receipt")
        self.assertTrue(again["repeat"])
        self.assertEqual(again["tx"], first["tx"])
        self.assertEqual(again["failed"], first["failed"], "Retry must not turn a partial write into success")
        self.assertEqual(again["refs"], first["refs"], "Retry must restore original object references")

    def test_12_capture_detects_unrecorded_tail_after_earlier_receipt(self):
        self.hook("session-start", {"session_id": "before", "source": "startup"})
        self.ok([{"op": "note.learned", "text": "Ранняя контрольная точка"}], session="before")
        self.touch("late_change.py", "print('later unrecorded work')\n")
        self.hook("stop", {"session_id": "before", "stop_hook_active": False})
        self.hook("session-start", {"session_id": "after", "source": "startup"})
        self.assertTrue(self.state().open_gaps(), "An early receipt does not cover later file changes")

    def test_13_unresolved_gap_stays_visible_in_owner_report(self):
        self.hook("session-start", {"session_id": "gap-source", "source": "startup"})
        self.touch("unrecorded.py", "print('unrecorded')\n")
        self.hook("stop", {"session_id": "gap-source", "stop_hook_active": False})
        self.hook("session-start", {"session_id": "next", "source": "startup"})
        self.assertTrue(self.state().open_gaps(), "Fixture must create an unresolved capture gap")
        _, output = self.cli("report")
        self.assertIn("частично", output, "Unresolved gap must not disappear after its first display")


class StorageRegressionTests(ProjectCase):
    @unittest.skipIf(os.name == "nt", "POSIX executable-mode fixture")
    def test_14_fingerprint_changes_with_executable_mode(self):
        self.sh("git", "config", "core.filemode", "true")
        script = Path(self.project) / "run.sh"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o644)
        self.sh("git", "add", "run.sh")
        self.sh("git", "commit", "-qm", "review executable fixture")
        before = worktree_fingerprint(self.project)
        script.chmod(0o755)
        self.assertIn("mode change", self.sh("git", "diff", "--summary"))
        after = worktree_fingerprint(self.project)
        self.assertNotEqual(before["hash"], after["hash"], "Executable mode affects behavior even when bytes match")

    def test_15_live_writer_lock_is_not_stale_by_age_alone(self):
        path = Path(self.store.locks) / "review-writer.lock"
        path.write_text("%d %f" % (os.getpid(), time.time() - 121), encoding="utf-8")
        self.assertFalse(WriterLock(str(path), timeout=0.1)._stale(),
                         "Age alone must not revoke a lock held by a live process")

    def test_16_explicit_project_does_not_fall_back_to_another_project(self):
        wrong = Path(self.tmp) / "not-connected"
        wrong.mkdir()
        with mock.patch("os.getcwd", return_value=self.project):
            self.assertIsNone(find_project(str(wrong)),
                              "An explicit unconnected --project must not target a different cwd project")


class FollowUpTests(ProjectCase):
    def test_r14_fully_accepted_scope_is_closed_and_leaves_wip(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1", "c2", "c3"))
        self.ok([{"op": "accept", "result": rid, "purpose": "внутреннего показа", "outcome": "accepted",
                  "source": OWNER}])
        st = self.state()
        t = st.threads[refs["t"]]
        self.assertEqual(st.view_state(t), "closed")
        self.assertEqual(st.wip_count(), 0)
        self.assertNotIn("Вход через Google", views.next_step(st)["text"])
        self.assertIn("проверено 0 из 3", views.trust_line_thread(st, t))  # acceptance is not a check
        self.ok([{"op": "criterion.add", "thread": refs["t"], "text": "Человек выходит из аккаунта"}])
        self.assertEqual(self.state().view_state(self.state().threads[refs["t"]]), "active")

    def test_r14_partial_acceptance_keeps_work_open(self):
        refs = self.login_topic()
        rid = self.present(refs)  # c1, c3 — c2 (phone) is outside the version
        self.ok([{"op": "accept", "result": rid, "purpose": "показа", "outcome": "accepted", "source": OWNER}])
        st = self.state()
        self.assertEqual(st.view_state(st.threads[refs["t"]]), "active")

    def test_r16_blocking_question_outranks_result_and_bookmark(self):
        refs = self.login_topic()
        rid = self.present(refs)
        self.ok([{"op": "bookmark.save", "stopped_at": "вход",
                  "next": {"text": "проверить вход", "kind": "check_result", "ref": rid}},
                 {"op": "decision.ask", "question": "Какой домен использовать для входа?", "urgency": "blocking"}])
        st = self.state()
        kinds = [i["type"] for i in st.needs()]
        self.assertEqual(kinds[:2], ["decision", "result"])
        self.assertIn("Какой домен", views.next_step(st)["text"])

    def test_correct_cannot_hide_a_failed_observed_check(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        code, _ = self.cli("check", "--result", rid, "--criteria", refs["c1"], "--",
                           sys.executable, "-c", "import sys; sys.exit(1)")
        vid = [v for v in self.state().verifications.values()][0]["id"]
        self.fails([{"op": "correct", "target": vid, "action": "withdraw", "was": "провал", "becomes": "не было"}],
                   "владельца")
        self.fails([{"op": "correct", "target": refs["c1"], "action": "withdraw", "was": "x", "becomes": "y"}],
                   "владельца")

    def test_r08_hash_covers_all_or_nothing_mode(self):
        ops = [{"op": "note.learned", "text": "раз"}]
        self.ok(ops, key="mode-key")
        with self.assertRaises(Exception):
            self.write(ops, key="mode-key", all_or_nothing=True)

    def test_r15_command_runner_is_not_preapproved(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, os.pardir, "skills", "atlas", "SKILL.md"), encoding="utf-8") as fh:
            front = fh.read().split("---\n")[1]
        for sub in ("check", "redact", "disconnect", "connect", "export"):
            self.assertNotIn("atlas.py %s *)" % sub, front)
        self.assertNotIn("atlas.py *)", front)
        self.assertIn("atlas.py write *)", front)

    def test_r05_core_records_never_keep_secret_text(self):
        refs = self.login_topic()
        rid = self.present(refs, criteria=("c1",))
        canary = "ghp_" + "b" * 36
        self.cli("check", "--result", rid, "--criteria", refs["c1"], "--env", "ключ " + canary, "--",
                 sys.executable, "-c", "print(%r)" % canary)
        records = "\n".join(p.read_text(encoding="utf-8") for p in Path(self.store.records_dir).glob("*.json"))
        self.assertNotIn(canary, records)


if __name__ == "__main__":
    unittest.main()
