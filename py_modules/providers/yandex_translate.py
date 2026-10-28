# providers/yandex_translate.py
# Yandex Cloud Translate provider

import asyncio
import logging
from typing import List, Optional, Tuple

import requests

from .base import TranslationProvider, ProviderType, NetworkError, ApiKeyError, RateLimitError

logger = logging.getLogger(__name__)

# Yandex Cloud Translate accepts at most 10 000 characters across all texts
# in one request, so batches are built to stay under that limit.
_MAX_CHARS_PER_REQUEST = 10000
_MAX_TEXTS_PER_REQUEST = 100

# Yandex's error text is shown in the overlay as is; a long one would
# overflow it, so it is cut to this length.
_MAX_ERROR_MESSAGE_LENGTH = 200

_INVALID_RESPONSE_MESSAGE = "Яндекс Перевод вернул некорректный ответ"

# The plugin uses Google-style codes; Yandex has a single code for Chinese.
_LANGUAGE_ALIASES = {
    "zh-CN": "zh",
    "zh-TW": "zh",
    "zh-Hans": "zh",
    "zh-Hant": "zh",
}


def _normalize_language(code: str) -> str:
    if not code:
        return code
    return _LANGUAGE_ALIASES.get(code, code)


def _error_message(response) -> str:
    """Return the "message" field of a Yandex error response, or "" if there is none."""
    # Error bodies look like {"code": 3, "message": "...", "details": [...]},
    # but a proxy or balancer in between may answer with HTML or plain text.
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if not isinstance(message, str):
        return ""
    message = message.strip()
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[:_MAX_ERROR_MESSAGE_LENGTH].rstrip() + "…"
    return message


