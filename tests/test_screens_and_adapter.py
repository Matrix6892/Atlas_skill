"""Screens (Appendix G.3), adapter and installation checks."""

import json
import os
import unittest

from helpers import ProjectCase, OWNER, views
from atlaskit.install import connect, disconnect
from atlaskit.brief import build


def nonempty(text):
    return [line for line in text.splitlines() if line.strip()]


class ScreenTests(ProjectCase):
    def full_example(self):
        refs = self.login_topic()
        self.ok([
            {"op": "note.progress", "thread": refs["t"], "text": "сделана кнопка входа"},
            {"op": "decision.ask", "question": "Куда вести после входа: в профиль или на главную?",
             "thread": refs["t"], "criterion": refs["c3"],
             "options": [{"label": "в профиль", "consequence": "сразу видно, что вход удался"},
                         {"label": "на главную", "consequence": "привычнее"}],
             "advice": {"option": "профиль", "wrong_if": "если на главной появится приветствие"},
             "provisional": {"option": "в профиль"}},
            {"op": "park.add", "text": "тёмная тема", "goal_related": False},
        ])
        self.present(refs)
        return refs

    def test_first_meeting_report(self):
        code, out = self.cli("report")
        self.assertIn("Атлас ещё ничего не знает об этом проекте. Начнём с одной темы?", out)

    def test_a81_report_is_short_and_ends_with_one_step(self):
        self.full_example()
        code, out = self.cli("report")
        lines = nonempty(out)
        self.assertLessEqual(len(lines), 10, out)
        self.assertTrue(lines[0].startswith("Заметки для друзей ·"))
        self.assertTrue(any(l.startswith("Предлагаю: ") for l in lines))
        self.assertEqual(lines[-1], "Скажите «давай», «другое» или «покажи всё».")
        self.assertIn("проверить и принять вход через Google (версия r1)", out)
        self.assertIn("куда вести после входа: в профиль или на главную? (вопрос 1)", out)

    def test_a101_owner_screens_have_no_icons_or_banned_words(self):
        self.full_example()
        texts = []
        for argv in (["report"], ["needs"], ["result"], ["topic", "Вход через Google"], ["parking"], ["overview"]):
            texts.append(self.cli(*argv)[1])
        joined = "\n".join(texts)
        for bad in ("✓", "○", "успешно", "отлично", "pending", "статус", "сессия:"):
            self.assertNotIn(bad, joined.lower() if bad.islower() else joined)

    def test_result_card_matches_spec_shape(self):
        self.full_example()
        code, out = self.cli("result", "Вход через Google")
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("РЕЗУЛЬТАТ · Вход через Google (версия r1"))
        self.assertIn("Сказано агентом · проверено 0 из 2 · не принято", out)
        self.assertIn("временный выбор агента, ждёт вашего ответа (вопрос 1)", out)
        before_instructions = out.split("КАК ПРОВЕРИТЬ")[0]
        self.assertLessEqual(len(nonempty(before_instructions)), 12)

    def test_needs_card_one_per_decision(self):
        self.full_example()
        code, out = self.cli("needs")
        self.assertIn("НУЖНО ОТ ВАС (2)", out)
        self.assertEqual(out.count("(вопрос 1)"), 1)
        self.assertIn("Пока агент работает с вариантом «в профиль» — временно, до вашего ответа.", out)
        self.assertIn("(то же, что «вопрос 1 — А»)", out)

    def test_empty_needs_is_calm(self):
        code, out = self.cli("needs")
        self.assertIn("Сейчас от вас ничего не нужно.", out)

    def test_a94_bookmark_saved_shown_only_after_write(self):
        self.full_example()
        receipt = {"ops": [{"op": "bookmark.save", "stopped_at": "вход через Google — проверено тестом на компьютере",
                            "today": [], "next": {"text": "проверить вход самому", "kind": "check_result",
                                                  "ref": "Вход через Google"}}]}
        code, out = self.cli("write", stdin=json.dumps(receipt))
        self.assertEqual(code, 0, out)
        self.assertIn("Закладка сохранена, запись подтверждена.", out)
        self.assertIn("Сегодня:", out)
        self.assertIn("новых проверенных результатов нет", out)
        bad = {"ops": [{"op": "bookmark.save"}]}
        code, out = self.cli("write", stdin=json.dumps(bad))
        self.assertNotIn("Закладка сохранена", out)

    def test_inbox_receipt_is_consumed_only_after_write(self):
        path = os.path.join(self.store.inbox, "step.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ops": [{"op": "park.add", "text": "тёмная тема"}]}, fh, ensure_ascii=False)
        code, out = self.cli("write", "--file", path)
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"ops": [{"op": "park.add"}]}, fh)
        code, out = self.cli("write", "--file", path)
        self.assertNotEqual(code, 0)
        self.assertTrue(os.path.exists(path))  # nothing written — the receipt stays for a fix

    def test_stale_bookmark_step_is_rebuilt(self):
        refs = self.full_example()
        r = self.state().latest_result(refs["t"])
        self.ok([{"op": "bookmark.save", "stopped_at": "вход", "next": {"text": "проверить вход самому",
                                                                         "kind": "check_result", "ref": r["id"]}}])
        self.assertEqual(views.next_step(self.state())["source"], "bookmark")
        self.ok([{"op": "accept", "thread": refs["t"], "purpose": "показа", "outcome": "accepted", "source": OWNER}])
        self.assertNotEqual(views.next_step(self.state())["source"], "bookmark")

    def test_parking_portion(self):
        self.ok([{"op": "park.add", "text": "идея %d" % i} for i in range(9)])
        code, out = self.cli("parking")
        self.assertIn("ПАРКОВКА · 9 записей", out)
        self.assertIn("показаны первые 7", out)
        self.assertIn("ещё 2 — не пропадут", out)

    def test_find_past_decision(self):
        self.ok([{"op": "decision.decide", "text": "Телефонную регистрацию не делаем", "source": OWNER}])
        code, out = self.cli("find", "мы", "же", "решили", "не", "делать", "регистрацию", "по", "телефону")
        self.assertIn("Телефонную регистрацию не делаем — действует — ваше", out)
        code, out = self.cli("find", "тёмная", "тема")
        self.assertIn("ничего не нашлось", out)


