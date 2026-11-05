"""
Контракт модели у провайдеров (ModelContract в py_modules/providers/base.py).

Новый провайдер обязан сказать, локальный ли он и скачана ли его модель —
по этому readiness.py говорит «Модель X не скачана». Тест ловит забытый
контракт раньше, чем это увидит человек на Деке.
"""

import inspect
import unittest
from typing import List, Set

import providers
from providers.base import OCRProvider, TranslationProvider

# Сейчас провайдеров 11 (5 OCR + 6 перевода, Opus-MT — наследник CT2).
# Нижняя граница страхует сам поиск: если он сломается и ничего не найдёт,
# тест контракта прошёл бы «успешно» на пустом списке.
MIN_EXPECTED_PROVIDERS = 11


def find_provider_classes() -> List[type]:
    """
    Все классы провайдеров из модулей пакета providers.

    Ищем по модулям, а не по списку имён: так новый провайдер попадает
    в проверку сам, без правки теста.
    """
    base_classes = (OCRProvider, TranslationProvider)
    found: Set[type] = set()
    for _, module in inspect.getmembers(providers, inspect.ismodule):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls in base_classes:
                continue
            if issubclass(cls, base_classes):
                found.add(cls)
    return sorted(found, key=lambda cls: cls.__name__)


class ProviderContractTest(unittest.TestCase):

    def test_search_finds_all_providers(self):
        self.assertGreaterEqual(len(find_provider_classes()), MIN_EXPECTED_PROVIDERS)

    def test_every_provider_implements_contract(self):
        # Абстрактный класс = какой-то обязательный метод не реализован,
        # и создать такой провайдер нельзя.
        for cls in find_provider_classes():
            with self.subTest(provider=cls.__name__):
                self.assertFalse(
                    inspect.isabstract(cls),
                    f"не реализованы: {sorted(cls.__abstractmethods__)}",
                )

    def test_provider_without_contract_cannot_be_created(self):
        # Всё обязательное, кроме контракта модели.
        class ForgotModelContract(OCRProvider):
            name = "forgot"
            provider_type = None

            async def recognize(self, image_data, language="auto"):
                return []

            def is_available(self, language="auto"):
                return True

            def get_supported_languages(self):
                return []

        with self.assertRaises(TypeError):
            ForgotModelContract()


if __name__ == "__main__":
    unittest.main()
