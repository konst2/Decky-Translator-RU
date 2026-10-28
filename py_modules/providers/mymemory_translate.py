# providers/mymemory_translate.py
# Free translation via MyMemory (api.mymemory.translated.net), no API key needed

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

import requests

from .base import TranslationProvider, ProviderType, NetworkError, ApiKeyError, RateLimitError

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.mymemory.translated.net/get"

# MyMemory rejects a query longer than 500 characters (characters, not bytes:
# checked live 28.10.2026 with 499 Cyrillic characters).
_MAX_CHARS_PER_QUERY = 500

# One request per text. It is a free service with a small daily quota, so the
# plugin does not hammer it as hard as free Google (10 parallel requests).
_MAX_PARALLEL_REQUESTS = 5

# MyMemory has no "auto": it answers 403 "'AUTO' IS AN INVALID SOURCE LANGUAGE".
# Language detection is turned on by this word instead.
_AUTODETECT = "Autodetect"

# Marker of a machine translation in the "matches" list. Other entries are
# human translations of similar sentences from the translation memory.
_MACHINE_TRANSLATION_AUTHOR = "MT!"

# Text of MyMemory errors is shown in the overlay as is; a long one would
# overflow it, so it is cut to this length.
_MAX_ERROR_MESSAGE_LENGTH = 200

_INVALID_RESPONSE_MESSAGE = "MyMemory вернул некорректный ответ"
_QUOTA_MESSAGE = (
    "Дневной лимит MyMemory исчерпан. Укажите email в настройках метода перевода "
    "(лимит вырастет в 10 раз) или переключитесь на Яндекс"
)


# Where a sentence may end. CJK punctuation is not followed by a space.
_SENTENCE_ENDS = (". ", "! ", "? ", "。", "！", "？")


def _find_cut(text: str) -> int:
    """Return where to cut a text longer than the per-query limit."""
    # A cut in the middle of a sentence spoils its translation (checked live:
    # "Древний в северных горах просыпается дракон"), so the last sentence
    # end within the limit is preferred. The punctuation stays with the first
    # piece. Only a sentence longer than the limit itself is cut at a space,
    # and a single "word" longer than the limit is cut anywhere.
    best_sentence_end = 0
    for mark in _SENTENCE_ENDS:
        position = text.rfind(mark, 0, _MAX_CHARS_PER_QUERY)
        if position >= 0:
            # Keep the mark itself (without its trailing space) in the piece.
            end = position + len(mark.rstrip())
            best_sentence_end = max(best_sentence_end, end)
    if best_sentence_end > 0:
        return best_sentence_end

    space = text.rfind(" ", 0, _MAX_CHARS_PER_QUERY)
    if space > 0:
        return space
    return _MAX_CHARS_PER_QUERY


def _split_oversized(text: str) -> List[str]:
    """Split a text longer than the per-query limit, preferably between sentences."""
    # Similar to yandex_translate.py, but with MyMemory's limit and sentence
    # boundaries. Not shared with it: the Yandex helper is tied to its own
    # module constant, and changing a working provider is out of scope.
    if len(text) <= _MAX_CHARS_PER_QUERY:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > _MAX_CHARS_PER_QUERY:
        cut = _find_cut(remaining)
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _shorten(message: str) -> str:
    message = message.strip()
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[:_MAX_ERROR_MESSAGE_LENGTH].rstrip() + "…"
    return message


def _find_machine_translation(matches) -> Optional[str]:
    """Return the machine translation from the "matches" list, if there is one."""
    if not isinstance(matches, list):
        return None
    for item in matches:
        if not isinstance(item, dict):
            continue
        if item.get("created-by") != _MACHINE_TRANSLATION_AUTHOR:
            continue
        translation = item.get("translation")
        if isinstance(translation, str) and translation.strip():
            return translation
    return None


