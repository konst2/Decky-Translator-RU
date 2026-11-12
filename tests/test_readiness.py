"""
Проверки готовности (py_modules/providers/readiness.py).

Провайдеры поддельные (fakes.py): проверяется только логика «можно ли
переводить и почему нет», без файлов моделей и сети.
"""

import unittest
from unittest import mock

from providers import readiness

from fakes import cloud, local


class OcrReadinessTest(unittest.TestCase):

    def test_default_first_run_without_model(self):
        # Дефолт новой установки: Chrome Screen AI + MyMemory, ничего не скачано.
        chrome = local("Chrome Screen AI", 120, downloaded=False)

        result = readiness.check_all("chromescreenai", chrome, "mymemory", cloud(), "en", "ru")

        self.assertFalse(result["ready"])
        self.assertEqual(result["stage"], "ocr")
        self.assertEqual(result["reason"], readiness.REASON_MODEL_MISSING)
        self.assertEqual(result["scroll_target"], "chromescreenai-action")
        self.assertIn("Модель Chrome Screen AI не скачана (~120 МБ)", result["message"])
        # Тело тоста — без повтора заголовка «Модель не скачана»: Steam
        # показывает его одной строкой, длинное обрезалось.
        self.assertEqual(result["detail"], "Chrome Screen AI (~120 МБ)")

    def test_ready_when_model_downloaded(self):
        chrome = local("Chrome Screen AI", 120)

        result = readiness.check_all("chromescreenai", chrome, "mymemory", cloud(), "en", "ru")

        self.assertTrue(result["ready"])

    def test_new_local_method_unknown_to_readiness(self):
        # Ради этого и заведён контракт модели: метод, о котором readiness.py
        # ничего не знает, всё равно получает понятное «модель не скачана».
        new_method = local("SuperOCR", 300, downloaded=False, available=False)

        result = readiness.check_ocr("superocr", new_method, "ja")

        self.assertEqual(result["reason"], readiness.REASON_MODEL_MISSING)
        self.assertIn("Модель SuperOCR не скачана (~300 МБ)", result["message"])
        self.assertEqual(result["scroll_target"], "superocr-action")

    def test_unsupported_language_is_reported_not_silent(self):
        # Раньше такой запуск заканчивался молчаливым «Текст не найден».
        chrome = local("Chrome Screen AI", 120, available=False)

        result = readiness.check_ocr("chromescreenai", chrome, "xx")

        self.assertEqual(result["reason"], readiness.REASON_NOT_AVAILABLE)
        self.assertEqual(result["message"], readiness.OCR_NOT_AVAILABLE_MESSAGE)
        self.assertIsNone(result["scroll_target"])

    def test_ocr_asks_provider_about_input_language(self):
        # Тот же вопрос, что задаёт настоящий запуск (ProviderManager.recognize_text).
        provider = cloud()

        readiness.check_ocr("ocrspace", provider, "ja")

        self.assertEqual(provider.availability_questions, [("ja",)])

    def test_missing_provider_is_not_ready(self):
        result = readiness.check_ocr("ocrspace", None, "en")

        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], readiness.REASON_NOT_AVAILABLE)

    def test_ocr_is_checked_before_translation(self):
        # Порядок как в работе: сначала распознавание, потом перевод.
        chrome = local("Chrome Screen AI", 120, downloaded=False)
        nllb = local("NLLB", 1410, downloaded=False)

        result = readiness.check_all("chromescreenai", chrome, "ct2", nllb, "en", "ru")

        self.assertEqual(result["stage"], "ocr")

    def test_rapidocr_engine_not_shipped(self):
        # Движок не входит в базовый архив: причина отдельная и с советом
        # выбрать другой метод, а не «модель не скачана» и не «метод не настроен».
        rapidocr = local("RapidOCR", 75, downloaded=False)

        with mock.patch.object(readiness, "RAPIDOCR_AVAILABLE", False):
            result = readiness.check_ocr("rapidocr", rapidocr, "en")

        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], readiness.REASON_ENGINE_UNAVAILABLE)
        self.assertIn("RapidOCR недоступен", result["message"])
        self.assertIsNone(result["scroll_target"])


