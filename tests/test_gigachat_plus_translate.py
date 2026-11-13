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
from providers.base import ProviderType, TranslationSupersededError
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


class _NeverFiringTimer:
    def cancel(self):
        pass


def _never_start_timer(seconds, callback):
    return _NeverFiringTimer()


# A real cleanup timer would fire 5 s later in the middle of another test
# and send a request through whatever HTTP fake is active then. No test
# starts one; the cleanup tests drive the timer by hand (ManualTimers).
_timer_patch = patch.object(gp, "_start_timer", _never_start_timer)


def setUpModule():
    _timer_patch.start()


def tearDownModule():
    _timer_patch.stop()


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

    def test_address_rule_only_for_russian(self):
        api = FakeApi(chat_contents=["1. Привет\n---", "1. Hallo\n---"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Hello"], "en", "ru"))
            run(p.translate_batch(["Hello"], "en", "de"))

        self.assertIn(gp._RUSSIAN_ADDRESS_RULE, system_prompt(api.chat_calls[0]))
        self.assertNotIn("«ты» или «вы»", system_prompt(api.chat_calls[1]).casefold())

    def test_address_is_chosen_by_relationship_not_always_formal(self):
        # "Always «вы» to the player" leaked into dialogue between characters
        # ("Брат, вы в порядке?"); the rule must not come back.
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(provider().translate_batch(["Hello"], "en", "ru"))

        prompt = system_prompt(api.chat_calls[0])
        self.assertNotIn("всегда обращайся на «вы»", prompt)
        self.assertIn("по отношениям", prompt)
        self.assertIn("повествование, обращённые к игроку, — всегда на «вы»", prompt)

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

    def test_gender_only_after_original_moves_to_translation(self):
        api = FakeApi(chat_contents=["1. Куон улыбается\n---\nKuon (ж) → Куон"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Kuon smiles"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Kuon", "Куон (ж)")])

    def test_translation_side_gender_wins_over_original_side(self):
        api = FakeApi(chat_contents=["1. Куон улыбается\n---\nKuon (м) → Куон (ж)"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Kuon smiles"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Kuon", "Куон (ж)")])

    def test_gender_mark_is_stored_in_one_form(self):
        # "Куон(ж)" without a space is the same entry as "Куон (ж)"; a later
        # gender for a genderless entry must still be recognized as the
        # same name.
        api = FakeApi(chat_contents=[
            "1. Куон\n---\nKuon → Куон(ж)",
            "1. Хаку\n---\nHaku → Хаку",
            "1. Хаку кивает\n---\nHaku (м) → Хаку",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Kuon"], "en", "ru"))
            run(p.translate_batch(["Haku"], "en", "ru"))
            run(p.translate_batch(["Haku nods, he agrees"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Kuon", "Куон (ж)"), ("Haku", "Хаку (м)")])

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

    def test_gender_is_corrected_with_the_same_translation(self):
        api = FakeApi(chat_contents=[
            "1. Карлах\n---\nKarlach → Карлах (м)",
            "1. Карлах улыбается\n---\nKarlach → Карлах (ж)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Karlach"], "en", "ru"))
            run(p.translate_batch(["Karlach smiles, she is happy"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Karlach", "Карлах (ж)")])

    def test_another_translation_of_the_name_is_ignored_even_with_gender(self):
        api = FakeApi(chat_contents=[
            "1. Гейл\n---\nGale → Гейл",
            # Another translation of the name: ignored, gender included.
            "1. Гэйл\n---\nGale → Гэйл (м)",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Gale"], "en", "ru"))
            run(p.translate_batch(["Gale"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Gale", "Гейл")])

    def test_answer_without_gender_does_not_remove_it(self):
        api = FakeApi(chat_contents=[
            "1. Карлах\n---\nKarlach → Карлах (ж)",
            "1. Карлах\n---\nKarlach → Карлах",
        ])
        p = provider()
        with fake_http(api):
            run(p.translate_batch(["Karlach"], "en", "ru"))
            run(p.translate_batch(["Karlach"], "en", "ru"))

        self.assertEqual(p.glossary_entries(), [("Karlach", "Карлах (ж)")])

    def test_prompt_asks_for_initial_form_and_allows_gender_correction(self):
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(provider().translate_batch(["Hello"], "en", "ru"))

        prompt = user_prompt(api.chat_calls[0])
        self.assertIn("в начальной форме", prompt)
        self.assertIn("кроме исправлений пола и формы", prompt)
        self.assertIn("Исправление формы", prompt)
        self.assertIn("(форма)", prompt)

    def test_prompt_forbids_interface_labels(self):
        api = FakeApi(chat_contents=["1. Привет\n---"])
        with fake_http(api):
            run(provider().translate_batch(["Hello"], "en", "ru"))

        prompt = user_prompt(api.chat_calls[0])
        self.assertIn("Не выписывай элементы интерфейса игры", prompt)
        self.assertIn("A Next, Y Auto Mode", prompt)

    def seeded(self, translation, term="Ukon"):
        """A provider whose glossary already holds `term → translation`."""
        api = FakeApi(chat_contents=[f"1. x\n---\n{term} → {translation}"])
        p = provider()
        with fake_http(api):
            run(p.translate_batch([term], "en", "ru"))
        return p

    def answer(self, p, glossary_part, text="Ukon raises his fan"):
        api = FakeApi(chat_contents=[f"1. x\n---\n{glossary_part}"])
        with fake_http(api):
            run(p.translate_batch([text], "en", "ru"))
        return dict(p.glossary_entries())

    def test_marked_form_fix_replaces_the_ending(self):
        p = self.seeded("Уконом")

        self.assertEqual(self.answer(p, "Ukon → Укон (форма)"), {"Ukon": "Укон"})

    def test_form_fix_keeps_gender_and_handles_feminine_ending(self):
        p = self.seeded("Минтару (ж)", term="Minthara")

        entries = self.answer(p, "Minthara → Минтара (форма)", text="Minthara waits")

        self.assertEqual(entries, {"Minthara": "Минтара (ж)"})

    def test_form_fix_mark_may_come_before_the_gender(self):
        p = self.seeded("Мунэтику", term="Munechika")

        entries = self.answer(p, "Munechika → Мунэтика (форма) (ж)", text="Munechika waits")

        self.assertEqual(entries, {"Munechika": "Мунэтика (ж)"})

    def test_unmarked_other_form_does_not_replace_the_entry(self):
        # The model also writes oblique forms by mistake: a good entry must
        # not turn into "Укону" just because it was re-listed so.
        p = self.seeded("Укон")

        self.assertEqual(self.answer(p, "Ukon → Укону", text="Give the fan to Ukon"), {"Ukon": "Укон"})

    def test_marked_respelling_is_rejected(self):
        p = self.seeded("Оштор", term="Oshtor")

        entries = self.answer(p, "Oshtor → Осутор (форма)", text="Oshtor smiles")

        self.assertEqual(entries, {"Oshtor": "Оштор"})

    def test_form_mark_on_a_new_entry_is_dropped(self):
        p = provider()

        entries = self.answer(p, "Ukon → Укон (форма)")

        self.assertEqual(entries, {"Ukon": "Укон"})

    def test_unmarked_form_proposed_twice_replaces_the_ending(self):
        # Seen live: for a stored "Минтарой (ж)" the model re-listed
        # "Минтара (ж)" without the mark.
        p = self.seeded("Минтарой (ж)", term="Minthara")

        first = self.answer(p, "Minthara → Минтара (ж)", text="Talk to Minthara")
        second = self.answer(p, "Minthara → Минтара (ж)", text="Minthara waits")

        self.assertEqual(first, {"Minthara": "Минтарой (ж)"})
        self.assertEqual(second, {"Minthara": "Минтара (ж)"})

    def test_a_different_proposal_starts_the_votes_again(self):
        p = self.seeded("Укон")

        self.answer(p, "Ukon → Укону", text="Give it to Ukon")
        entries = self.answer(p, "Ukon → Уконе", text="Think of Ukon")

        self.assertEqual(entries, {"Ukon": "Укон"})

    def test_one_answer_is_one_vote(self):
        p = self.seeded("Уконом")

        entries = self.answer(p, "Ukon → Укон\nUkon → Укон")

        self.assertEqual(entries, {"Ukon": "Уконом"})

    def test_unmarked_respelling_never_counts(self):
        p = self.seeded("Оштор", term="Oshtor")

        self.answer(p, "Oshtor → Осутор", text="Oshtor smiles")
        entries = self.answer(p, "Oshtor → Осутор", text="Oshtor laughs")

        self.assertEqual(entries, {"Oshtor": "Оштор"})

    def test_reset_forgets_pending_votes(self):
        p = self.seeded("Уконом")
        self.answer(p, "Ukon → Укон")
        # Same game, same pair; the game id is empty here, so the form
        # methods cannot reach it: the switch drops the votes the same way.
        p.set_game("Disco Elysium")
        p.set_game("Baldur's Gate 3")
        self.answer(p, "Ukon → Уконом")  # a fresh entry, as the model writes it
        entries = self.answer(p, "Ukon → Укон")

        # One vote after the switch is not enough.
        self.assertEqual(entries, {"Ukon": "Уконом"})

    def test_name_and_gender_for_the_window(self):
        self.assertEqual(gp.name_and_gender("Шэдоухарт (ж)"), ("Шэдоухарт", "ж"))
        self.assertEqual(gp.name_and_gender("Гейл (м)"), ("Гейл", "м"))
        self.assertEqual(gp.name_and_gender("Врата Балдура"), ("Врата Балдура", ""))

    def test_ending_change_rule(self):
        cases = [
            ("Уконом", "Укон", True),
            ("Минтару", "Минтара", True),
            ("Ким Кицураги", "Ким Кицураги", False),   # nothing changed
            ("Кима Кицураги", "Ким Кицураги", True),   # one word of two
            ("Оштор", "Осутор", False),                # respelling
            ("Осхтор", "Осётор", False),
            ("Ию", "Ия", False),                       # stem too short to tell
            ("Укон", "Укон Великий", False),           # another number of words
            ("Укон", "Уконоооов", False),              # tail too long
        ]
        for old, new, expected in cases:
            with self.subTest(old=old, new=new):
                self.assertEqual(gp._is_ending_change(old, new), expected)

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


class GigaChatPlusSpeechTest(unittest.TestCase):
    """Speech manner notes: parsing, the update rule, the prompt block."""

    def setUp(self):
        self.p = provider()
        # Ukon and Kuon are in the glossary from the start.
        self.say("Ukon and Kuon", "Ukon → Укон (м)\nKuon → Куон (ж)")

    def say(self, text, glossary_part):
        api = FakeApi(chat_contents=[f"1. x\n---\n{glossary_part}"])
        with fake_http(api):
            run(self.p.translate_batch([text], "en", "ru"))
        return api

    def speech(self, term="Ukon"):
        return self.p._glossary[gp._match_key(term)].speech

    def test_parse_speech_lines(self):
        text = (
            "Ukon → Укон (м)\n"
            "Речь: Ukon — грубо, коротко, на «ты»\n"
            "- речь: «Куон» – игриво, с усмешкой.\n"
            "Речь: Haku - лениво\n"
            "Речь: Nekone без тире\n"
            "Речь: Oshtor — " + "очень " * 20 + "\n"
        )
        self.assertEqual(gp._parse_speech(text), [
            ("Ukon", "грубо, коротко, на «ты»"),
            ("Куон", "игриво, с усмешкой"),
            ("Haku", "лениво"),
        ])

    def test_speech_line_before_the_separator_does_not_reach_the_screen(self):
        api = FakeApi(chat_contents=["1. Двигаемся!\nРечь: Ukon — грубо, коротко\n---"])
        with fake_http(api):
            result = run(self.p.translate_batch(["Ukon: Move!"], "en", "ru"))

        self.assertEqual(result, ["Двигаемся!"])
        # Not lost either: taken as the note it is.
        self.assertEqual(self.speech(), "грубо, коротко")

    def test_numbered_line_starting_with_speech_word_is_a_translation(self):
        api = FakeApi(chat_contents=["1. Речь: короткая и злая\n---"])
        with fake_http(api):
            result = run(self.p.translate_batch(["Speech: short and angry"], "en", "ru"))

        self.assertEqual(result, ["Речь: короткая и злая"])

    def test_first_note_is_taken_at_once(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, коротко")

        self.assertEqual(self.speech(), "грубо, коротко")

    def test_note_by_translated_name(self):
        self.say("Ukon: Move!", "Речь: Укон — грубо, коротко")

        self.assertEqual(self.speech(), "грубо, коротко")

    def test_same_manner_in_other_words_keeps_the_note(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, самоуверенно")
        self.say("Ukon: Enough talk.", "Речь: Ukon — грубо, коротко, на «ты»")
        self.say("Ukon: Tch.", "Речь: Ukon — коротко, грубо")

        self.assertEqual(self.speech(), "грубо, самоуверенно")

    def test_two_different_screens_in_a_row_replace_the_note(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, самоуверенно")
        self.say("Ukon: Your Highness…", "Речь: Ukon — почтительно, официально, церемонно")
        self.assertEqual(self.speech(), "грубо, самоуверенно")

        self.say("Ukon: Pray, rest easy.", "Речь: Ukon — почтительно, торжественно")

        # The latest description wins.
        self.assertEqual(self.speech(), "почтительно, торжественно")

    def test_echo_of_the_stored_note_does_not_break_the_run(self):
        # Seen live: on a narration screen the model copies the stored note
        # of a character who only is mentioned.
        self.say("Ukon: Move!", "Речь: Ukon — грубо, самоуверенно")
        self.say("Ukon: Your Highness…", "Речь: Ukon — почтительно, официально")
        self.say("Ukon walks beside the palanquin.", "Речь: Ukon — Грубо, самоуверенно")
        self.say("Ukon: Pray, rest easy.", "Речь: Ukon — почтительно, торжественно")

        self.assertEqual(self.speech(), "почтительно, торжественно")

    def test_one_different_scene_does_not_replace_the_note(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, самоуверенно")
        self.say("Ukon: Your Highness…", "Речь: Ukon — почтительно, официально")
        self.say("Ukon: Tch. Kids.", "Речь: Ukon — грубо, пренебрежительно")
        self.say("Ukon: Your Highness…", "Речь: Ukon — почтительно, официально")

        self.assertEqual(self.speech(), "грубо, самоуверенно")

    def test_short_words_do_not_make_notes_alike(self):
        # "на «ты»" and "на «вы»" share "на": it must not count.
        self.say("Ukon: Move!", "Речь: Ukon — грубо, на «ты»")
        self.say("Ukon: Your Highness…", "Речь: Ukon — почтительно, на «вы»")
        self.say("Ukon: Pray, rest easy.", "Речь: Ukon — церемонно, на «вы»")

        self.assertEqual(self.speech(), "церемонно, на «вы»")

    def test_one_answer_is_one_screen(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, самоуверенно")
        self.say(
            "Ukon: Your Highness…",
            "Речь: Ukon — почтительно, официально\nРечь: Укон — почтительно, церемонно",
        )

        self.assertEqual(self.speech(), "грубо, самоуверенно")

    def test_speaker_not_on_screen_or_unknown_is_ignored(self):
        self.say("Kuon laughs.", "Речь: Ukon — грубо\nРечь: Nekone — вежливо")

        self.assertEqual(self.speech(), "")
        self.assertNotIn("nekone", self.p._glossary)

    def test_prompt_has_notes_of_speakers_on_screen_outside_the_glossary(self):
        self.say("Ukon and Kuon", "Речь: Ukon — грубо, коротко\nРечь: Kuon — игриво")

        api = self.say("Ukon: Move!", "")
        prompt = user_prompt(api.chat_calls[0])

        glossary_block = prompt.split("\n\n")[0]
        self.assertNotIn("грубо", glossary_block)
        self.assertIn("Как персонажи говорили раньше (манера может меняться):\nUkon — грубо, коротко", prompt)
        # Kuon is not on this screen: her note is not sent.
        self.assertNotIn("игриво", prompt)
        self.assertIn("Манера речи:", prompt)

    def test_no_notes_no_block(self):
        api = self.say("Ukon: Move!", "")

        self.assertNotIn("Как персонажи говорили раньше", user_prompt(api.chat_calls[0]))

    def test_gender_and_form_fixes_keep_the_note(self):
        self.say("Ukon: Move!", "Речь: Ukon — грубо, коротко")
        # A form fix and a gender fix of the same entry.
        self.say("Ukon nods", "Ukon → Укона (форма)")
        self.say("Ukon nods", "Ukon → Укона (ж)")

        entry = self.p._glossary["ukon"]
        self.assertEqual(entry.translation, "Укона (ж)")
        self.assertEqual(entry.speech, "грубо, коротко")


class GigaChatPlusSpeechStoreTest(unittest.TestCase):

    def test_note_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = GlossaryStore(os.path.join(tmp, "glossary.db"))
            try:
                p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY, glossary_store=store)
                p.set_game("Baldur's Gate 3", BG3_ID)
                api = FakeApi(chat_contents=["1. x\n---\nKarlach → Карлах (ж)\nРечь: Karlach — бурно, по-свойски"])
                with fake_http(api):
                    run(p.translate_batch(["Karlach: Woo!"], "en", "ru"))

                restarted = gp.GigaChatPlusTranslateProvider(api_key=API_KEY, glossary_store=store)
                restarted.set_game("Baldur's Gate 3", BG3_ID)
                api = FakeApi(chat_contents=["1. x\n---"])
                with fake_http(api):
                    run(restarted.translate_batch(["Karlach: Hey, soldier."], "en", "ru"))

                self.assertIn("Karlach — бурно, по-свойски", user_prompt(api.chat_calls[0]))
            finally:
                store.close()


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


class GigaChatPlusInterfaceLabelTest(unittest.TestCase):
    """Interface labels proposed by the model never reach the glossary."""

    def test_glued_labels_are_split_into_words(self):
        self.assertEqual(gp._term_words("lb Back"), ["lb", "back"])
        self.assertEqual(gp._term_words("SaveBack"), ["save", "back"])
        self.assertEqual(gp._term_words("QSaveQLoad"), ["q", "save", "q", "load"])
        self.assertEqual(gp._term_words("BHide Window"), ["b", "hide", "window"])
        self.assertEqual(gp._term_words("Q.Save"), ["q", "save"])
        self.assertEqual(gp._term_words("Skip>>"), ["skip"])
        # A run of capitals is one word; names keep their parts.
        self.assertEqual(gp._term_words("HP"), ["hp"])
        self.assertEqual(gp._term_words("LeBlanc"), ["le", "blanc"])
        self.assertEqual(gp._term_words("Lae'zel"), ["lae", "zel"])

    def test_interface_terms(self):
        for term in ("lb Back", "Auto Mode", "AutoMode", "Y Auto Mode", "BHide Window", "Backlog",
                     "ASave", "SaveBack", "LB Backlog", "QSaveQLoad", "RE Skip", "Skip>>",
                     "Message Log", "A Next", ">>", "42"):
            with self.subTest(term=term):
                self.assertTrue(gp._is_interface_term(term))

    def test_names_are_not_interface_terms(self):
        # Whole words only: a listed word inside a longer word does not count.
        for term in ("Shadowheart", "Backstreet", "Saveria", "Skipper", "Autumn Court",
                     "Logan", "LeBlanc", "Lae'zel", "X-23", "Agent Y", "2B", "Selûne", "Ukon"):
            with self.subTest(term=term):
                self.assertFalse(gp._is_interface_term(term))

    def test_interface_labels_are_dropped_and_names_kept(self):
        # A visual novel screen: a line of dialogue and the button bar
        # glued by OCR; the model lists the buttons next to the name.
        api = FakeApi(chat_contents=[
            "1. Хаку, ты покраснел!\n2. Назад Скрыть окно Сохранить\n---\n"
            "Haku → Хаку (м)\nlb Back → Назад\nBHide Window → Скрыть окно\nSaveBack → Сохранить"
        ])
        p = provider("Utawarerumono")
        with fake_http(api), self.assertLogs(gp.logger, level="INFO") as logs:
            result = run(p.translate_batch(["Haku, you're blushing!", "lb Back BHide Window SaveBack"], "en", "ru"))

        self.assertEqual(result, ["Хаку, ты покраснел!", "Назад Скрыть окно Сохранить"])
        self.assertEqual(p.glossary_entries(), [("Haku", "Хаку (м)")])
        self.assertTrue(any("interface labels dropped: lb Back, BHide Window, SaveBack" in line
                            for line in logs.output))


class GigaChatPlusBlackListTest(unittest.TestCase):
    """Forbid an entry, allow it again: the model must not bring it back."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self._tmp.name, "glossary.db"))

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def provider(self, game="Utawarerumono", game_id=BG3_ID):
        p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY, glossary_store=self.store)
        p.set_game(game, game_id)
        return p

    def translate(self, p, answer, line="Haku Gallery"):
        """One screen; `answer` is the model's whole reply."""
        api = FakeApi(chat_contents=[answer])
        with fake_http(api):
            run(p.translate_batch([line], "en", "ru"))
        return api

    def learned(self, p):
        # "Gallery" is an interface label the stop list does not know
        # (it is left to the model): only the black list can keep it out.
        self.translate(p, "1. Хаку Галерея\n---\nHaku → Хаку (м)\nGallery → Галерея")
        return p

    def saved_terms(self):
        return [entry.term for entry in self.store.load_entries(BG3_KEY)]

    def test_forbidden_term_is_not_added_back(self):
        p = self.learned(self.provider())

        self.assertTrue(p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery"))
        api = self.translate(p, "1. Хаку Галерея\n---\nGallery → Галерея")

        self.assertEqual(p.glossary_entries(), [("Haku", "Хаку (м)")])
        self.assertEqual(self.saved_terms(), ["Haku"])
        self.assertNotIn("Gallery →", user_prompt(api.chat_calls[0]))

    def test_black_list_survives_a_restart(self):
        self.learned(self.provider()).forbid_glossary_entry(BG3_ID, "en", "ru", "gallery")

        restarted = self.provider()
        self.translate(restarted, "1. Хаку Галерея\n---\nGallery → Галерея")

        self.assertEqual(restarted.glossary_entries(), [("Haku", "Хаку (м)")])

    def test_deleting_the_last_entry_keeps_the_black_list(self):
        # The store drops a glossary left without entries; with a black list
        # it is not empty and must stay, or the forbidden term comes back.
        p = self.learned(self.provider())
        p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery")

        p.delete_glossary_entry(BG3_ID, "en", "ru", "haku")

        self.assertEqual([item.term for item in self.store.load_rejected(BG3_KEY)], ["Gallery"])
        restarted = self.provider()
        self.translate(restarted, "1. Галерея\n---\nGallery → Галерея", line="Gallery")
        self.assertEqual(restarted.glossary_entries(), [])

    def test_black_list_is_shown_with_translation_and_reason(self):
        p = self.learned(self.provider())

        p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery")

        shown = p.rejected_for(BG3_ID, "en", "ru")
        self.assertEqual([(item.term, item.translation, item.reason) for item in shown],
                         [("Gallery", "Галерея", gp.REJECTED_BY_USER)])

    def test_forbid_in_another_game_goes_to_its_saved_glossary(self):
        self.learned(self.provider())
        p = self.provider(game="Disco Elysium", game_id="632470")

        self.assertTrue(p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery"))

        self.assertEqual(self.saved_terms(), ["Haku"])
        self.assertEqual([item.term for item in p.rejected_for(BG3_ID, "en", "ru")], ["Gallery"])
        name = self.store._conn.execute("SELECT game_name FROM glossaries").fetchone()[0]
        self.assertEqual(name, "Utawarerumono")

    def test_allowed_term_may_be_added_again(self):
        p = self.learned(self.provider())
        p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery")

        self.assertTrue(p.allow_rejected_term(BG3_ID, "en", "ru", "gallery"))
        self.translate(p, "1. Хаку Галерея\n---\nGallery → Галерея")

        self.assertEqual(p.rejected_for(BG3_ID, "en", "ru"), [])
        self.assertIn(("Gallery", "Галерея"), p.glossary_entries())

    def test_reset_clears_the_black_list(self):
        p = self.learned(self.provider())
        p.forbid_glossary_entry(BG3_ID, "en", "ru", "gallery")

        self.assertTrue(p.reset_glossary(BG3_ID, "en", "ru"))

        self.assertEqual(p.rejected_for(BG3_ID, "en", "ru"), [])
        self.assertEqual(self.store.load_rejected(BG3_KEY), [])

    def test_forbidding_a_missing_entry_or_without_a_game_reports_false(self):
        p = self.learned(self.provider())

        self.assertFalse(p.forbid_glossary_entry(BG3_ID, "en", "ru", "nobody"))
        self.assertFalse(p.forbid_glossary_entry("", "en", "ru", "gallery"))
        self.assertFalse(p.allow_rejected_term(BG3_ID, "en", "ru", "nobody"))


class GigaChatPlusCleanupAnswerTest(unittest.TestCase):
    """The cleanup answer is trusted only when it is numbers and commas."""

    def test_numbers_are_accepted(self):
        self.assertEqual(gp._parse_cleanup_answer("3, 7,12", 20), [3, 7, 12])
        self.assertEqual(gp._parse_cleanup_answer(" 3,7. ", 20), [3, 7])

    def test_nothing_to_delete(self):
        self.assertEqual(gp._parse_cleanup_answer("нет", 20), [])
        self.assertEqual(gp._parse_cleanup_answer("Нет.", 20), [])

    def test_untrusted_answers(self):
        cases = {
            "empty": "",
            # Echoed entries: the digits of "2B" and "X-23" must not delete
            # entries 2 and 23.
            "echoed entries": "3. 2B → Ту-Би, 7. X-23 → Икс-23",
            "explanation": "Интерфейс: 3, 7",
            "out of range": "3, 21",
            "zero": "0",
            "repeated": "3, 3",
            "spaces only": "3 7",
            "superscript digit": "²",
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.assertIsNone(gp._parse_cleanup_answer(answer, 20))


class ManualTimers:
    """Timer factory for tests: records timers, fires them on demand."""

    def __init__(self):
        self.started = []

    def __call__(self, seconds, callback):
        timer = _ManualTimer(seconds, callback)
        self.started.append(timer)
        return timer

    def pending(self):
        return [timer for timer in self.started if not timer.cancelled]

    def fire(self):
        """Fire the one pending timer, as the real one would after its delay."""
        pending = self.pending()
        assert len(pending) == 1, f"expected one pending timer, got {len(pending)}"
        pending[0].cancelled = True
        pending[0].callback()


class _ManualTimer:
    def __init__(self, seconds, callback):
        self.seconds = seconds
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


# Haku and Kuon are characters (gender), the rest are interface labels the
# stop list does not know, as the model would have listed them.
CLEANUP_GLOSSARY = [
    ("Haku", "Хаку (м)"),
    ("Gallery", "Галерея"),
    ("Kuon", "Куон (ж)"),
    ("Text Speed", "Скорость текста"),
    ("Yamato", "Ямато"),
]


class GigaChatPlusCleanupTest(unittest.TestCase):
    """When the cleanup runs, what it removes and what it never touches."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GlossaryStore(os.path.join(self._tmp.name, "glossary.db"))
        self.now = 1000.0
        self.timers = ManualTimers()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def provider(self, glossary=CLEANUP_GLOSSARY):
        """A provider whose saved glossary holds `glossary`, loaded as on a game start."""
        entries = [gp.GlossaryEntry(gp._match_key(term), term, translation, 1)
                   for term, translation in glossary]
        self.store.replace_entries(BG3_KEY, "Utawarerumono", entries)
        p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY, glossary_store=self.store)
        p._clock = lambda: self.now
        p._timer_factory = self.timers
        p.set_game("Utawarerumono", BG3_ID)
        return p

    def translate(self, p, answer="1. Привет\n---", line="Hello"):
        api = FakeApi(chat_contents=[answer])
        with fake_http(api):
            run(p.translate_batch([line], "en", "ru"))

    def fire_after_idle(self, p, cleanup_answer):
        """Let the idle time pass and fire the timer; returns the fake API."""
        self.now += gp._CLEANUP_IDLE_SECONDS
        api = FakeApi(chat_contents=[cleanup_answer])
        with fake_http(api), self.assertLogs(gp.logger, level="INFO") as logs:
            self.timers.fire()
        self.logs = logs.output
        return api

    def terms(self, p):
        return [term for term, _ in p.glossary_entries()]

    # -- what is removed --

    def test_labels_are_moved_to_the_black_list(self):
        p = self.provider()
        self.translate(p)

        api = self.fire_after_idle(p, "2, 4")

        self.assertEqual(self.terms(p), ["Haku", "Kuon", "Yamato"])
        self.assertCountEqual(
            [(item.term, item.reason) for item in self.store.load_rejected(BG3_KEY)],
            [("Gallery", gp.REJECTED_BY_CLEANUP), ("Text Speed", gp.REJECTED_BY_CLEANUP)],
        )
        prompt = user_prompt(api.chat_calls[0])
        self.assertIn("Ответь только номерами", prompt)
        self.assertIn("2. Gallery → Галерея", prompt)
        self.assertIn("«Utawarerumono»", system_prompt(api.chat_calls[0]))

    def test_removed_label_is_not_added_back(self):
        p = self.provider()
        self.translate(p)
        self.fire_after_idle(p, "2")

        self.translate(p, "1. Галерея\n---\nGallery → Галерея", line="Gallery")

        self.assertNotIn("Gallery", self.terms(p))

    def test_characters_are_never_removed(self):
        p = self.provider()
        self.translate(p)

        self.fire_after_idle(p, "1, 2")

        self.assertEqual(self.terms(p), ["Haku", "Kuon", "Text Speed", "Yamato"])
        self.assertTrue(any("kept characters: Haku" in line for line in self.logs))

    def test_answer_naming_more_than_half_is_not_trusted(self):
        p = self.provider()
        self.translate(p)

        self.fire_after_idle(p, "2, 4, 5")

        self.assertEqual(len(p.glossary_entries()), 5)
        self.assertEqual(self.store.load_rejected(BG3_KEY), [])

    def test_untrusted_answer_removes_nothing(self):
        p = self.provider()
        self.translate(p)

        self.fire_after_idle(p, "2. Gallery → Галерея")

        self.assertEqual(len(p.glossary_entries()), 5)

    def test_refusal_removes_nothing_and_is_not_shown(self):
        p = self.provider()
        self.translate(p)
        self.now += gp._CLEANUP_IDLE_SECONDS

        api = FakeApi(chat_responses=[chat_response(REFUSAL, "blacklist")])
        with fake_http(api):
            self.timers.fire()

        self.assertEqual(len(p.glossary_entries()), 5)

    def test_result_is_dropped_if_the_user_edited_the_glossary_meanwhile(self):
        p = self.provider()
        self.translate(p)
        self.now += gp._CLEANUP_IDLE_SECONDS
        api = FakeApi(chat_contents=["2, 4"])

        def post_and_delete(*args, **kwargs):
            response = api.post(*args, **kwargs)
            if args and args[0].endswith("/chat/completions"):
                p.delete_glossary_entry(BG3_ID, "en", "ru", "yamato")
            return response

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post_and_delete), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            self.timers.fire()

        self.assertEqual(self.terms(p), ["Haku", "Gallery", "Kuon", "Text Speed"])

    # -- when it runs --

    def test_saved_glossary_is_cleaned_after_the_first_translation(self):
        p = self.provider()

        self.translate(p)

        self.assertEqual(len(self.timers.pending()), 1)
        self.assertEqual(self.timers.pending()[0].seconds, gp._CLEANUP_IDLE_SECONDS)

    def test_each_translation_restarts_the_timer(self):
        p = self.provider()
        self.translate(p)
        self.translate(p)

        self.assertEqual(len(self.timers.started), 2)
        self.assertEqual(len(self.timers.pending()), 1)

    def test_no_cleanup_while_a_translation_is_running(self):
        p = self.provider()
        self.translate(p)
        self.now += gp._CLEANUP_IDLE_SECONDS
        p._translations_running = 1

        api = FakeApi()
        with fake_http(api):
            self.timers.fire()

        self.assertEqual(api.chat_calls, [])

    def test_no_cleanup_before_the_idle_time(self):
        p = self.provider()
        self.translate(p)
        self.now += gp._CLEANUP_IDLE_SECONDS - 1

        api = FakeApi()
        with fake_http(api):
            self.timers.fire()

        self.assertEqual(api.chat_calls, [])

    def test_not_more_often_than_the_interval(self):
        p = self.provider()
        self.translate(p)
        self.fire_after_idle(p, "нет")
        p._new_since_cleanup = gp._CLEANUP_AFTER_NEW_ENTRIES

        self.translate(p)
        self.assertEqual(self.timers.pending(), [])

        self.now += gp._CLEANUP_MIN_INTERVAL_SECONDS
        self.translate(p)
        self.assertEqual(len(self.timers.pending()), 1)

    def test_failed_cleanup_is_not_retried_at_once(self):
        p = self.provider()
        self.translate(p)
        self.now += gp._CLEANUP_IDLE_SECONDS
        api = FakeApi(chat_responses=[chat_response(REFUSAL, "blacklist")])
        with fake_http(api):
            self.timers.fire()

        self.translate(p)

        self.assertEqual(self.timers.pending(), [])

    def test_new_entries_trigger_the_cleanup(self):
        p = self.provider()
        self.translate(p)
        self.fire_after_idle(p, "нет")
        self.now += gp._CLEANUP_MIN_INTERVAL_SECONDS
        names = ["Aaron", "Bella", "Cyrus", "Dora", "Ezra", "Fiona", "Gideon", "Hana", "Ivan"]

        # 9 new entries: below the threshold of 10.
        self.translate(p, "1. x\n---\n" + "\n".join(f"{n} → {n}" for n in names[:5]), " ".join(names))
        self.translate(p, "1. x\n---\n" + "\n".join(f"{n} → {n}" for n in names[5:]), " ".join(names))
        self.assertEqual(self.timers.pending(), [])

        self.translate(p, "1. x\n---\nJudith → Джудит", "Judith")
        self.assertEqual(len(self.timers.pending()), 1)

    def test_nearly_full_glossary_is_cleaned_sooner(self):
        filler = [(f"Name{i}", f"Имя{i}") for i in range(41)]
        p = self.provider(filler)
        self.translate(p)
        self.fire_after_idle(p, "нет")
        self.now += gp._CLEANUP_MIN_INTERVAL_SECONDS

        self.translate(p, "1. x\n---\nAaron → Аарон\nBella → Белла\nCyrus → Сайрус", "Aaron Bella Cyrus")

        self.assertEqual(len(self.timers.pending()), 1)

    def test_switch_off(self):
        with patch.object(gp, "_GLOSSARY_CLEANUP_ENABLED", False):
            p = self.provider()
            self.translate(p)

        self.assertEqual(self.timers.started, [])


class GigaChatClientOneRequestAtATimeTest(unittest.TestCase):
    """Chat requests never overlap, whoever sends them."""

    def test_requests_from_two_threads_do_not_overlap(self):
        import threading
        import time as time_module

        api = FakeApi(chat_contents=["1. a", "1. b"])
        state = {"running": 0, "most": 0}
        state_lock = threading.Lock()

        def slow_post(*args, **kwargs):
            is_chat = args and args[0].endswith("/chat/completions")
            if is_chat:
                with state_lock:
                    state["running"] += 1
                    state["most"] = max(state["most"], state["running"])
                time_module.sleep(0.05)
            try:
                return api.post(*args, **kwargs)
            finally:
                if is_chat:
                    with state_lock:
                        state["running"] -= 1

        client = GigaChatClient(API_KEY)
        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=slow_post), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            threads = [
                threading.Thread(target=client.chat, args=([{"role": "user", "content": "x"}],))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(api.chat_calls), 2)
        self.assertEqual(state["most"], 1)

    def test_stale_screen_is_not_sent(self):
        api = FakeApi(chat_contents=["1. b", "1. c"])
        client = GigaChatClient(API_KEY)
        old_screen = client.begin_screen()
        new_screen = client.begin_screen()

        with fake_http(api):
            with self.assertRaises(TranslationSupersededError):
                client.chat([{"role": "user", "content": "x"}], screen=old_screen)
            client.chat([{"role": "user", "content": "x"}], screen=new_screen)
            # Not a screen (the glossary cleanup): never stale.
            client.chat([{"role": "user", "content": "x"}])

        self.assertEqual(len(api.chat_calls), 2)

    def test_only_the_latest_of_queued_screens_is_sent(self):
        # Screen 1 is on its way; meanwhile the user closes the overlay and
        # asks again twice. Screen 2 waits for its turn and is dropped:
        # screen 3 is newer. Screen 3 is sent.
        import threading
        import time as time_module

        api = FakeApi(chat_contents=["1. один", "1. три"])
        first_sent = threading.Event()
        release_first = threading.Event()

        def post(*args, **kwargs):
            if args and args[0].endswith("/chat/completions") and not first_sent.is_set():
                first_sent.set()
                release_first.wait(5)
            return api.post(*args, **kwargs)

        p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY)
        p.set_game("")
        results = {}

        def translate(name, line):
            try:
                results[name] = run(p.translate_batch([line], "en", "ru"))
            except TranslationSupersededError:
                results[name] = "superseded"

        def wait_for(condition):
            deadline = time_module.monotonic() + 5
            while not condition():
                self.assertLess(time_module.monotonic(), deadline, "timed out")
                time_module.sleep(0.01)

        from providers import gigachat_client as gcc
        with patch.object(gcc.requests, "post", side_effect=post), \
                patch.object(gcc.requests, "get", side_effect=api.get):
            threads = {}
            for name, line in (("first", "One"), ("second", "Two"), ("third", "Three")):
                threads[name] = threading.Thread(target=translate, args=(name, line))
            threads["first"].start()
            self.assertTrue(first_sent.wait(5))
            threads["second"].start()
            wait_for(lambda: p._client._latest_screen == 2)
            threads["third"].start()
            wait_for(lambda: p._client._latest_screen == 3)
            release_first.set()
            for thread in threads.values():
                thread.join(5)

        self.assertEqual(results, {"first": ["один"], "second": "superseded", "third": ["три"]})
        self.assertEqual(len(api.chat_calls), 2)

    def test_glossary_cleanup_is_never_dropped_as_stale(self):
        # The cleanup timer is started while a screen is being translated;
        # even if its thread inherited that screen's number, a newer screen
        # must not drop the cleanup.
        from providers.gigachat_translate import _current_screen

        timers = ManualTimers()
        p = gp.GigaChatPlusTranslateProvider(api_key=API_KEY)
        p._timer_factory = timers
        p._clock = lambda: 1000.0
        p.set_game("Utawarerumono")
        api = FakeApi(chat_contents=["1. Хаку Галерея\n---\nHaku → Хаку (м)\nGallery → Галерея"])
        with fake_http(api):
            run(p.translate_batch(["Haku Gallery"], "en", "ru"))
        # Make a cleanup due and end a translation, which starts the timer.
        with p._glossary_lock:
            p._cleanup_after_load = True
            p._translations_running += 1
        p._translation_finished()

        stale = p._client.begin_screen()
        p._client.begin_screen()
        token = _current_screen.set(stale)
        cleanup_api = FakeApi(chat_contents=["2"])
        try:
            p._clock = lambda: 1000.0 + gp._CLEANUP_IDLE_SECONDS
            with fake_http(cleanup_api):
                timers.fire()
        finally:
            _current_screen.reset(token)

        self.assertEqual(len(cleanup_api.chat_calls), 1)
        self.assertEqual(p.glossary_entries(), [("Haku", "Хаку (м)")])


if __name__ == "__main__":
    unittest.main()
