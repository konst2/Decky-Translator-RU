"""
Признак «модель скачана» у локальных OCR-провайдеров.

Провайдер обязан отвечать так же, как его загрузчик (is_installed):
по загрузчику вкладка пишет «Установлено / Не установлено», а по провайдеру
предпроверка решает, пускать ли в работу. Раньше RapidOCR смотрел только
2 файла из 18 — при неполной установке вкладка и предпроверка расходились.

Файлы моделей здесь пустые: проверяется только их наличие.
"""

import os
import tempfile
import unittest

from providers import rapidocr_downloader, screenai_downloader
from providers.chromescreenai_provider import ChromeScreenAIProvider
from providers.rapidocr_downloader import RapidOCRDownloader
from providers.rapidocr_provider import RapidOCRProvider
from providers.screenai_downloader import ScreenAIDownloader


def create_empty_files(base_dir: str, relative_paths) -> None:
    for relative_path in relative_paths:
        path = os.path.join(base_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()


class RapidOcrModelDownloadedTest(unittest.TestCase):

    def setUp(self):
        self.models_dir = tempfile.TemporaryDirectory()
        self.downloader = RapidOCRDownloader(self.models_dir.name)
        self.provider = RapidOCRProvider(
            models_dir=self.downloader.get_target_dir(),
            downloader=self.downloader,
        )

    def tearDown(self):
        self.models_dir.cleanup()

    def test_nothing_downloaded(self):
        self.assertFalse(self.provider.is_model_downloaded())

    def test_full_install(self):
        create_empty_files(self.downloader.get_target_dir(), rapidocr_downloader.REQUIRED_FILES)

        self.assertTrue(self.provider.is_model_downloaded())
        self.assertEqual(self.provider.is_model_downloaded(), self.downloader.is_installed())

    def test_partial_install_is_not_downloaded(self):
        # Есть модели поиска и ориентации текста (раньше этого хватало),
        # но нет словаря одного из языков — например, после обновления,
        # добавившего файлы в манифест.
        all_but_one = [name for name in rapidocr_downloader.REQUIRED_FILES if name != "thai_dict.txt"]
        create_empty_files(self.downloader.get_target_dir(), all_but_one)

        self.assertFalse(self.provider.is_model_downloaded())
        self.assertEqual(self.provider.is_model_downloaded(), self.downloader.is_installed())

    def test_without_downloader_is_not_downloaded(self):
        # Каталог моделей не настроен — ставить модель некуда.
        provider = RapidOCRProvider(models_dir=self.downloader.get_target_dir())
        create_empty_files(self.downloader.get_target_dir(), rapidocr_downloader.REQUIRED_FILES)

        self.assertFalse(provider.is_model_downloaded())


class ChromeScreenAiModelDownloadedTest(unittest.TestCase):

    def setUp(self):
        self.models_dir = tempfile.TemporaryDirectory()
        self.downloader = ScreenAIDownloader(self.models_dir.name)
        self.provider = ChromeScreenAIProvider(
            model_dir=self.downloader.get_resources_dir(),
            downloader=self.downloader,
        )

    def tearDown(self):
        self.models_dir.cleanup()

    def test_nothing_downloaded(self):
        self.assertFalse(self.provider.is_model_downloaded())

    def test_full_install(self):
        create_empty_files(self.downloader.get_target_dir(), screenai_downloader.REQUIRED_FILES)

        self.assertTrue(self.provider.is_model_downloaded())
        self.assertEqual(self.provider.is_model_downloaded(), self.downloader.is_installed())

    def test_without_downloader_is_not_downloaded(self):
        provider = ChromeScreenAIProvider(model_dir=self.downloader.get_resources_dir())
        create_empty_files(self.downloader.get_target_dir(), screenai_downloader.REQUIRED_FILES)

        self.assertFalse(provider.is_model_downloaded())


if __name__ == "__main__":
    unittest.main()
