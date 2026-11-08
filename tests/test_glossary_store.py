"""
GlossaryStore: словари, сохранённые по методу, игре и языковой паре.

Каждый тест работает с новым файлом базы во временной папке: проверяется
настоящее поведение SQLite (транзакции, AUTOINCREMENT, переоткрытие базы),
а не база в памяти.
"""

import os
import tempfile
import unittest

from providers.glossary_store import GlossaryEntry, GlossaryKey, GlossaryStore

BG3 = GlossaryKey("gigachat_plus", "1086940", "en", "ru")


def entry(term, translation, last_used_at):
    return GlossaryEntry(term.casefold(), term, translation, last_used_at)


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
            "INSERT INTO glossary_entries VALUES (999, 'ghost', 'Ghost', 'Призрак', 1)"
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


if __name__ == "__main__":
    unittest.main()
