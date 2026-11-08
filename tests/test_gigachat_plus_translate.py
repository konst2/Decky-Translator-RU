"""
GigaChat+ : game name and session glossary on top of the direct method.

HTTP is faked the same way as in test_gigachat_translate (helpers are reused
from there). The point is what goes into the prompt, what is taken from the
answer into the glossary, and that a bad answer still yields a translation.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from providers import gigachat_plus_translate as gp
from providers.base import ProviderType
from providers.gigachat_client import GigaChatClient
from providers.glossary_store import GlossaryKey, GlossaryStore
from test_gigachat_translate import (
    API_KEY,
    REFUSAL,
    FakeApi,
    chat_response,
    fake_http,
    run,
)


def provider(game="Baldur's Gate 3"):
    p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY)
    p.set_game(game)
    return p


def system_prompt(call):
    return call["json"]["messages"][0]["content"]


def user_prompt(call):
    return call["json"]["messages"][1]["content"]


class GigaChatPlusPromptTest(unittest.TestCase):

    def test_identity(self):
        p = provider()
        self.assertEqual(p.name, "GigaChat+")
        self.assertEqual(p.provider_type, ProviderType.GIGACHAT_PLUS)

    def test_game_name_and_glossary_instruction_are_sent(self):
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertIn("«Baldur's Gate 3»", system_prompt(api.chat_calls[0]))
        self.assertIn("---", user_prompt(api.chat_calls[0]))
        # Empty glossary: no glossary block yet.
        self.assertNotIn("Словарь игры", user_prompt(api.chat_calls[0]))

    def test_formal_address_only_for_russian(self):
        api = FakeApi(chat_contents=["1. Привет\n---", "1. Hallo\n---"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            run(p.translate_batch(["Hello"], "en", "de"))

        self.assertIn("на «вы»", system_prompt(api.chat_calls[0]))
        self.assertNotIn("на «вы»", system_prompt(api.chat_calls[1]))

    def test_no_game_line_without_game(self):
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(provider(game="").translate_batch(["Hello"], "en", "ru"))

        self.assertNotIn("Это текст из игры", system_prompt(api.chat_calls[0]))


class GigaChatPlusGlossaryTest(unittest.TestCase):

    def test_glossary_is_learned_and_sent_next_time(self):
        api = FakeApi(chat_contents=[
            "1. Шэдоухарт ранена\n---\nShadowheart → Шэдоухарт (ж)",
            "1. Шэдоухарт улыбается\n---",
        ])
        p = provider()
        with fake_http(api):
            first = run(p.translate_batch(["Shadowheart is wounded"], "en", "ru"))
            second = run(p.translate_batch(["Shadowheart smiles"], "en", "ru"))

        self.assertEqual(first, ["Шэдоухарт ранена"])
        self.assertEqual(second, ["Шэдоухарт улыбается"])
        self.assertEqual(p.glossary_entries(), [("Shadowheart", "Шэдоухарт (ж)")])
        self.assertIn("Shadowheart → Шэдоухарт (ж)", user_prompt(api.chat_calls[1]))

    def test_existing_entry_is_not_overwritten(self):
        api = FakeApi(chat_contents=[
            "1. Шэдоухарт\n---\nShadowheart → Шэдоухарт (ж)",
            "1. Тенесердце\n---\nshadowheart → Тенесердце (ж)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Shadowheart"], "en", "ru"))
            run(p.translate_batch(["Shadowheart"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Shadowheart", "Шэдоухарт (ж)")])

    def test_ascii_arrow_and_bullets_are_accepted(self):
        api = FakeApi(chat_contents=["1. Свежеватель атакует\n---\n- Mind Flayer -> Свежеватель разума"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["The Mind Flayer attacks"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Mind Flayer", "Свежеватель разума")])

    def test_junk_in_glossary_part_does_not_break_translation(self):
        long_term = "x" * (gp._MAX_TERM_LENGTH + 1)
        api = FakeApi(chat_contents=[
            "1. Привет\n2. Тав пришёл\n---\nНовых терминов нет.\n → пусто\n"
            f"{long_term} → длинно\nok → \nTav → Тав (м)"
        ])
        p = provider()
        with fake_http(api):
            result = run(p.translate_batch(["Hello", f"Tav came {long_term}"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Тав пришёл"])
        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])

    def test_echoed_format_and_absent_terms_are_rejected(self):
        # Seen live: the model copied the format example and listed names
        # that were not in the lines.
        api = FakeApi(chat_contents=[
            "1. Тав пришёл\n---\nоригинал → перевод\nGeralt → Геральт (м)\nTav → Тав (м)"
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Tav came"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])

    def test_gender_after_original_name_is_dropped_from_term(self):
        # Seen live: "Kim Kitsuragi (м) → Ким Кицураги (м)".
        api = FakeApi(chat_contents=["1. Ким кивает\n---\nKim Kitsuragi (м) → Ким Кицураги (м)"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Kim Kitsuragi nods"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Kim Kitsuragi", "Ким Кицураги (м)")])

    def test_typographic_apostrophe_matches_ascii_one(self):
        api = FakeApi(chat_contents=["1. Лаэзель\n---\nLae'zel → Лаэзель (ж)"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Lae’zel draws her sword"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Lae'zel", "Лаэзель (ж)")])

    def test_gender_mark_is_removed_from_translated_lines(self):
        # Seen live (GigaChat-2-Max, GigaChat-3-Ultra): the mark leaked into
        # the line and would be shown in the overlay.
        api = FakeApi(chat_contents=[
            "1. Шэдоухарт хранит своё прошлое в тайне. (ж)\n2. Лаэзель обнажает меч.(м)\n"
            "---\nShadowheart → Шэдоухарт (ж)"
        ])
        p = provider()
        with fake_http(api):
            result = run(p.translate_batch(
                ["Shadowheart keeps her past a secret.", "Lae'zel draws her sword."], "en", "ru"
            ))

        self.assertEqual(result, ["Шэдоухарт хранит своё прошлое в тайне.", "Лаэзель обнажает меч."])
        # The glossary keeps its gender: only the lines are cleaned.
        self.assertEqual(p.glossary_entries(), [("Shadowheart", "Шэдоухарт (ж)")])

    def test_prompt_forbids_gender_in_lines(self):
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertIn("В переводах строк пол не пиши", user_prompt(api.chat_calls[0]))

    def test_missing_gender_is_added_later(self):
        api = FakeApi(chat_contents=[
            "1. Шэдоухарт\n---\nShadowheart → Шэдоухарт",
            "1. Шэдоухарт улыбается\n---\nShadowheart → Шэдоухарт (ж)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Shadowheart"], "en", "ru"))
            run(p.translate_batch(["Shadowheart smiles, she is happy"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Shadowheart", "Шэдоухарт (ж)")])

    def test_gender_does_not_replace_gender_or_translation(self):
        api = FakeApi(chat_contents=[
            "1. Карлах\n---\nKarlach → Карлах (ж)\nGale → Гейл",
            # Another gender and another translation of the name: both ignored.
            "1. Карлах и Гейл\n---\nKarlach → Карлах (м)\nGale → Гэйл (м)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Karlach and Gale"], "en", "ru"))
            run(p.translate_batch(["Karlach and Gale"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Karlach", "Карлах (ж)"), ("Gale", "Гейл")])

    def test_missing_separator_retries_without_context(self):
        # Without a separator any line after the translations would be glued
        # to the last one as its "continuation" and reach the screen
        # ("Тав пришёл Словарь:"). Such an answer is not trusted.
        for junk in ["Tav → Тав (м)", "Словарь:\nTav → Тав (м)", "Tav — Тав (м)"]:
            with self.subTest(junk=junk):
                api = FakeApi(chat_contents=[
                    f"1. Привет\n2. Тав пришёл\n{junk}",
                    "1. Привет\n2. Тав пришёл",
                ])
                p = provider()
                with fake_http(api):
                    result = run(p.translate_batch(["Hello", "Tav came"], "en", "ru"))

                self.assertEqual(result, ["Привет", "Тав пришёл"])
                self.assertEqual(len(api.chat_calls), 2)
                self.assertNotIn("---", user_prompt(api.chat_calls[1]))
                self.assertEqual(p.glossary_entries(), [])

    def test_separator_variants_are_accepted(self):
        for separator in ["---", "----", "———", "***", "==="]:
            with self.subTest(separator=separator):
                api = FakeApi(chat_contents=[f"1. Привет\n2. Тав пришёл\n{separator}\nTav → Тав (м)"])
                p = provider()
                with fake_http(api):
                    result = run(p.translate_batch(["Hello", "Tav came"], "en", "ru"))

                self.assertEqual(result, ["Привет", "Тав пришёл"])
                self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])

    def test_answer_without_separator_and_without_glossary_is_fine(self):
        api = FakeApi(chat_contents=["1. Привет\n2. Мир"])
        p = provider()
        with fake_http(api):
            result = run(p.translate_batch(["Hello", "World"], "en", "ru"))

        self.assertEqual(result, ["Привет", "Мир"])
        self.assertEqual(len(api.chat_calls), 1)

    def test_apostrophe_variants_are_one_entry(self):
        api = FakeApi(chat_contents=[
            "1. Лаэзель\n---\nLae'zel → Лаэзель (ж)",
            "1. Лейзел\n---\nLae’zel → Лейзел (ж)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Lae'zel"], "en", "ru"))
            run(p.translate_batch(["Lae’zel"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Lae'zel", "Лаэзель (ж)")])

    def test_game_change_during_request_keeps_old_terms_out(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = provider()

        def post_and_switch_game(*args, **kwargs):
            # A new screenshot from another game arrives while the request
            # for the old game is in flight.
            response = api.post(*args, **kwargs)
            if args and args[0].endswith("/chat/completions"):
                p.set_game("Disco Elysium")
            return response

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post_and_switch_game), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            result = run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(result, ["Тав"])
        self.assertEqual(p.glossary_entries(), [])

    def test_line_of_only_gender_mark_is_kept(self):
        api = FakeApi(chat_contents=["1. (ж)\n---"])
        with fake_http(api):
            result = run(provider().translate_batch(["(f)"], "en", "ru"))

        self.assertEqual(result, ["(ж)"])

    def test_new_game_resets_glossary(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))
        p.set_game("Baldur's Gate 3")  # same game: kept
        self.assertEqual(len(p.glossary_entries()), 1)
        p.set_game("Disco Elysium")
        self.assertEqual(p.glossary_entries(), [])

    def test_language_pair_change_resets_glossary(self):
        api = FakeApi(chat_contents=[
            "1. Тав\n---\nTav → Тав (м)",
            "1. Привет\n---",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))
            run(p.translate_batch(["こんにちは"], "ja", "ru"))

        self.assertNotIn("Tav → Тав", user_prompt(api.chat_calls[1]))
        self.assertEqual(p.glossary_entries(), [])

    def test_list_screen_adds_at_most_five_new_entries(self):
        names = ["Kuon", "Haku", "Ukon", "Nekone", "Atuy", "Rulutieh", "Kiwru", "Anju"]
        roster = " ".join(names)
        api = FakeApi(chat_contents=[
            "1. Ош\n---\nOshtor → Ош",
            "1. список\n---\n" + "\n".join(f"{name} → {name}-ру" for name in names),
        ])
        p = provider()
        with patch.object(gp, "_MAX_GLOSSARY_ENTRIES", 6), fake_http(api), \
                self.assertLogs(gp.logger, level="INFO") as logs:
            run(p.translate_batch(["Oshtor"], "en", "ru"))
            run(p.translate_batch([roster], "en", "ru"))

        terms = [term for term, _ in p.glossary_entries()]
        # The active character not on the list screen survives; only the
        # first five names of the list are taken.
        self.assertEqual(terms, ["Oshtor", "Kuon", "Haku", "Ukon", "Nekone", "Atuy"])
        self.assertTrue(any("dropped: Rulutieh, Kiwru, Anju" in line for line in logs.output))

    def test_gender_for_existing_entries_is_not_limited(self):
        names = ["Kuon", "Haku", "Ukon", "Nekone", "Atuy", "Rulutieh"]
        text = " ".join(names)
        api = FakeApi(chat_contents=[
            # 5 new now, the sixth comes on the next screen.
            "1. а\n---\n" + "\n".join(f"{name} → {name}-ру" for name in names[:5]),
            "1. б\n---\nRulutieh → Rulutieh-ру\n"
            + "\n".join(f"{name} → {name}-ру (ж)" for name in names[:5]),
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch([text], "en", "ru"))
            run(p.translate_batch([text], "en", "ru"))

        entries = dict(p.glossary_entries())
        self.assertEqual(len(entries), 6)
        # All five genders, not just the first: with the limit wrongly
        # counting genders, the last ones would be the ones dropped.
        for name in names[:5]:
            self.assertEqual(entries[name], f"{name}-ру (ж)")

    def test_eviction_on_merge_is_logged(self):
        api = FakeApi(chat_contents=["1. а б в\n---\nA → А\nB → Б\nC → В"])
        p = provider()
        with patch.object(gp, "_MAX_GLOSSARY_ENTRIES", 2), fake_http(api), \
                self.assertLogs(gp.logger, level="INFO") as logs:
            run(p.translate_batch(["a b c"], "en", "ru"))

        self.assertTrue(any("glossary full (2), evicted: A" in line for line in logs.output))

    def test_full_glossary_evicts_oldest_unused_entry(self):
        api = FakeApi(chat_contents=[
            "1. а б\n---\nA → А\nB → Б",
            # "A" occurs in this batch, so it is "used" and survives.
            "1. а в\n---\nC → В",
        ])
        p = provider()
        with patch.object(gp, "_MAX_GLOSSARY_ENTRIES", 2), fake_http(api):
            run(p.translate_batch(["a b"], "en", "ru"))
            run(p.translate_batch(["a c"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("A", "А"), ("C", "В")])


BG3_ID = "1086940"
BG3_KEY = GlossaryKey("gigachat_plus", BG3_ID, "en", "ru")


class GigaChatPlusSavedGlossaryTest(unittest.TestCase):
    """The glossary in a GlossaryStore: saved, loaded back, kept per game and pair."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self._tmp.name, "glossary.db"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def provider(self, game="Baldur's Gate 3", game_id=BG3_ID):
        p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY, glossary_store=self.store)
        p.set_game(game, game_id)
        return p

    def saved(self, key=BG3_KEY):
        return [(e.term, e.translation) for e in self.store.load_entries(key)]

    def test_learned_entry_is_saved(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        with fake_http(api):
            run(self.provider().translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(self.saved(), [("Tav", "Тав (м)")])

    def test_restarted_provider_loads_and_sends_the_glossary(self):
        api = FakeApi(chat_contents=[
            "1. Тав\n---\nTav → Тав (м)",
            "1. Тав спит\n---",
        ])
        with fake_http(api):
            run(self.provider().translate_batch(["Tav"], "en", "ru"))
            # A new provider = the plugin after a restart.
            restarted = self.provider()
            run(restarted.translate_batch(["Tav sleeps"], "en", "ru"))

        self.assertIn("Tav → Тав (м)", user_prompt(api.chat_calls[1]))
        self.assertEqual(restarted.glossary_entries(), [("Tav", "Тав (м)")])

    def test_without_game_id_nothing_is_saved(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = self.provider(game_id="")
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))

        # Still works in memory, as before saving existed.
        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])
        count = self.store._conn.execute("SELECT COUNT(*) FROM glossaries").fetchone()[0]
        self.assertEqual(count, 0)

    def test_returning_to_a_game_loads_its_glossary(self):
        api = FakeApi(chat_contents=[
            "1. Тав\n---\nTav → Тав (м)",
            "1. Ким\n---\nKim → Ким (м)",
            "1. Тав\n---",
        ])
        p = self.provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))
            p.set_game("Disco Elysium", "632470")
            run(p.translate_batch(["Kim"], "en", "ru"))
            p.set_game("Baldur's Gate 3", BG3_ID)
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertNotIn("Tav → Тав", user_prompt(api.chat_calls[1]))
        self.assertIn("Tav → Тав (м)", user_prompt(api.chat_calls[2]))
        self.assertNotIn("Kim → Ким", user_prompt(api.chat_calls[2]))
        self.assertEqual(self.saved(), [("Tav", "Тав (м)")])
        disco = GlossaryKey("gigachat_plus", "632470", "en", "ru")
        self.assertEqual(self.saved(disco), [("Kim", "Ким (м)")])

    def test_each_language_pair_has_its_own_saved_glossary(self):
        api = FakeApi(chat_contents=[
            "1. Тав\n---\nTav → Тав (м)",
            "1. Тав\n---",
            "1. Тав\n---",
        ])
        p = self.provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))
            run(p.translate_batch(["Tav"], "en", "de"))
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertNotIn("Tav → Тав", user_prompt(api.chat_calls[1]))
        self.assertIn("Tav → Тав (м)", user_prompt(api.chat_calls[2]))

    def test_added_gender_is_saved(self):
        api = FakeApi(chat_contents=[
            "1. Карлах\n---\nKarlach → Карлах",
            "1. Карлах улыбается\n---\nKarlach → Карлах (ж)",
        ])
        p = self.provider()
        with fake_http(api):
            run(p.translate_batch(["Karlach"], "en", "ru"))
            run(p.translate_batch(["Karlach smiles, she is happy"], "en", "ru"))

        self.assertEqual(self.saved(), [("Karlach", "Карлах (ж)")])

    def test_eviction_order_survives_a_restart(self):
        api = FakeApi(chat_contents=[
            "1. а б\n---\nA → А\nB → Б",
            # Uses "A": after this, "B" is the least recently used.
            "1. а\n---",
            "1. в\n---\nC → В",
        ])
        with patch.object(gp, "_MAX_GLOSSARY_ENTRIES", 2), fake_http(api):
            first = self.provider()
            run(first.translate_batch(["a b"], "en", "ru"))
            with patch.object(gp.time, "time", return_value=10**10):
                run(first.translate_batch(["a"], "en", "ru"))
            restarted = self.provider()
            with patch.object(gp.time, "time", return_value=10**10 + 1):
                run(restarted.translate_batch(["c"], "en", "ru"))

        self.assertEqual(restarted.glossary_entries(), [("A", "А"), ("C", "В")])
        self.assertEqual(self.saved(), [("A", "А"), ("C", "В")])

    def test_store_errors_do_not_break_translation(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        broken = sqlite3.OperationalError("disk I/O error")
        p = self.provider()
        with patch.object(self.store, "load_entries", side_effect=broken), \
                patch.object(self.store, "replace_entries", side_effect=broken), \
                fake_http(api):
            result = run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(result, ["Тав"])
        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])

    def test_failed_load_does_not_let_a_save_wipe_the_stored_glossary(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        self.store.replace_entries(BG3_KEY, "Baldur's Gate 3", [
            gp.GlossaryEntry("karlach", "Karlach", "Карлах (ж)", 1),
            gp.GlossaryEntry("gale", "Gale", "Гейл (м)", 2),
        ])
        p = self.provider()
        # Only the read fails (e.g. "database is locked"); writes work.
        locked = sqlite3.OperationalError("database is locked")
        with patch.object(self.store, "load_entries", side_effect=locked), fake_http(api):
            result = run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(result, ["Тав"])
        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)")])
        self.assertEqual(self.saved(), [("Karlach", "Карлах (ж)"), ("Gale", "Гейл (м)")])

    def test_same_name_with_another_game_id_is_another_glossary(self):
        # Two shortcuts with the same name (e.g. a Steam and a GOG copy).
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)", "1. Тав\n---"])
        p = self.provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))
            p.set_game("Baldur's Gate 3", "3000000001")
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertNotIn("Tav → Тав", user_prompt(api.chat_calls[1]))
        self.assertEqual(p.glossary_entries(), [])

    def test_loaded_glossary_is_trimmed_to_the_limit_and_eviction_is_logged(self):
        self.store.replace_entries(BG3_KEY, "Baldur's Gate 3", [
            gp.GlossaryEntry("a", "A", "А", 1),
            gp.GlossaryEntry("b", "B", "Б", 2),
            gp.GlossaryEntry("c", "C", "В", 3),
        ])
        api = FakeApi(chat_contents=["1. Привет\n---"])
        p = self.provider()
        with patch.object(gp, "_MAX_GLOSSARY_ENTRIES", 2), fake_http(api), \
                self.assertLogs(gp.logger, level="INFO") as logs:
            run(p.translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("B", "Б"), ("C", "В")])
        self.assertTrue(any("evicted: A" in line for line in logs.output))

    def test_numeric_game_id_works(self):
        # Steam's other app types use numbers for appid; the declared string
        # type may not hold.
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = self.provider(game_id=int(BG3_ID))
        with fake_http(api):
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(self.saved(), [("Tav", "Тав (м)")])
        self.assertEqual([e.term for e in p.glossary_for(int(BG3_ID), "en", "ru")], ["Tav"])

    def test_terms_answered_after_a_game_switch_are_not_saved_for_either_game(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = self.provider()

        def post_and_switch_game(*args, **kwargs):
            response = api.post(*args, **kwargs)
            if args and args[0].endswith("/chat/completions"):
                p.set_game("Disco Elysium", "632470")
            return response

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post_and_switch_game), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(self.saved(), [])
        disco = GlossaryKey("gigachat_plus", "632470", "en", "ru")
        self.assertEqual(self.saved(disco), [])