def _parse_response(payload) -> Optional[str]:
    """
    Extract the translation from a MyMemory response.

    Returns None when the service answered fine but has no translation for
    this text: only this text then stays untranslated. Raises when the whole
    request failed.
    """
    if not isinstance(payload, dict):
        raise NetworkError(_INVALID_RESPONSE_MESSAGE)

    # MyMemory reports errors inside an HTTP 200 response: the real status is
    # in "responseStatus" (a number or a string, e.g. "403"), and the error
    # text is put into "translatedText". Without this check the error text
    # would be shown in the overlay as if it were the translation.
    if payload.get("quotaFinished") is True:
        raise RateLimitError(_QUOTA_MESSAGE)
    status = str(payload.get("responseStatus"))
    if status == "429":
        raise RateLimitError(_QUOTA_MESSAGE)

    data = payload.get("responseData")
    if not isinstance(data, dict):
        raise NetworkError(_INVALID_RESPONSE_MESSAGE)
    best = data.get("translatedText")

    if status != "200":
        details = payload.get("responseDetails") or best
        # A bad "de" parameter: 403 "INVALID EMAIL PROVIDED" (checked live
        # 28.10.2026). Only the user can fix it, so it gets a clear message.
        if isinstance(details, str) and "INVALID EMAIL" in details.upper():
            raise ApiKeyError(
                "MyMemory не принял email. Проверьте адрес в настройках метода перевода"
            )
        if isinstance(details, str) and details.strip():
            raise NetworkError(f"MyMemory отклонил запрос: {_shorten(details)}")
        raise NetworkError(f"MyMemory отклонил запрос (код {status})")

    if not isinstance(best, str):
        raise NetworkError(_INVALID_RESPONSE_MESSAGE)
    # The quota message format is not documented. Known from other clients:
    # "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY...",
    # possibly with status 200. Checked just in case.
    if best.startswith("MYMEMORY WARNING"):
        raise RateLimitError(_QUOTA_MESSAGE)

    # The top result may be a human translation of a merely *similar* sentence
    # from the translation memory: for "Press START to continue" MyMemory
    # picked "Для продолжения нажмите NC start." with match 0.92. When the top
    # result is not an exact match, the machine translation of this very text
    # is more reliable, so it wins if present.
    match = data.get("match")
    is_exact = isinstance(match, (int, float)) and match >= 1
    if not is_exact:
        machine = _find_machine_translation(payload.get("matches"))
        if machine is not None:
            return machine

    if not best.strip():
        return None
    return best


