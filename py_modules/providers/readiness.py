"""
Готовность выбранных методов распознавания и перевода.

Один источник правды на вопрос «можно ли сейчас переводить и если нет,
то почему». Им пользуются:
- RPC `check_readiness` (`main.py`): фронтенд зовёт его перед скриншотом,
  чтобы не ставить игру на паузу и не снимать экран впустую;
- страховочные проверки в `recognize_text` / `translate_text` (`main.py`):
  настройку могли поменять между предпроверкой и запуском.

О моделях здесь ничего не записано: название, размер и «скачана ли» каждый
провайдер сообщает сам (`ModelContract` в `base.py`). Поэтому новый
локальный метод получает понятное «модель не скачана» без правок этого файла.

Проверки по шагам (одинаково для распознавания и перевода):
1. Известные особые случаи по id метода — про языки, а не про модель
   (NLLB без «Автоопределения», Opus-MT только en→ru). Идут раньше модели:
   человек с неподходящей парой не должен качать модель впустую.
2. Локальный метод без скачанной модели — «Модель X не скачана (~N МБ)».
3. Повтор той же проверки `is_available`, что делает сам запуск
   (`ProviderManager.recognize_text` / `translate_text`). Она не может
   запретить то, что сейчас работает: если она говорит «нет», запуск упал бы
   позже — у распознавания вообще молчаливым «Текст не найден».

Проверки ключей и выбранных языков сюда не входят — они остаются во
фронтенде (`canStartTranslation` в `src/Translator.tsx`) и идут раньше.
"""

from typing import Optional

from .base import ModelContract, OCRProvider, TranslationProvider
from .capabilities import RAPIDOCR_AVAILABLE, RAPIDOCR_UNAVAILABLE_MESSAGE

# Причины «не готово». Фронтенд различает их, чтобы решить, куда вести
# пользователя: к кнопке загрузки или к выбору языков.
REASON_MODEL_MISSING = "model_missing"
REASON_PAIR_UNSUPPORTED = "pair_unsupported"
REASON_AUTO_UNSUPPORTED = "auto_unsupported"
REASON_NOT_AVAILABLE = "not_available"
REASON_ENGINE_UNAVAILABLE = "engine_unavailable"
REASON_NOT_STARTED = "not_started"

READY = {"ready": True}

# Тексты шага 3. Перевод — дословно тот, что уже показывает
# ProviderManager.translate_text; распознавание — по его образцу.
OCR_NOT_AVAILABLE_MESSAGE = (
    "Выбранный метод распознавания не настроен или не поддерживает исходный язык. "
    "Проверьте вкладку «Перевод»"
)
TRANSLATION_NOT_AVAILABLE_MESSAGE = (
    "Выбранный метод перевода не настроен или не поддерживает эту пару языков. "
    "Проверьте вкладку «Перевод»"
)
NOT_AVAILABLE_DETAIL = "Проверьте вкладку «Перевод»"


def format_approx_size(size_mb: int) -> str:
    """«~120 МБ», «~1,4 ГБ» — как `formatApproxSize` в TabTranslation.tsx."""
    if size_mb >= 1000:
        # В русском десятичная запятая: «1,4 ГБ», а не «1.4 ГБ».
        gigabytes = f"{size_mb / 1024:.1f}".replace(".", ",")
        return f"~{gigabytes} ГБ"
    return f"~{size_mb} МБ"


def _not_ready(stage: str, reason: str, message: str, detail: str,
               scroll_target: Optional[str] = None) -> dict:
    """
    stage — какой этап не готов: "ocr" или "translation".
    message — полный текст: его показывает оверлей, когда срабатывает
    страховка в recognize_text / translate_text.
    detail — короткое тело тоста. Тост Steam показывает тело одной строкой,
    а заголовок (по reason, `TOAST_TITLES` в Readiness.ts) уже говорит, что
    случилось, — поэтому здесь только то, чего в заголовке нет, без повтора.
    scroll_target — id кнопки загрузки на вкладке перевода, к которой
    фронтенд прокручивает (см. `scrollTarget` в TabTranslation.tsx).
    None — кнопки нет, причина не в модели.
    """
    return {
        "ready": False,
        "stage": stage,
        "reason": reason,
        "message": message,
        "detail": detail,
        "scroll_target": scroll_target,
    }