class GigaChatPlusGlossaryFormTest(unittest.TestCase):
    """View, delete an entry, reset: what the glossary form does."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self._tmp.name, "glossary.db"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def provider(self, game="Baldur's Gate 3", game_id=BG3_ID, store=True):
        p = gp.GigaChatPlusTranslateProvider(
            api_key=API_KEY, glossary_store=self.store if store else None
        )
        p.set_game(game, game_id)
        return p

    def learned(self, p, answer="1. Тав и Карлах\n---\nTav → Тав (м)\nKarlach → Карлах (ж)"):
        api = FakeApi(chat_contents=[answer])
        with fake_http(api):
            run(p.translate_batch(["Tav and Karlach"], "en", "ru"))
        return p

    def terms(self, entries):
        return [entry.term for entry in entries]

    def saved_terms(self, key=BG3_KEY):
        return self.terms(self.store.load_entries(key))

    def test_current_game_glossary_is_shown(self):
        p = self.learned(self.provider())

        self.assertEqual(self.terms(p.glossary_for(BG3_ID, "en", "ru")), ["Tav", "Karlach"])

    def test_saved_glossary_of_another_game_is_shown(self):
        self.learned(self.provider())
        # The form is opened in BG3 before its first translation: the
        # provider still holds another game, the saved glossary is shown.
        p = self.provider(game="Disco Elysium", game_id="632470")

        self.assertEqual(self.terms(p.glossary_for(BG3_ID, "en", "ru")), ["Tav", "Karlach"])

    def test_glossary_of_unsaved_current_game_is_shown_from_memory(self):
        p = self.learned(self.provider(store=False))

        self.assertEqual(self.terms(p.glossary_for(BG3_ID, "en", "ru")), ["Tav", "Karlach"])

    def test_no_game_shows_nothing_and_deletes_nothing(self):
        p = self.learned(self.provider())

        self.assertEqual(p.glossary_for("", "en", "ru"), [])
        self.assertFalse(p.delete_glossary_entry("", "en", "ru", "tav"))
        self.assertFalse(p.reset_glossary("", "en", "ru"))

    def test_language_codes_are_normalized_like_translation(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = self.provider()
        with fake_http(api):
            run(p.translate_batch(["Tav"], "zh-CN", "ru"))

        self.assertEqual(self.terms(p.glossary_for(BG3_ID, "zh-CN", "ru")), ["Tav"])
        self.assertTrue(p.delete_glossary_entry(BG3_ID, "zh-CN", "ru", "tav"))

    def test_deleted_entry_is_gone_from_memory_store_and_next_prompt(self):
        p = self.learned(self.provider())

        self.assertTrue(p.delete_glossary_entry(BG3_ID, "en", "ru", "tav"))

        self.assertEqual(p.glossary_entries(), [("Karlach", "Карлах (ж)")])
        self.assertEqual(self.saved_terms(), ["Karlach"])
        api = FakeApi(chat_contents=["1. Тав\n---"])
        with fake_http(api):
            run(p.translate_batch(["Karlach"], "en", "ru"))
        self.assertNotIn("Tav →", user_prompt(api.chat_calls[0]))
        # The translation above saved the glossary again: the entry must
        # not come back with it.
        self.assertEqual(self.saved_terms(), ["Karlach"])

    def test_deleting_from_another_game_keeps_the_current_glossary(self):
        self.learned(self.provider())
        p = self.learned(
            self.provider(game="Disco Elysium", game_id="632470"),
            answer="1. Тав\n---\nTav → Тав из Диско",
        )

        self.assertTrue(p.delete_glossary_entry(BG3_ID, "en", "ru", "tav"))

        self.assertEqual(p.glossary_entries(), [("Tav", "Тав из Диско")])
        self.assertEqual(self.saved_terms(), ["Karlach"])

    def test_deleting_a_missing_entry_reports_false(self):
        p = self.learned(self.provider())

        self.assertFalse(p.delete_glossary_entry(BG3_ID, "en", "ru", "nobody"))

    def test_reset_clears_memory_and_store(self):
        p = self.learned(self.provider())

        self.assertTrue(p.reset_glossary(BG3_ID, "en", "ru"))

        self.assertEqual(p.glossary_entries(), [])
        self.assertEqual(self.saved_terms(), [])

    def test_reset_of_unknown_glossary_reports_false(self):
        self.assertFalse(self.provider().reset_glossary(BG3_ID, "en", "ru"))

    def test_failed_store_delete_leaves_memory_untouched(self):
        p = self.learned(self.provider())
        broken = sqlite3.OperationalError("attempt to write a readonly database")

        with patch.object(self.store, "delete_entry", side_effect=broken):
            with self.assertRaises(sqlite3.OperationalError):
                p.delete_glossary_entry(BG3_ID, "en", "ru", "tav")

        # Memory and disk still agree: the entry is in both.
        self.assertEqual(p.glossary_entries(), [("Tav", "Тав (м)"), ("Karlach", "Карлах (ж)")])
        self.assertEqual(self.saved_terms(), ["Tav", "Karlach"])

    def test_failed_store_reset_leaves_memory_untouched(self):
        p = self.learned(self.provider())
        broken = sqlite3.OperationalError("attempt to write a readonly database")

        with patch.object(self.store, "delete_glossary", side_effect=broken):
            with self.assertRaises(sqlite3.OperationalError):
                p.reset_glossary(BG3_ID, "en", "ru")

        self.assertEqual(len(p.glossary_entries()), 2)

    def test_terms_of_a_request_in_flight_during_entry_delete_are_dropped(self):
        p = self.learned(self.provider())
        api = FakeApi(chat_contents=["1. Гейл\n---\nGale → Гейл (м)"])

        def post_and_delete(*args, **kwargs):
            response = api.post(*args, **kwargs)
            if args and args[0].endswith("/chat/completions"):
                p.delete_glossary_entry(BG3_ID, "en", "ru", "tav")
            return response

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post_and_delete), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            run(p.translate_batch(["Gale"], "en", "ru"))

        # The answer was built with the old glossary: its "Gale" is dropped.
        self.assertEqual(p.glossary_entries(), [("Karlach", "Карлах (ж)")])
        self.assertEqual(self.saved_terms(), ["Karlach"])

    def test_terms_of_a_request_in_flight_during_reset_are_dropped(self):
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        p = self.provider()

        def post_and_reset(*args, **kwargs):
            response = api.post(*args, **kwargs)
            if args and args[0].endswith("/chat/completions"):
                p.reset_glossary(BG3_ID, "en", "ru")
            return response

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post_and_reset), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            run(p.translate_batch(["Tav"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [])
        self.assertEqual(self.saved_terms(), [])


class GigaChatPlusFallbackTest(unittest.TestCase):

    def test_refusal_with_context_retries_without_it(self):
        api = FakeApi(chat_responses=[
            chat_response(REFUSAL, "blacklist"),
            chat_response("1. Привет"),
        ])
        p = provider()
        with fake_http(api):
            result = run(p.translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(len(api.chat_calls), 2)
        # The retry is the direct method's request: no game, no glossary.
        self.assertNotIn("Это текст из игры", system_prompt(api.chat_calls[1]))
        self.assertNotIn("---", user_prompt(api.chat_calls[1]))

    def test_broken_numbering_retries_without_context(self):
        api = FakeApi(chat_contents=["Вот перевод: Привет", "1. Привет"])
        p = provider()
        with fake_http(api):
            result = run(p.translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual(p.glossary_entries(), [])

    def test_glossary_from_failed_answer_is_ignored(self):
        api = FakeApi(chat_contents=["1. Привет\n---\nTav → Тав (м)", "1. Привет\n2. Мир"])
        p = provider()
        with fake_http(api):
            # Two lines asked, one answered: the answer is not trusted.
            run(p.translate_batch(["Hello", "World"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [])


LIVE_MODELS = [
    "GigaChat-2", "GigaChat-2-Max", "GigaChat-2-Pro",
    "GigaChat-3-Lightning", "GigaChat-3-Pro", "GigaChat-3-Ultra",
]


class GigaChatPlusModelChoiceTest(unittest.TestCase):
    """Which model GigaChat+ takes from the list (the direct method takes the first)."""

    def test_rules(self):
        cases = [
            ("list of 05.11.2026", LIVE_MODELS, "GigaChat-3-Ultra"),
            ("newer Ultra wins", LIVE_MODELS + ["GigaChat-4-Ultra"], "GigaChat-4-Ultra"),
            ("old Ultra beats new Pro", ["GigaChat-4-Pro", "GigaChat-3-Ultra"], "GigaChat-3-Ultra"),
            ("no Ultra: newest generation of Max/Pro",
             ["GigaChat-2", "GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-3-Lightning", "GigaChat-3-Pro"],
             "GigaChat-3-Pro"),
            ("same generation: Max before Pro", ["GigaChat-3-Pro", "GigaChat-3-Max"], "GigaChat-3-Max"),
            ("dotted generation", ["GigaChat-3-Pro", "GigaChat-3.1-Pro"], "GigaChat-3.1-Pro"),
            ("nothing strong: newest, first of equals",
             ["GigaChat-2", "GigaChat-3-Lightning", "GigaChat-3-Mini"], "GigaChat-3-Lightning"),
            ("class is a whole part, not a substring", ["GigaChat-2-Promo", "GigaChat-3"], "GigaChat-3"),
            ("empty list", [], None),
        ]
        for label, models, expected in cases:
            with self.subTest(label):
                self.assertEqual(gp._preferred_model(models), expected)

    def test_plus_uses_ultra_and_direct_uses_first(self):
        from providers.gigachat_translate import GigaChatTranslateProvider

        client = GigaChatClient(API_KEY)
        direct = GigaChatTranslateProvider(client=client)
        plus = gp.GigaChatPlusTranslateProvider(client=client)
        api = FakeApi(chat_contents=["1. Привет", "1. Пока\n---"], model_ids=LIVE_MODELS)
        with fake_http(api):
            run(direct.translate_batch(["Hello"], "en", "ru"))
            run(plus.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(api.chat_calls[0]["json"]["model"], "GigaChat-2")
        self.assertEqual(api.chat_calls[1]["json"]["model"], "GigaChat-3-Ultra")
        # One list for both methods.
        self.assertEqual(len(api.models_calls), 1)

    def test_retry_without_context_stays_on_plus_model(self):
        api = FakeApi(
            chat_responses=[chat_response(REFUSAL, "blacklist"), chat_response("1. Привет")],
            model_ids=LIVE_MODELS,
        )
        with fake_http(api):
            run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual([c["json"]["model"] for c in api.chat_calls],
                         ["GigaChat-3-Ultra", "GigaChat-3-Ultra"])

    def test_removed_ultra_falls_to_next_choice(self):
        from test_gigachat_translate import FakeResponse, models_response

        api = FakeApi(
            chat_responses=[FakeResponse(404, {"message": "No such model"}), chat_response("1. Привет\n---")],
            models_responses=[
                models_response(LIVE_MODELS),
                models_response([m for m in LIVE_MODELS if m != "GigaChat-3-Ultra"]),
            ],
        )
        with fake_http(api):
            result = run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertEqual(result, ["Привет"])
        self.assertEqual([c["json"]["model"] for c in api.chat_calls],
                         ["GigaChat-3-Ultra", "GigaChat-3-Pro"])

    def test_used_up_pool_of_plus_model_suggests_direct_method(self):
        from providers.base import RateLimitError
        from test_gigachat_translate import FakeResponse

        api = FakeApi(chat_responses=[FakeResponse(402, {"message": "Payment Required"})],
                      model_ids=LIVE_MODELS)
        with fake_http(api):
            with self.assertRaises(RateLimitError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))

        message = str(ctx.exception)
        self.assertIn("GigaChat-3-Ultra", message)
        self.assertIn("Переключитесь на метод GigaChat", message)

    def test_quota_after_404_names_the_retried_model(self):
        from providers.base import RateLimitError
        from test_gigachat_translate import FakeResponse, models_response

        api = FakeApi(
            chat_responses=[FakeResponse(404, {"message": "no"}), FakeResponse(402, {"message": "pay"})],
            models_responses=[
                models_response(LIVE_MODELS),
                models_response([m for m in LIVE_MODELS if m != "GigaChat-3-Ultra"]),
            ],
        )
        with fake_http(api):
            with self.assertRaises(RateLimitError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertIn("GigaChat-3-Pro", str(ctx.exception))
        self.assertIn("Переключитесь на метод GigaChat", str(ctx.exception))

    def test_listed_model_answering_404_is_skipped(self):
        # The model is in /v1/models but not usable (e.g. by the plan):
        # re-reading the list chooses it again. GigaChat+ must move on to
        # the next choice and not knock on it every request.
        from test_gigachat_translate import FakeResponse

        api = FakeApi(
            chat_responses=[
                FakeResponse(404, {"message": "no"}),
                chat_response("1. Привет\n---"),
                chat_response("1. Пока\n---"),
            ],
            model_ids=LIVE_MODELS,
        )
        p = provider()
        with fake_http(api):
            first = run(p.translate_batch(["Hello"], "en", "ru"))
            second = run(p.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual((first, second), (["Привет"], ["Пока"]))
        self.assertEqual([c["json"]["model"] for c in api.chat_calls],
                         ["GigaChat-3-Ultra", "GigaChat-3-Pro", "GigaChat-3-Pro"])

    def test_used_up_pool_of_shared_model_keeps_general_message(self):
        # No strong model: GigaChat+ is on the same model as the direct
        # method, so "switch to GigaChat" would not help.
        from providers.base import RateLimitError
        from test_gigachat_translate import FakeResponse

        api = FakeApi(chat_responses=[FakeResponse(402, {"message": "Payment Required"})],
                      model_ids=["GigaChat-2"])
        with fake_http(api):
            with self.assertRaises(RateLimitError) as ctx:
                run(provider().translate_batch(["Hello"], "en", "ru"))

        self.assertNotIn("Переключитесь", str(ctx.exception))


class GigaChatSharedClientTest(unittest.TestCase):

    def test_both_methods_share_one_token(self):
        from providers.gigachat_translate import GigaChatTranslateProvider

        client = GigaChatClient(API_KEY)
        direct = GigaChatTranslateProvider(client=client)
        plus = gp.GigaChatPlusTranslateProvider(client=client)
        api = FakeApi(chat_contents=["1. Привет", "1. Пока\n---"])
        with fake_http(api):
            run(direct.translate_batch(["Hello"], "en", "ru"))
            run(plus.translate_batch(["Bye"], "en", "ru"))

        self.assertEqual(len(api.oauth_calls), 1)
        self.assertEqual(len(api.models_calls), 1)

    def test_new_key_reaches_both_methods(self):
        from providers.gigachat_translate import GigaChatTranslateProvider

        client = GigaChatClient(API_KEY)
        direct = GigaChatTranslateProvider(client=client)
        plus = gp.GigaChatPlusTranslateProvider(client=client)
        direct.set_api_key("")
        self.assertFalse(plus.is_available("en", "ru"))


class GigaChatPlusManagerTest(unittest.TestCase):
    """Wiring in ProviderManager: selection, shared client, game name."""

    def manager(self):
        from providers import ProviderManager

        m = ProviderManager()
        m.configure(ocr_provider="rapidocr", translation_provider="gigachat_plus")
        m.set_gigachat_api_key(API_KEY)
        return m

    def test_preference_selects_gigachat_plus(self):
        m = self.manager()
        self.assertIsInstance(m.get_translation_provider(), gp.GigaChatPlusTranslateProvider)

    def test_game_set_before_provider_exists_reaches_it(self):
        m = self.manager()
        m.set_current_game("Disco Elysium")
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(m.translate_text(["Hello"], "en", "ru"))

        self.assertIn("«Disco Elysium»", system_prompt(api.chat_calls[0]))

    def test_game_change_resets_glossary_of_existing_provider(self):
        m = self.manager()
        m.set_current_game("Baldur's Gate 3")
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        with fake_http(api):
            run(m.translate_text(["Tav"], "en", "ru"))
        plus = m.get_translation_provider()
        self.assertEqual(len(plus.glossary_entries()), 1)

        m.set_current_game("Disco Elysium")
        self.assertEqual(plus.glossary_entries(), [])

    def test_manager_passes_store_and_game_id_to_the_provider(self):
        from providers import ProviderManager

        with tempfile.TemporaryDirectory() as tmp:
            store = GlossaryStore(os.path.join(tmp, "glossary.db"))
            try:
                m = ProviderManager(glossary_store=store)
                m.configure(ocr_provider="rapidocr", translation_provider="gigachat_plus")
                m.set_gigachat_api_key(API_KEY)
                # Set before the provider exists: it must reach it on creation.
                m.set_current_game("Baldur's Gate 3", BG3_ID)
                api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
                with fake_http(api):
                    run(m.translate_text(["Tav"], "en", "ru"))

                saved = [(e.term, e.translation) for e in store.load_entries(BG3_KEY)]
                self.assertEqual(saved, [("Tav", "Тав (м)")])
            finally:
                store.close()

    def test_manager_glossary_form_reaches_the_provider(self):
        m = self.manager()
        m.set_current_game("Baldur's Gate 3", BG3_ID)
        api = FakeApi(chat_contents=["1. Тав\n---\nTav → Тав (м)"])
        with fake_http(api):
            run(m.translate_text(["Tav"], "en", "ru"))

        self.assertEqual([e.term for e in m.get_glossary(BG3_ID, "en", "ru")], ["Tav"])
        self.assertTrue(m.delete_glossary_entry(BG3_ID, "en", "ru", "tav"))
        self.assertEqual(m.get_glossary(BG3_ID, "en", "ru"), [])
        self.assertFalse(m.reset_glossary(BG3_ID, "en", "ru"))

    def test_both_methods_use_the_managers_client(self):
        from providers.gigachat_translate import GigaChatTranslateProvider

        m = self.manager()
        direct = m.get_translation_provider(ProviderType.GIGACHAT)
        plus = m.get_translation_provider(ProviderType.GIGACHAT_PLUS)
        self.assertIsInstance(direct, GigaChatTranslateProvider)
        self.assertIs(direct._client, plus._client)

        m.set_gigachat_api_key("")
        self.assertFalse(direct.is_available("en", "ru"))
        self.assertFalse(plus.is_available("en", "ru"))

    def test_reachability_probes_gigachat_plus(self):
        m = self.manager()
        api = FakeApi()
        with fake_http(api):
            result = run(m.check_web_reachability())

        self.assertEqual(result["translation"]["provider"], "gigachat_plus")
        self.assertTrue(result["translation"]["ok"])

    def test_cache_tier_is_above_direct_gigachat(self):
        from providers.translation_cache import PROVIDER_TIERS

        self.assertGreater(PROVIDER_TIERS["gigachat_plus"], PROVIDER_TIERS["gigachat"])


if __name__ == "__main__":
    unittest.main()