class MyMemoryTranslateProvider(TranslationProvider):
    """Translation provider using the free MyMemory API."""

    SUPPORTED_LANGUAGES = [
        'auto', 'en', 'ja', 'zh-CN', 'zh-TW', 'ko', 'de', 'fr', 'es', 'it',
        'pt', 'ru', 'ar', 'el', 'fi', 'nl', 'no', 'pl', 'tr', 'uk', 'hi', 'th',
        'vi', 'id', 'ro', 'bg', 'hr', 'cs', 'hu', 'sv', 'da'
    ]

    def __init__(self, email: str = ""):
        """
        Args:
            email: Optional contact email. MyMemory raises the daily quota from
                5 000 to 50 000 characters for requests that carry it.
        """
        self._email = email
        logger.debug("MyMemoryTranslateProvider initialized")

    def set_email(self, email: str) -> None:
        self._email = email

    @property
    def name(self) -> str:
        return "MyMemory"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.MYMEMORY

    def is_available(self, source_lang: str, target_lang: str) -> bool:
        """Check if translation is available for the language pair."""
        if source_lang not in self.SUPPORTED_LANGUAGES:
            return False
        return target_lang in self.SUPPORTED_LANGUAGES and target_lang != 'auto'

    def get_supported_languages(self) -> List[str]:
        """Return list of supported language codes."""
        return self.SUPPORTED_LANGUAGES.copy()

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate a single text."""
        if not text or not text.strip():
            return text
        results = await self.translate_batch([text], source_lang, target_lang)
        if results and results[0] is not None:
            return results[0]
        return text

    async def translate_batch(self, texts: List[str], source_lang: str, target_lang: str) -> List[Optional[str]]:
        """
        Translate multiple texts, one request per text, in parallel.

        Returns:
            Translations in the same order. None for a text MyMemory had no
            translation for (main.py shows its original and does not cache it).
            A failure of the request itself raises instead, so the user sees
            an error rather than silently untranslated text.
        """
        if not texts:
            return texts

        source = _AUTODETECT if source_lang == "auto" else source_lang
        langpair = f"{source}|{target_lang}"

        # Split oversized texts, remembering where each piece belongs so the
        # original order can be restored afterwards. Blank texts are passed
        # through untouched instead of being sent to the API.
        originals: List[str] = []
        spans: List[Optional[Tuple[int, int]]] = []
        pieces: List[str] = []
        for text in texts:
            value = text if isinstance(text, str) else ("" if text is None else str(text))
            originals.append(value)
            if not value.strip():
                spans.append(None)
                continue
            parts = _split_oversized(value)
            spans.append((len(pieces), len(pieces) + len(parts)))
            pieces.extend(parts)

        logger.debug(f"Translating {len(texts)} texts ({len(pieces)} requests): {langpair}")
        translated = await asyncio.to_thread(self._translate_pieces, pieces, langpair)

        final: List[Optional[str]] = []
        for index, span in enumerate(spans):
            if span is None:
                final.append(originals[index])
                continue
            start, end = span
            text_pieces = translated[start:end]
            if any(piece is None for piece in text_pieces):
                final.append(None)
            else:
                final.append(" ".join(text_pieces))
        return final

    def _translate_pieces(self, pieces: List[str], langpair: str) -> List[Optional[str]]:
        """Send one request per piece in parallel; runs in a worker thread."""
        if not pieces:
            return []

        session = requests.Session()
        try:
            workers = min(_MAX_PARALLEL_REQUESTS, len(pieces))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                # executor.map re-raises the first exception (in input order)
                # when its results are read, so one failed request fails the
                # whole batch with a message for the user.
                results = executor.map(
                    lambda piece: self._translate_one(session, piece, langpair),
                    pieces,
                )
                return list(results)
        finally:
            session.close()

    def _translate_one(self, session: requests.Session, text: str, langpair: str) -> Optional[str]:
        """Translate one piece. Raises NetworkError / RateLimitError on failure."""
        params = {"q": text, "langpair": langpair}
        if self._email:
            params["de"] = self._email

        try:
            response = session.get(_ENDPOINT, params=params, timeout=10.0)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"MyMemory connection error: {e}")
            raise NetworkError("Нет подключения к интернету") from e
        except requests.exceptions.Timeout as e:
            logger.error(f"MyMemory timeout error: {e}")
            raise NetworkError("Превышено время ожидания ответа MyMemory") from e
        except requests.exceptions.RequestException as e:
            logger.error(f"MyMemory request error: {e}")
            raise NetworkError(f"Ошибка запроса к MyMemory: {type(e).__name__}") from e

        if response.status_code != 200:
            logger.error(f"MyMemory API error: {response.status_code}, body: {(response.text or '')[:300]}")
            if response.status_code == 429:
                raise RateLimitError(_QUOTA_MESSAGE)
            if response.status_code >= 500:
                raise NetworkError(
                    f"Сервис MyMemory временно недоступен (код {response.status_code})"
                )
            raise NetworkError(f"Ошибка MyMemory (код {response.status_code})")

        try:
            payload = response.json()
        except ValueError as e:
            logger.error("MyMemory returned invalid JSON")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE) from e

        try:
            return _parse_response(payload)
        except (NetworkError, ApiKeyError, RateLimitError) as e:
            logger.error(f"MyMemory rejected the request: {e}")
            raise

    async def test_network(self) -> tuple:
        """Check that MyMemory answers with a real translation (uses ~2 chars of quota)."""
        def _probe():
            params = {"q": "ok", "langpair": "en|ru"}
            if self._email:
                params["de"] = self._email
            try:
                resp = requests.get(_ENDPOINT, params=params, timeout=5)
            except (requests.ConnectionError, requests.Timeout):
                return False, "Сеть недоступна"
            except Exception as e:
                return False, f"Ошибка проверки: {type(e).__name__}"

            if resp.status_code == 429:
                return False, "дневной лимит исчерпан"
            if resp.status_code != 200:
                return False, f"Ошибка API ({resp.status_code})"
            try:
                _parse_response(resp.json())
            except ValueError:
                return False, "некорректный ответ"
            except RateLimitError:
                return False, "дневной лимит исчерпан"
            except ApiKeyError:
                return False, "неверный email"
            except NetworkError:
                return False, "некорректный ответ"
            return True, ""

        return await asyncio.to_thread(_probe)