class TranslationReadinessTest(unittest.TestCase):

    def test_nllb_missing_size_in_gigabytes(self):
        nllb = local("NLLB", 1410, downloaded=False)

        result = readiness.check_translation("ocrspace", "ct2", nllb, "en", "ru")

        self.assertEqual(result["scroll_target"], "ct2-action")
        self.assertIn("~1,4 ГБ", result["message"])

    def test_nllb_auto_language_is_reported_before_model(self):
        # Сначала языки, потом модель: не предлагать качать 1,4 ГБ, если
        # с такими настройками метод всё равно не заработает.
        nllb = local("NLLB", 1410, downloaded=False)

        result = readiness.check_translation("ocrspace", "ct2", nllb, "auto", "ru")

        self.assertEqual(result["reason"], readiness.REASON_AUTO_UNSUPPORTED)
        self.assertIsNone(result["scroll_target"])

    def test_opusmt_wrong_pair_is_reported_before_model(self):
        opusmt = local("Opus-MT en→ru", 82, downloaded=False)

        result = readiness.check_translation("ocrspace", "opusmt", opusmt, "ja", "ru")

        self.assertEqual(result["reason"], readiness.REASON_PAIR_UNSUPPORTED)
        self.assertEqual(result["detail"], "Только английский → русский")

    def test_opusmt_missing_model(self):
        opusmt = local("Opus-MT en→ru", 82, downloaded=False)

        result = readiness.check_translation("ocrspace", "opusmt", opusmt, "en", "ru")

        self.assertEqual(result["scroll_target"], "opusmt-action")
        self.assertIn("~82 МБ", result["message"])

    def test_translation_asks_provider_about_language_pair(self):
        provider = cloud()
        provider.available = False

        result = readiness.check_translation("ocrspace", "mymemory", provider, "ja", "ru")

        self.assertEqual(result["message"], readiness.TRANSLATION_NOT_AVAILABLE_MESSAGE)
        self.assertEqual(provider.availability_questions, [("ja", "ru")])

    def test_gemini_vision_skips_translation_check(self):
        # Gemini Vision распознаёт и переводит сам — метод перевода не используется.
        nllb = local("NLLB", 1410, downloaded=False)

        result = readiness.check_all("gemini_vision", cloud(), "ct2", nllb, "auto", "ru")

        self.assertTrue(result["ready"])


class ToastDetailTest(unittest.TestCase):

    def test_every_not_ready_result_has_short_detail(self):
        # У каждого «не готово» есть короткое тело тоста. Граница в 40 знаков —
        # примерно столько тост Steam показывает без обрезки (замечено на Деке).
        results = [
            readiness.check_ocr("superocr", local("SuperOCR", 300, downloaded=False), "en"),
            readiness.check_ocr("ocrspace", None, "en"),
            readiness.check_translation("ocrspace", "mymemory", None, "en", "ru"),
            readiness.check_translation("ocrspace", "ct2", local("NLLB", 1410), "auto", "ru"),
            readiness.check_translation("ocrspace", "opusmt", local("Opus-MT en→ru", 82), "ja", "ru"),
            readiness.not_started(),
        ]
        for result in results:
            with self.subTest(reason=result["reason"]):
                self.assertTrue(result["detail"])
                self.assertLessEqual(len(result["detail"]), 40)


class HelpersTest(unittest.TestCase):

    def test_format_approx_size(self):
        self.assertEqual(readiness.format_approx_size(75), "~75 МБ")
        self.assertEqual(readiness.format_approx_size(1410), "~1,4 ГБ")

    def test_not_started(self):
        self.assertEqual(readiness.not_started()["reason"], readiness.REASON_NOT_STARTED)


if __name__ == "__main__":
    unittest.main()
