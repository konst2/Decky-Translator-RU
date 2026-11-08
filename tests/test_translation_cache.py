"""
TranslationCache.delete_from_tier: очистка кэша после правки словаря GigaChat+.

Строки, переведённые с ошибочным именем, лежат в кэше с уровнем GigaChat+
и показывались бы дальше. Проверяем, что уходят именно они, а переводы
других методов остаются.
"""

import os
import tempfile
import unittest

from providers.translation_cache import CONTEXT_TIER, PROVIDER_TIERS, TranslationCache, reads_cache


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


class ReadsCacheTest(unittest.TestCase):
    """
    Методы с контекстом (GigaChat+) не берут переводы из кэша: иначе модели
    уходил обрезанный экран (реплика из кэша, а на перевод — одна строка
    интерфейса, из которой модель выписывала кнопки в словарь) и старые
    переводы, сделанные до исправлений словаря.
    """

    def test_context_method_does_not_read_the_cache(self):
        self.assertEqual(PROVIDER_TIERS["gigachat_plus"], CONTEXT_TIER)
        self.assertFalse(reads_cache(PROVIDER_TIERS["gigachat_plus"]))

    def test_other_methods_read_the_cache(self):
        for provider, tier in PROVIDER_TIERS.items():
            if provider == "gigachat_plus":
                continue
            with self.subTest(provider=provider):
                self.assertTrue(reads_cache(tier))


if __name__ == "__main__":
    unittest.main()
