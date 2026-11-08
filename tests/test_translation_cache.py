"""
TranslationCache.delete_from_tier: очистка кэша после правки словаря GigaChat+.

Строки, переведённые с ошибочным именем, лежат в кэше с уровнем GigaChat+
и показывались бы дальше. Проверяем, что уходят именно они, а переводы
других методов остаются.
"""

import os
import tempfile
import unittest

from providers.translation_cache import PROVIDER_TIERS, TranslationCache


class DeleteFromTierTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = TranslationCache(os.path.join(self._tmp.name, "cache.db"))

    def tearDown(self):
        self.cache.close()
        self._tmp.cleanup()

    def put(self, text, translation, provider):
        self.cache.put_many([(text, translation)], "en", "ru", PROVIDER_TIERS[provider], provider)

    def cached(self, *texts):
        # Наименьший уровень: видны записи любого метода.
        return self.cache.get_many(list(texts), "en", "ru", min_tier=0)

    def test_only_glossary_tier_entries_are_deleted(self):
        self.put("Tav waits", "Тав ждёт", "gigachat_plus")
        self.put("Hello", "Привет", "gigachat")
        self.put("Goodbye", "До свидания", "mymemory")

        removed = self.cache.delete_from_tier(PROVIDER_TIERS["gigachat_plus"])

        self.assertEqual(removed, 1)
        self.assertEqual(
            self.cached("Tav waits", "Hello", "Goodbye"),
            {"Hello": "Привет", "Goodbye": "До свидания"},
        )

    def test_empty_cache_reports_zero(self):
        self.assertEqual(self.cache.delete_from_tier(PROVIDER_TIERS["gigachat_plus"]), 0)


if __name__ == "__main__":
    unittest.main()