def _error_summary(response) -> str:
    """Return Yandex error code and requestId of an error response, without its text."""
    # requestId sits in "details": [{"@type": "...RequestInfo", "requestId": "..."}].
    try:
        payload = response.json()
    except ValueError:
        return "body is not JSON"
    if not isinstance(payload, dict):
        return "body is not a JSON object"

    request_id = None
    details = payload.get("details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and "requestId" in item:
                request_id = item["requestId"]
                break
    return f"yandex code={payload.get('code')}, requestId={request_id}"


def _split_oversized(text: str) -> List[str]:
    """Split a single text that exceeds the per-request limit at a word boundary."""
    if len(text) <= _MAX_CHARS_PER_REQUEST:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > _MAX_CHARS_PER_REQUEST:
        cut = remaining.rfind(" ", 0, _MAX_CHARS_PER_REQUEST)
        if cut <= 0:
            cut = _MAX_CHARS_PER_REQUEST
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _make_batches(texts: List[str]):
    """Yield lists of indexes whose texts fit into one API request."""
    batch: List[int] = []
    chars = 0
    for index, text in enumerate(texts):
        size = len(text)
        if batch and (chars + size > _MAX_CHARS_PER_REQUEST or len(batch) >= _MAX_TEXTS_PER_REQUEST):
            yield batch
            batch, chars = [], 0
        batch.append(index)
        chars += size
    if batch:
        yield batch


class YandexTranslateProvider(TranslationProvider):
    """Translation provider using Yandex Cloud Translate API."""

    SUPPORTED_LANGUAGES = [
        'auto', 'en', 'ja', 'zh', 'ko', 'de', 'fr', 'es', 'it',
        'pt', 'ru', 'ar', 'nl', 'no', 'pl', 'tr', 'uk', 'hi', 'el', 'th', 'vi', 'fi', 'id', 'ro', 'bg', 'hr',
        'cs', 'hu', 'sv', 'da'
    ]

    def __init__(self, api_key: str = "", folder_id: str = ""):
        """Initialize the Yandex Translate provider."""
        self._api_key = api_key
        self._folder_id = folder_id
        self._endpoint = "https://translate.api.cloud.yandex.net/translate/v2/translate"
        self._languages_endpoint = "https://translate.api.cloud.yandex.net/translate/v2/languages"
        logger.debug("YandexTranslateProvider initialized")

    def set_credentials(self, api_key: str, folder_id: str) -> None:
        """Update the API key and folder ID."""
        self._api_key = api_key
        self._folder_id = folder_id

    @property
    def name(self) -> str:
        return "Yandex Translate"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.YANDEX

    def is_available(self, source_lang: str, target_lang: str) -> bool:
        """Check if translation is available for the language pair."""
        if not self._api_key or not self._folder_id:
            return False
        source = _normalize_language(source_lang)
        target = _normalize_language(target_lang)
        if source != 'auto' and source not in self.SUPPORTED_LANGUAGES:
            return False
        return target in self.SUPPORTED_LANGUAGES and target != 'auto'

    def get_supported_languages(self) -> List[str]:
        """Return list of supported language codes."""
        return self.SUPPORTED_LANGUAGES.copy()

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate a single text using Yandex Cloud Translate API."""
        if not text or not text.strip():
            return text
        results = await self.translate_batch([text], source_lang, target_lang)
        return results[0] if results else text

    async def translate_batch(self, texts: List[str], source_lang: str, target_lang: str) -> List[str]:
        """
        Translate multiple texts using Yandex Cloud Translate API.

        Args:
            texts: List of texts to translate
            source_lang: Source language code or "auto"
            target_lang: Target language code

        Returns:
            List of translated texts (original text for failed items)
        """
        if not self._api_key or not self._folder_id:
            logger.error("Yandex Translate credentials not configured")
            return texts

        if not texts:
            return texts

        source = _normalize_language(source_lang)
        target = _normalize_language(target_lang)

        # Split oversized texts, remembering where each piece belongs so the
        # original order can be restored afterwards. Blank texts are passed
        # through untouched instead of being sent to the API.
        originals: List[str] = []
        spans: List[Optional[Tuple[int, int]]] = []
        flat: List[str] = []
        for text in texts:
            value = text if isinstance(text, str) else ("" if text is None else str(text))
            originals.append(value)
            if not value.strip():
                spans.append(None)
                continue
            parts = _split_oversized(value)
            spans.append((len(flat), len(flat) + len(parts)))
            flat.extend(parts)

        translated = list(flat)
        for batch in _make_batches(flat):
            batch_texts = [flat[i] for i in batch]
            logger.debug(f"Translating {len(batch_texts)} texts: {source} -> {target}")
            # Any failure raises: returning the original text here would look
            # like a successful translation to the user (bug B3).
            results = await self._translate_request(batch_texts, source, target)
            for index, result in zip(batch, results):
                translated[index] = result

        final: List[str] = []
        for index, span in enumerate(spans):
            if span is None:
                final.append(originals[index])
            else:
                start, end = span
                final.append(" ".join(translated[start:end]))
        return final

    async def _translate_request(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[str]:
        """
        Send one request and return translations in the same order as texts.

        Every failure raises NetworkError / ApiKeyError / RateLimitError:
        only these three reach the user (main.py translate_text), any other
        exception ends up as a silently untranslated overlay.
        """
        request_data = {
            "folderId": self._folder_id,
            "texts": texts,
            "targetLanguageCode": target_lang,
        }
        if source_lang and source_lang != "auto":
            request_data["sourceLanguageCode"] = source_lang

        headers = {
            "Authorization": f"Api-Key {self._api_key}",
            "Content-Type": "application/json",
        }

        def do_request():
            return requests.post(
                self._endpoint,
                json=request_data,
                headers=headers,
                timeout=15.0,
            )

        try:
            response = await asyncio.to_thread(do_request)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Yandex Translate connection error: {e}")
            raise NetworkError("Нет подключения к интернету") from e
        except requests.exceptions.Timeout as e:
            logger.error(f"Yandex Translate timeout error: {e}")
            raise NetworkError("Превышено время ожидания ответа Yandex Translate") from e
        except Exception as e:
            logger.error(f"Yandex Translate request error: {e}")
            raise NetworkError(
                f"Ошибка запроса к Яндекс Переводу: {type(e).__name__}"
            ) from e

        if response.status_code != 200:
            self._handle_error(response)

        try:
            payload = response.json()
        except ValueError as e:
            logger.error("Yandex Translate returned invalid JSON")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE) from e

        return self._parse_translations(payload, len(texts))

    def _parse_translations(self, payload, expected_count: int) -> List[str]:
        """Extract translated texts, rejecting any response of unexpected shape."""
        # The shape is checked explicitly: an unexpected type here would raise
        # AttributeError/TypeError, which main.py does not show to the user.
        if not isinstance(payload, dict):
            logger.error(f"Yandex Translate returned {type(payload).__name__} instead of an object")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE)

        translations = payload.get("translations")
        if not isinstance(translations, list) or len(translations) != expected_count:
            count = len(translations) if isinstance(translations, list) else "no"
            logger.error(f"Yandex Translate returned {count} translations for {expected_count} texts")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE)

        results: List[str] = []
        for item in translations:
            if not isinstance(item, dict):
                logger.error("Yandex Translate returned a translation that is not an object")
                raise NetworkError(_INVALID_RESPONSE_MESSAGE)
            results.append(str(item.get("text", "")))

        logger.debug(f"Successfully translated {len(results)} texts")
        return results

    def _handle_error(self, response) -> None:
        """Log a non-200 response and raise an error with a message for the user."""
        body = (response.text or "")[:500]
        lowered = body.lower()
        logger.error(f"Yandex Translate API error: {response.status_code}")
        # Errors about the folder quote the real folder ID of the service
        # account, so their body is not logged. Yandex code and requestId are
        # enough for Yandex support to find the request.
        if "folder" in lowered:
            logger.error(f"Response hidden (mentions folder ID), {_error_summary(response)}")
        else:
            logger.error(f"Response: {body}")

        if response.status_code == 401:
            raise ApiKeyError("Неверный API-ключ Яндекс Облака")
        if response.status_code == 403:
            if "folder" in lowered:
                raise ApiKeyError(
                    "Нет доступа к указанному folder_id. Проверьте folder_id и права сервисного аккаунта"
                )
            raise ApiKeyError(
                "Доступ запрещён. Проверьте права сервисного аккаунта (роль ai.translate.user)"
            )
        if response.status_code == 400 and "folder" in lowered:
            raise ApiKeyError("Неверный folder_id")
        if response.status_code == 429:
            raise RateLimitError("Превышен лимит запросов Yandex Translate. Попробуйте позже")
        if response.status_code >= 500:
            raise NetworkError(
                f"Сервис Яндекс Перевода временно недоступен (код {response.status_code})"
            )
        if response.status_code == 400:
            # Usually an unsupported language pair or a malformed request.
            # Yandex's own text is more precise than any paraphrase of ours.
            reason = _error_message(response)
            if reason:
                raise NetworkError(f"Яндекс Перевод отклонил запрос: {reason}")
            raise NetworkError("Яндекс Перевод отклонил запрос (код 400)")
        raise NetworkError(f"Ошибка Яндекс Перевода (код {response.status_code})")

    async def test_network(self) -> tuple:
        if not self._api_key:
            return False, "Требуется API-ключ"
        if not self._folder_id:
            return False, "Требуется folder_id"

        headers = {
            "Authorization": f"Api-Key {self._api_key}",
            "Content-Type": "application/json",
        }

        def _probe():
            try:
                resp = requests.post(
                    self._languages_endpoint,
                    json={"folderId": self._folder_id},
                    headers=headers,
                    timeout=5,
                )
            except (requests.ConnectionError, requests.Timeout):
                return False, "Сеть недоступна"
            except Exception as e:
                return False, f"Ошибка проверки: {type(e).__name__}"

            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "Неверный API-ключ"
            if resp.status_code == 403:
                return False, "Нет доступа к folder_id"
            # A wrong folder_id comes back as 400, not 403 (checked live 28.10.2026).
            if resp.status_code == 400 and "folder" in (resp.text or "").lower():
                return False, "Неверный folder_id"
            return False, f"Ошибка API ({resp.status_code})"

        return await asyncio.to_thread(_probe)