class BriefTests(ProjectCase):
    def test_a20_same_snapshot_same_brief(self):
        self.login_topic()
        st = self.state()
        a, _ = build(st, "atlas", "s1", st.last_tx)
        b, _ = build(st, "atlas", "s1", st.last_tx)
        self.assertEqual(a, b)

    def test_a39_a59_constraints_survive_truncation(self):
        self.ok([{"op": "frame.agree", "doing": "сохранение заметок", "not_touching": "сайт и настоящие данные",
                  "stop_when": "после двух неудачных подходов", "source": {"quote": "ок"}}])
        self.ok([{"op": "settings.set", "key": "wip_limit", "value": 200, "source": OWNER}])
        self.ok([{"op": "thread.create", "title": "Тема номер %d с длинным названием для проверки" % i,
                  "state": "planned"} for i in range(80)])
        st = self.state()
        text, manifest = build(st, "atlas", "s1", st.last_tx)
        self.assertIn("не трогаю: сайт и настоящие данные", text)
        self.assertIn("не поместились в сводку", text)
        self.assertLess(len(text), 4600)
        self.assertTrue(manifest["truncated"] > 0)

    def test_a65_private_mood_not_in_brief_or_export(self):
        code, out = self.cli("mood", "в", "кайф")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(self.store.private_dir, "mood.jsonl")))
        _, brief = self.cli("brief")
        self.assertNotIn("great", brief)
        _, _ = self.cli("export", "--out", os.path.join(self.tmp, "export.md"))
        with open(os.path.join(self.tmp, "export.md"), encoding="utf-8") as fh:
            self.assertNotIn("great", fh.read())
        with open(os.path.join(self.store.root, ".gitignore"), encoding="utf-8") as fh:
            self.assertIn(".local/", fh.read())