def _missing_model(stage: str, method: str, provider: ModelContract) -> Optional[dict]:
    """Шаг 2: локальный метод без скачанной модели. None — с моделью всё в порядке."""
    model = provider.local_model()
    if model is None or provider.is_model_downloaded():
        return None
    size = format_approx_size(model.approx_size_mb)
    return _not_ready(
        stage=stage,
        reason=REASON_MODEL_MISSING,
        message=f"Модель {model.label} не скачана ({size}).\n"
                "Скачайте её в настройках плагина",
        detail=f"{model.label} ({size})",
        # Соглашение: кнопка загрузки метода на вкладке перевода — «<id>-action»
        # (chromescreenai-action, ct2-action …). Если у нового метода кнопки
        # с таким id нет, фронтенд просто откроет вкладку без прокрутки.
        scroll_target=f"{method}-action",
    )


def not_started() -> dict:
    """Бэкенд ещё не создал ProviderManager — проверять пока нечего."""
    return _not_ready(
        stage="ocr",
        reason=REASON_NOT_STARTED,
        message="Плагин ещё запускается. Попробуйте через несколько секунд",
        detail="Попробуйте через несколько секунд",
    )


def check_ocr(method: str, provider: Optional[OCRProvider], input_lang: str) -> dict:
    """
    Готов ли метод распознавания. method — id из настроек (`ocr_provider`),
    provider — его объект (`ProviderManager.get_ocr_provider()`).
    """
    # Движок не входит в сборку: это не «метод не настроен» и не «модель не
    # скачана», поэтому причина отдельная — пользователю нужно выбрать метод.
    if method == "rapidocr" and not RAPIDOCR_AVAILABLE:
        return _not_ready("ocr", REASON_ENGINE_UNAVAILABLE,
                          RAPIDOCR_UNAVAILABLE_MESSAGE,
                          "RapidOCR не входит в эту сборку")

    if provider is None:
        return _not_ready("ocr", REASON_NOT_AVAILABLE, OCR_NOT_AVAILABLE_MESSAGE, NOT_AVAILABLE_DETAIL)

    missing = _missing_model("ocr", method, provider)
    if missing:
        return missing

    if not provider.is_available(input_lang):
        return _not_ready("ocr", REASON_NOT_AVAILABLE, OCR_NOT_AVAILABLE_MESSAGE, NOT_AVAILABLE_DETAIL)

    return READY


def check_translation(ocr_method: str, method: str,
                      provider: Optional[TranslationProvider],
                      input_lang: str, target_lang: str) -> dict:
    """
    Готов ли метод перевода для этой пары языков. method — id из настроек
    (`translation_provider`), provider — `ProviderManager.get_translation_provider()`.
    """
    # Gemini Vision распознаёт и переводит одним запросом — отдельный метод
    # перевода не используется (в translate_text этот путь пропускается).
    if ocr_method == "gemini_vision":
        return READY

    if provider is None:
        return _not_ready("translation", REASON_NOT_AVAILABLE, TRANSLATION_NOT_AVAILABLE_MESSAGE,
                          NOT_AVAILABLE_DETAIL)

    # Шаг 1: особые случаи про языки — тексты точнее общего шага 3.
    if method == "opusmt" and (input_lang, target_lang) != ("en", "ru"):
        return _not_ready(
            stage="translation",
            reason=REASON_PAIR_UNSUPPORTED,
            message="Метод «Локально (лёгкая, en→ru)» переводит только "
                    "английский → русский, а выбранная пара — другая. "
                    "Выберите другой метод перевода или пару языков",
            detail="Только английский → русский",
        )
    if method == "ct2" and input_lang == "auto":
        return _not_ready(
            stage="translation",
            reason=REASON_AUTO_UNSUPPORTED,
            message="Для офлайн-перевода нужен конкретный исходный язык. "
                    "Выберите его в настройках плагина",
            detail="Выберите его в настройках плагина",
        )

    missing = _missing_model("translation", method, provider)
    if missing:
        return missing

    if not provider.is_available(input_lang, target_lang):
        return _not_ready("translation", REASON_NOT_AVAILABLE, TRANSLATION_NOT_AVAILABLE_MESSAGE,
                          NOT_AVAILABLE_DETAIL)

    return READY


def check_all(ocr_method: str, ocr_provider: Optional[OCRProvider],
              translation_method: str, translation_provider: Optional[TranslationProvider],
              input_lang: str, target_lang: str) -> dict:
    """Распознавание проверяется первым — в том же порядке, в каком идёт работа."""
    ocr_result = check_ocr(ocr_method, ocr_provider, input_lang)
    if not ocr_result["ready"]:
        return ocr_result
    return check_translation(ocr_method, translation_method, translation_provider,
                             input_lang, target_lang)
