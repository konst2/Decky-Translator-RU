"""
GlossaryStore: словари, сохранённые по методу, игре и языковой паре.

Каждый тест работает с новым файлом базы во временной папке: проверяется
настоящее поведение SQLite (транзакции, AUTOINCREMENT, переоткрытие базы),
а не база в памяти.
"""

import os
import sqlite3
import tempfile
import unittest

from providers.glossary_store import (
    REJECTED_BY_CLEANUP,
    REJECTED_BY_USER,
    GlossaryEntry,
    GlossaryKey,
    GlossaryStore,
    RejectedTerm,
)

BG3 = GlossaryKey("gigachat_plus", "1086940", "en", "ru")


def entry(term, translation, last_used_at):
    return GlossaryEntry(term.casefold(), term, translation, last_used_at)


def rejected(term, translation, rejected_at, reason=REJECTED_BY_USER):
    return RejectedTerm(term.casefold(), term, translation, rejected_at, reason)


class GlossaryStoreTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "glossary.db")
        self.store = GlossaryStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def count(self, table):
        # Взгляд прямо в таблицы: часть гарантий (нет записей без словаря,
        # нет пустых словарей) через публичные методы не видна.
        return self.store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def glossary_id(self, key):
        row = self.store._conn.execute(
            "SELECT id FROM glossaries WHERE method=? AND game_id=? "
            "AND source_lang=? AND target_lang=?",
            [key.method, key.game_id, key.source_lang, key.target_lang],
        ).fetchone()
        return row[0] if row else None

    # -- load / replace --

    def test_unknown_glossary_loads_empty(self):
        self.assertEqual(self.store.load_entries(BG3), [])

    def test_entries_load_least_recently_used_first(self):
        shadowheart = entry("Shadowheart", "Шэдоухарт (ж)", 300)
        astarion = entry("Astarion", "Астарион (м)", 100)
        baldur = entry("Baldur's Gate", "Врата Балдура", 200)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [shadowheart, astarion, baldur])

        self.assertEqual(self.store.load_entries(BG3), [astarion, baldur, shadowheart])

    def test_replace_drops_entries_missing_from_the_new_list(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        karlach = entry("Karlach", "Карлах (ж)", 200)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion, karlach])

        self.store.replace_entries(BG3, "Baldur's Gate 3", [karlach])

        self.assertEqual(self.store.load_entries(BG3), [karlach])

    def test_replace_keeps_one_glossary_row_and_refreshes_the_name(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])
        first_id = self.glossary_id(BG3)

        self.store.replace_entries(BG3, "Baldur's Gate III", [astarion])

        self.assertEqual(self.count("glossaries"), 1)
        self.assertEqual(self.glossary_id(BG3), first_id)
        name = self.store._conn.execute("SELECT game_name FROM glossaries").fetchone()[0]
        self.assertEqual(name, "Baldur's Gate III")

    def test_glossaries_are_separate_per_method_game_and_pair(self):
        other_keys = [
            GlossaryKey("other_ai", BG3.game_id, BG3.source_lang, BG3.target_lang),
            GlossaryKey(BG3.method, "292030", BG3.source_lang, BG3.target_lang),
            GlossaryKey(BG3.method, BG3.game_id, "ja", BG3.target_lang),
        ]
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])

        for key in other_keys:
            with self.subTest(key=key):
                self.assertEqual(self.store.load_entries(key), [])

    def test_empty_replace_does_not_create_a_glossary(self):
        self.store.replace_entries(BG3, "Baldur's Gate 3", [])

        self.assertEqual(self.count("glossaries"), 0)

    def test_empty_replace_deletes_an_existing_glossary(self):
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Astarion", "Астарион", 1)])

        self.store.replace_entries(BG3, "Baldur's Gate 3", [])

        self.assertEqual(self.count("glossaries"), 0)
        self.assertEqual(self.count("glossary_entries"), 0)

    def test_glossary_survives_reopening(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])
        self.store.close()

        self.store = GlossaryStore(self.db_path)

        self.assertEqual(self.store.load_entries(BG3), [astarion])

    def test_speech_is_saved_and_loaded(self):
        astarion = GlossaryEntry("astarion", "Astarion", "Астарион (м)", 1, "язвительно, манерно")
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])

        self.assertEqual(self.store.load_entries(BG3), [astarion])

    def test_old_database_gets_the_speech_column(self):
        # База прежней версии: таблица записей без колонки speech.
        self.store.close()
        os.remove(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE glossaries (id INTEGER PRIMARY KEY AUTOINCREMENT, method TEXT NOT NULL, "
            "game_id TEXT NOT NULL, game_name TEXT, source_lang TEXT NOT NULL, target_lang TEXT NOT NULL, "
            "UNIQUE (method, game_id, source_lang, target_lang))"
        )
        conn.execute(
            "CREATE TABLE glossary_entries (glossary_id INTEGER NOT NULL, match_key TEXT NOT NULL, "
            "term TEXT NOT NULL, translation TEXT NOT NULL, last_used_at INTEGER, "
            "PRIMARY KEY (glossary_id, match_key))"
        )
        conn.execute("INSERT INTO glossaries VALUES (1, 'gigachat_plus', '1086940', 'BG3', 'en', 'ru')")
        conn.execute("INSERT INTO glossary_entries VALUES (1, 'gale', 'Gale', 'Гейл (м)', 5)")
        conn.commit()
        conn.close()

        self.store = GlossaryStore(self.db_path)

        # Старая запись читается, манера пустая; новую можно сохранить.
        self.assertEqual(self.store.load_entries(BG3), [GlossaryEntry("gale", "Gale", "Гейл (м)", 5)])
        karlach = GlossaryEntry("karlach", "Karlach", "Карлах (ж)", 6, "бурно, по-свойски")
        self.store.replace_entries(BG3, "BG3", [karlach])
        self.assertEqual(self.store.load_entries(BG3), [karlach])

    # -- delete --

    def test_delete_entry_removes_only_that_entry(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        karlach = entry("Karlach", "Карлах (ж)", 200)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion, karlach])

        self.assertTrue(self.store.delete_entry(BG3, astarion.match_key))

        self.assertEqual(self.store.load_entries(BG3), [karlach])

    def test_delete_entry_does_not_touch_other_glossaries(self):
        other_game = GlossaryKey(BG3.method, "292030", BG3.source_lang, BG3.target_lang)
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])
        self.store.replace_entries(other_game, "The Witcher 3", [astarion])

        self.store.delete_entry(BG3, astarion.match_key)

        self.assertEqual(self.store.load_entries(other_game), [astarion])

    def test_deleting_the_last_entry_removes_the_empty_glossary(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])

        self.assertTrue(self.store.delete_entry(BG3, astarion.match_key))

        self.assertEqual(self.count("glossaries"), 0)
        # Сбрасывать больше нечего: «сбросить всё» не должно считаться удалением.
        self.assertFalse(self.store.delete_glossary(BG3))

    def test_delete_missing_entry_reports_false(self):
        self.assertFalse(self.store.delete_entry(BG3, "nobody"))

    def test_delete_glossary_removes_its_entries(self):
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Astarion", "Астарион", 1)])

        self.assertTrue(self.store.delete_glossary(BG3))

        self.assertEqual(self.store.load_entries(BG3), [])
        self.assertEqual(self.count("glossaries"), 0)
        self.assertEqual(self.count("glossary_entries"), 0)

    def test_delete_missing_glossary_reports_false(self):
        self.assertFalse(self.store.delete_glossary(BG3))

    def test_delete_glossary_also_cleans_up_older_orphans(self):
        # Запись, чей словарь давно удалён, — как будто её оставила прошлая
        # ошибка. Подчистка в delete_glossary должна убрать и её.
        self.store._conn.execute(
            "INSERT INTO glossary_entries (glossary_id, match_key, term, translation, last_used_at) "
            "VALUES (999, 'ghost', 'Ghost', 'Призрак', 1)"
        )
        self.store._conn.commit()
        other_game = GlossaryKey(BG3.method, "292030", BG3.source_lang, BG3.target_lang)
        kept = entry("Geralt", "Геральт (м)", 1)
        self.store.replace_entries(other_game, "The Witcher 3", [kept])
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Astarion", "Астарион", 1)])

        self.store.delete_glossary(BG3)

        self.assertEqual(self.count("glossary_entries"), 1)
        self.assertEqual(self.store.load_entries(other_game), [kept])

    def test_new_glossary_never_reuses_a_deleted_id(self):
        # Без AUTOINCREMENT SQLite выдал бы удалённый наибольший номер снова,
        # и потерявшиеся записи старого словаря прилипли бы к новому.
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Astarion", "Астарион", 1)])
        old_id = self.glossary_id(BG3)
        self.store.delete_glossary(BG3)

        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Karlach", "Карлах", 1)])

        self.assertNotEqual(self.glossary_id(BG3), old_id)


    # -- чёрный список --

    def game_name(self, key):
        row = self.store._conn.execute(
            "SELECT game_name FROM glossaries WHERE id = ?", [self.glossary_id(key)]
        ).fetchone()
        return row[0]

    def test_reject_moves_the_entry_to_the_black_list(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        back = entry("lb Back", "Назад", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion, back])

        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 200)])

        self.assertEqual(self.store.load_entries(BG3), [astarion])
        self.assertEqual(self.store.load_rejected(BG3), [rejected("lb Back", "Назад", 200)])

    def test_rejecting_the_last_entry_keeps_the_glossary_and_its_black_list(self):
        # Пустым считается словарь без записей и без запретов: иначе запрет
        # последней записи удалил бы строку словаря, а с ней и сам запрет.
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("lb Back", "Назад", 100)])

        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 200)])

        self.assertEqual(self.store.load_entries(BG3), [])
        self.assertEqual(len(self.store.load_rejected(BG3)), 1)

    def test_empty_replace_keeps_the_black_list(self):
        # Провайдер сохраняет опустевший словарь пустым списком.
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 200)])

        self.store.replace_entries(BG3, "Baldur's Gate 3", [])

        self.assertEqual(len(self.store.load_rejected(BG3)), 1)

    def test_deleting_the_last_entry_keeps_the_black_list(self):
        astarion = entry("Astarion", "Астарион (м)", 100)
        self.store.replace_entries(BG3, "Baldur's Gate 3", [astarion])
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 200)])

        self.store.delete_entry(BG3, astarion.match_key)

        self.assertEqual(len(self.store.load_rejected(BG3)), 1)

    def test_black_list_loads_most_recent_first(self):
        self.store.reject_entries(BG3, "Baldur's Gate 3", [
            rejected("lb Back", "Назад", 100),
            rejected("Auto Mode", "Авторежим", 300, REJECTED_BY_CLEANUP),
            rejected("Skip", "Пропуск", 200),
        ])

        self.assertEqual(
            [item.term for item in self.store.load_rejected(BG3)], ["Auto Mode", "Skip", "lb Back"]
        )
        self.assertEqual(self.store.load_rejected(BG3)[0].reason, REJECTED_BY_CLEANUP)

    def test_rejecting_again_updates_the_row(self):
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 100)])

        self.store.reject_entries(
            BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 300, REJECTED_BY_CLEANUP)]
        )

        self.assertEqual(
            self.store.load_rejected(BG3), [rejected("lb Back", "Назад", 300, REJECTED_BY_CLEANUP)]
        )

    def test_reject_without_a_name_keeps_the_stored_name(self):
        # Окно словаря запрещает запись в словаре не запущенной игры и
        # её названия не знает.
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("lb Back", "Назад", 100)])

        self.store.reject_entries(BG3, None, [rejected("lb Back", "Назад", 200)])

        self.assertEqual(self.game_name(BG3), "Baldur's Gate 3")

    def test_delete_rejected_allows_the_term_and_removes_an_empty_glossary(self):
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 100)])

        self.assertTrue(self.store.delete_rejected(BG3, "lb back"))

        self.assertEqual(self.store.load_rejected(BG3), [])
        self.assertEqual(self.count("glossaries"), 0)
        self.assertFalse(self.store.delete_rejected(BG3, "lb back"))

    def test_delete_glossary_removes_its_black_list(self):
        self.store.replace_entries(BG3, "Baldur's Gate 3", [entry("Astarion", "Астарион", 1)])
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 100)])

        self.assertTrue(self.store.delete_glossary(BG3))

        self.assertEqual(self.count("glossary_rejected"), 0)

    def test_black_list_is_separate_per_glossary(self):
        other_game = GlossaryKey("gigachat_plus", "632470", "en", "ru")
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 100)])

        self.assertEqual(self.store.load_rejected(other_game), [])

    def test_black_list_survives_reopening(self):
        self.store.reject_entries(BG3, "Baldur's Gate 3", [rejected("lb Back", "Назад", 100)])
        self.store.close()

        self.store = GlossaryStore(self.db_path)

        self.assertEqual(self.store.load_rejected(BG3), [rejected("lb Back", "Назад", 100)])


if __name__ == "__main__":
    unittest.main()