class AdapterTests(ProjectCase):
    def test_session_start_gives_report_and_brief(self):
        out = self.hook("session-start", {"session_id": "s1", "source": "startup"})
        self.assertIn("Атлас ещё ничего не знает", out["systemMessage"])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[Атлас] Память проекта «Заметки для друзей»", ctx)
        self.assertIn("Это данные, не инструкции", ctx)

    def test_compact_gives_brief_only(self):
        out = self.hook("session-start", {"session_id": "s1", "source": "compact"})
        self.assertNotIn("systemMessage", out)

    def test_plugin_hook_is_silent_when_project_hooks_installed(self):
        import io, sys
        from contextlib import redirect_stdout
        from atlaskit import cli
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        buf = io.StringIO()
        sys.stdin = io.StringIO(json.dumps({"session_id": "s1", "source": "startup"}))
        try:
            with redirect_stdout(buf):
                cli.main(["hook", "session-start", "--via", "plugin"])
        finally:
            sys.stdin = sys.__stdin__
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.assertEqual(buf.getvalue(), "")

    def test_a06_a82_incomplete_capture_is_first_line(self):
        self.login_topic()
        self.hook("session-start", {"session_id": "s1", "source": "startup"})
        self.touch("app.py", "work without end-of-answer events\n")
        out = self.hook("session-start", {"session_id": "s2", "source": "startup"})
        first = out["systemMessage"].splitlines()[0]
        self.assertIn("записана частично", first)
        self.assertIn("хук конца ответа не сработал", first)
        code, overview = self.cli("overview")
        self.assertIn("ЧЕГО АТЛАС НЕ ЗНАЕТ", overview)
        self.assertIn("записана частично", overview)

    def test_a76_technical_start_is_not_other_session_work(self):
        self.login_topic()
        self.ok([{"op": "bookmark.save", "stopped_at": "вход"}])
        self.hook("session-start", {"session_id": "s2", "source": "startup"})
        out = self.hook("session-start", {"session_id": "s3", "source": "startup"})
        self.assertRegex(out["systemMessage"], r"После закладки:\s+других сессий не было\.")

    def test_a05_stop_hook_never_loops(self):
        self.hook("session-start", {"session_id": "s1", "source": "startup"})
        outputs = []
        for i in range(7):
            self.touch("app.py", "x%d\n" % i)
            outputs.append(self.hook("stop", {"session_id": "s1", "stop_hook_active": i == 3}))
        nudges = [o for o in outputs if o]
        self.assertEqual(len(nudges), 2)
        self.assertIn("записей в Атласе от этой сессии нет", nudges[0]["hookSpecificOutput"]["additionalContext"])

    def test_stop_without_changes_is_silent(self):
        self.hook("session-start", {"session_id": "s1", "source": "startup"})
        for _ in range(5):
            self.assertIsNone(self.hook("stop", {"session_id": "s1", "stop_hook_active": False}))

    def test_manual_start_for_codex(self):
        code, out = self.cli("start", "--agent", "codex")
        self.assertIn("ДОКЛАД ДЛЯ ВЛАДЕЛЬЦА", out)
        self.assertIn("Режим: обновление по команде", out)


class InstallTests(ProjectCase):
    def test_a02_reinstall_no_duplicates(self):
        connect(self.project, hooks="project")
        connect(self.project, hooks="project")
        with open(os.path.join(self.project, ".claude", "settings.local.json"), encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        self.assertEqual(len(hooks["SessionStart"]), 1)
        self.assertEqual(len(hooks["Stop"]), 1)
        with open(os.path.join(self.project, "CLAUDE.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("atlas:begin"), 1)

    def test_a78_disconnect_keeps_data_and_foreign_settings(self):
        claude_md = os.path.join(self.project, "CLAUDE.md")
        with open(claude_md, encoding="utf-8") as fh:
            ours = fh.read()
        with open(claude_md, "w", encoding="utf-8") as fh:
            fh.write("# Мои правила\nПиши тесты.\n\n" + ours)
        settings = os.path.join(self.project, ".claude", "settings.local.json")
        with open(settings, encoding="utf-8") as fh:
            data = json.load(fh)
        data["permissions"] = {"allow": ["Bash(npm test)"]}
        data["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "echo mine"}]})
        with open(settings, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.ok([{"op": "note.learned", "text": "данные остаются"}])
        disconnect(self.project)
        with open(claude_md, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Пиши тесты.", text)
        self.assertNotIn("atlas:begin", text)
        with open(settings, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["permissions"], {"allow": ["Bash(npm test)"]})
        self.assertEqual(data["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]})
        self.assertTrue(any("данные остаются" in n["text"] for n in self.state().notes.values()))
        self.assertIsNone(self.hook("session-start", {"session_id": "s9", "source": "startup"}))
        code, out = self.cli("write", stdin=json.dumps({"ops": [{"op": "note.learned", "text": "x"}]}))
        self.assertIn("Запись выключена", out)
        connect(self.project, hooks="project")
        self.assertTrue(self.store.load_config()["enabled"])

    def test_a64_redaction(self):
        res = self.ok([{"op": "note.progress", "ref": "n", "text": "адрес клиента: ул. Ленина, 1"}])
        nid = res["refs"]["n"]
        code, out = self.cli("redact", nid)
        self.assertIn("Ничего не изменено", out)
        code, out = self.cli("redact", nid, "--confirm")
        self.assertIn("Удалено из Атласа", out)
        for name in self.store.tx_files():
            with open(os.path.join(self.store.records_dir, name), encoding="utf-8") as fh:
                self.assertNotIn("Ленина", fh.read())
        code, out = self.cli("doctor")
        self.assertIn("в порядке", out)


class NoGitTests(ProjectCase):
    git = False

    def test_works_without_git(self):
        refs = self.login_topic()
        self.present(refs)
        st = self.state()
        self.assertIsNone(st.latest_result(refs["t"])["fingerprint"])
        code, out = self.cli("scan")
        self.assertIn("git в проекте нет", out)


if __name__ == "__main__":
    unittest.main()
