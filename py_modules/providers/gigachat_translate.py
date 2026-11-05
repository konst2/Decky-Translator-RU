# providers/gigachat_translate.py
# GigaChat (Sber) translation provider

import asyncio
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Tuple

import requests

from .base import TranslationProvider, ProviderType, NetworkError, ApiKeyError, RateLimitError

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
_API_BASE = "https://api.giga.chat/v1"
_CHAT_URL = f"{_API_BASE}/chat/completions"
_MODELS_URL = f"{_API_BASE}/models"

# Individual (physical person) plan. Corporate plans need another scope and
# are not what a Steam Deck user has.
_OAUTH_SCOPE = "GIGACHAT_API_PERS"

# The first model in /v1/models is used; the list changes over time (the
# former "GigaChat" model was removed in favour of GigaChat-2 and GigaChat-3),
# so it is not hardcoded. The fallback is only for the case when the list
# cannot be fetched at all.
_FALLBACK_MODEL = "GigaChat-2"

# The CA chain of the Ministry of Digital Development is not in any system
# store (macOS, SteamOS, browsers), so requests would reject the TLS
# certificate. The bundle ships with the plugin and is used only for the two
# GigaChat hosts; disabling verification is not an option: the long-lived
# Authorization Key travels over the same connection.
_CA_BUNDLE = os.path.join(os.path.dirname(__file__), "certs", "giga-ca.pem")

# notAfter of the Sub CA in the bundle (see the header of giga-ca.pem). The
# server certificates are signed by it, so this is the last moment TLS can be
# verified. Keep in sync with the dates in the file.
_CA_BUNDLE_NOT_AFTER = datetime(2029, 7, 19, 12, 50, 41, tzinfo=timezone.utc)

_OAUTH_TIMEOUT = 10.0
_CHAT_TIMEOUT = 40.0
_MODELS_TIMEOUT = 10.0

# Access tokens live 30 minutes; refresh a minute early so a token cannot
# expire between the check and the request.
_TOKEN_TTL = 30 * 60
_TOKEN_REFRESH_MARGIN = 60

# The model list is re-read every 10 minutes: models appear and disappear,
# and a stale name turns every translation into a 404.
_MODELS_TTL = 600

# After a failed fetch the list is retried sooner. Waiting the full TTL kept
# the probe red for 10 minutes after the network came back (e.g. the Deck
# woke up before Wi-Fi connected). Retrying on every call is not an option
# either: while /models is down each translation would wait for its timeout.
# 30 s is below the 60 s reachability TTL, so the next probe sees the fix.
_MODELS_RETRY_AFTER_FAILURE = 30

# One request carries several lines, which is faster than one request per
# line (the free plan generates in a single thread). The limits keep the
# prompt and the answer small enough that the model does not merge or drop
# lines.
_MAX_TEXTS_PER_REQUEST = 20
_MAX_CHARS_PER_REQUEST = 4000

# GigaChat error text is shown in the overlay as is; a long one would
# overflow it, so it is cut to this length.
_MAX_ERROR_MESSAGE_LENGTH = 200

_INVALID_RESPONSE_MESSAGE = "GigaChat вернул некорректный ответ"

# finish_reason values whose text must not be shown as a translation.
# "blacklist": GigaChat refused the topic and answered with a canned phrase
# (HTTP 200, so only this field tells it apart from a real translation; game
# text with violence or politics hits it). "length": the answer was cut off,
# a truncated last line could still pass the numbering check.
_UNUSABLE_FINISH_REASONS = {"blacklist", "length"}


class _ChatAnswer(NamedTuple):
    content: str
    finish_reason: str

    @property
    def usable(self) -> bool:
        return self.finish_reason not in _UNUSABLE_FINISH_REASONS

# The plugin uses Google-style codes; GigaChat has one code for Chinese.
_LANGUAGE_ALIASES = {
    "zh-CN": "zh",
    "zh-TW": "zh",
    "zh-Hans": "zh",
    "zh-Hant": "zh",
}

# Language names for the prompt. Russian names match the model's main
# language and read naturally inside a Russian instruction.
_LANGUAGE_NAMES = {
    "ar": "арабский",
    "bg": "болгарский",
    "cs": "чешский",
    "da": "датский",
    "de": "немецкий",
    "el": "греческий",
    "en": "английский",
    "es": "испанский",
    "fi": "финский",
    "fr": "французский",
    "hi": "хинди",
    "hr": "хорватский",
    "hu": "венгерский",
    "id": "индонезийский",
    "it": "итальянский",
    "ja": "японский",
    "ko": "корейский",
    "nl": "нидерландский",
    "no": "норвежский",
    "pl": "польский",
    "pt": "португальский",
    "ro": "румынский",
    "ru": "русский",
    "sv": "шведский",
    "th": "тайский",
    "tr": "турецкий",
    "uk": "украинский",
    "vi": "вьетнамский",
    "zh": "китайский",
}

_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.*)$")


def _normalize_language(code: str) -> str:
    if not code:
        return code
    return _LANGUAGE_ALIASES.get(code, code)


def _language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(_normalize_language(code), code)


def _shorten(message: str) -> str:
    message = message.strip()
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[:_MAX_ERROR_MESSAGE_LENGTH].rstrip() + "…"
    return message


def _check_bundle_fresh() -> None:
    """Fail with an actionable message once the shipped CA chain has expired."""
    if datetime.now(timezone.utc) >= _CA_BUNDLE_NOT_AFTER:
        logger.error(f"GigaChat CA bundle expired on {_CA_BUNDLE_NOT_AFTER.isoformat()}")
        raise NetworkError("Сертификат GigaChat устарел. Обновите плагин")


def _flatten(text: str) -> str:
    """Collapse whitespace so every line keeps its number on one output line."""
    return " ".join(text.split())


_QUOTE_PAIRS = (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'"))


def _quote_pair(value: str) -> Optional[Tuple[str, str]]:
    """Return the quote pair that wraps the whole value, or None."""
    for opening, closing in _QUOTE_PAIRS:
        if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
            return opening, closing
    return None


def _clean_translation(text: str, source: str) -> str:
    """
    Strip surrounding quotes the model sometimes adds around a translation.

    Only when the source itself is not quoted: a quoted game line ("Hello")
    must keep its quotes in the translation. The quote style may differ
    (the model turns "..." into «...»), so any pair around the source counts.
    """
    value = text.strip()
    if _quote_pair(source.strip()) is not None:
        return value
    if _quote_pair(value) is not None:
        value = value[1:-1].strip()
    return value


def _parse_numbered(text: str, sources: List[str]) -> Optional[List[str]]:
    """
    Parse the numbered answer "1. ...", "2. ..." into a list, one per source.

    None means the answer cannot be trusted (a missing number, a duplicate,
    an empty line), and the caller falls back to one request per line instead
    of returning a wrong translation for a line.
    """
    expected_count = len(sources)
    found = {}
    current = None
    for raw_line in text.splitlines():
        match = _NUMBERED_LINE_RE.match(raw_line)
        if match:
            number = int(match.group(1))
            if number in found or not 1 <= number <= expected_count:
                return None
            found[number] = match.group(2).strip()
            current = number
        elif raw_line.strip() and current is not None:
            # A translation that wrapped onto the next line still belongs to
            # the last number.
            found[current] = f"{found[current]} {raw_line.strip()}".strip()
        # Text before the first number is the model's preamble and is ignored.

    if set(found) != set(range(1, expected_count + 1)):
        return None
    translations = [
        _clean_translation(found[number], source)
        for number, source in enumerate(sources, start=1)
    ]
    if any(not item for item in translations):
        return None
    return translations


def _make_batches(texts: List[str]):
    """Yield lists of indexes whose flattened texts fit into one request."""
    batch: List[int] = []
    chars = 0
    for index, text in enumerate(texts):
        size = len(_flatten(text))
        if batch and (chars + size > _MAX_CHARS_PER_REQUEST or len(batch) >= _MAX_TEXTS_PER_REQUEST):
            yield batch
            batch, chars = [], 0
        batch.append(index)
        chars += size
    if batch:
        yield batch


def _error_message(response) -> str:
    """Return the message of a GigaChat/OpenAI-style error response, or ""."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""

    message = payload.get("message")
    if not isinstance(message, str):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
    if not isinstance(message, str):
        return ""
    return _shorten(message)


class GigaChatTranslateProvider(TranslationProvider):
    """Translation provider using the GigaChat chat completions API."""

    SUPPORTED_LANGUAGES = [
        'auto', 'en', 'ja', 'zh', 'ko', 'de', 'fr', 'es', 'it',
        'pt', 'ru', 'ar', 'nl', 'no', 'pl', 'tr', 'uk', 'hi', 'el', 'th', 'vi', 'fi', 'id', 'ro', 'bg', 'hr',
        'cs', 'hu', 'sv', 'da'
    ]

    def __init__(self, api_key: str = ""):
        """Initialize the GigaChat provider."""
        self._api_key = api_key
        self._model = ""
        # Shared cache: the reachability probe and translation must not fetch
        # the list more often than once per _MODELS_TTL.
        self._models: Optional[List[str]] = None
        # When to fetch the list next: after a success and after a failure
        # the delays differ, so the deadline is stored, not the fetch time.
        self._models_refresh_at = 0.0
        self._access_token = ""
        self._token_expires_at = 0.0
        # test_network and translate run in worker threads, so the token and
        # model caches need locks: two simultaneous calls must not fetch twice.
        self._token_lock = threading.Lock()
        self._model_lock = threading.Lock()
        logger.debug("GigaChatTranslateProvider initialized")

    def set_api_key(self, api_key: str) -> None:
        """Update the Authorization Key and drop token and model caches."""
        if api_key != self._api_key:
            self._api_key = api_key
            self._invalidate_token()
            # A different key can see a different set of models, so the list
            # cached for the old key must not be reused.
            with self._model_lock:
                self._models = None
                self._models_refresh_at = 0.0
                self._model = ""

    @property
    def name(self) -> str:
        return "GigaChat"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GIGACHAT

    def is_available(self, source_lang: str, target_lang: str) -> bool:
        """Check if translation is available for the language pair."""
        if not self._api_key:
            return False
        source = _normalize_language(source_lang)
        target = _normalize_language(target_lang)
        if source != 'auto' and source not in self.SUPPORTED_LANGUAGES:
            return False
        return target in self.SUPPORTED_LANGUAGES and target != 'auto'

    # Облачный метод: модель не скачивается (контракт — ModelContract в base.py).
    def local_model(self) -> None:
        return None

    def is_model_downloaded(self) -> bool:
        return True

    def get_supported_languages(self) -> List[str]:
        """Return list of supported language codes."""
        return self.SUPPORTED_LANGUAGES.copy()

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate a single text using GigaChat."""
        if not text or not text.strip():
            return text
        results = await self.translate_batch([text], source_lang, target_lang)
        # None (GigaChat refused the line) becomes the original here: this
        # method promises a str, and nothing outside tests calls it.
        if not results or results[0] is None:
            return text
        return results[0]

    async def translate_batch(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[Optional[str]]:
        """
        Translate multiple texts using GigaChat.

        Args:
            texts: List of texts to translate
            source_lang: Source language code or "auto"
            target_lang: Target language code

        Returns:
            List of translated texts in the same order. An item is None when
            GigaChat refused that line or cut its answer off: main.py shows
            the original for None and does not put it into the translation
            cache, so a refusal phrase never reaches the screen or the cache.
        """
        if not self._api_key:
            logger.error("GigaChat Authorization Key not configured")
            raise ApiKeyError("Не задан Authorization Key GigaChat")

        if not texts:
            return []

        if not os.path.isfile(_CA_BUNDLE):
            logger.error(f"GigaChat CA bundle not found: {_CA_BUNDLE}")
            raise NetworkError(
                "Не найден сертификат Минцифры для GigaChat. Переустановите плагин"
            )
        _check_bundle_fresh()

        source = _normalize_language(source_lang)
        target = _normalize_language(target_lang)

        originals: List[str] = []
        positions: List[int] = []
        to_translate: List[str] = []
        for text in texts:
            value = text if isinstance(text, str) else ("" if text is None else str(text))
            originals.append(value)
            if value.strip():
                positions.append(len(originals) - 1)
                to_translate.append(value)

        translated: List[Optional[str]] = list(originals)
        for batch in _make_batches(to_translate):
            batch_texts = [to_translate[i] for i in batch]
            logger.debug(f"Translating {len(batch_texts)} texts via GigaChat: {source} -> {target}")
            # Any failure raises: returning the original text here would look
            # like a successful translation to the user.
            results = await asyncio.to_thread(
                self._translate_request, batch_texts, source, target
            )
            for index, result in zip(batch, results):
                translated[positions[index]] = result

        return translated

    def _translate_request(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[Optional[str]]:
        """Translate a batch with one request, or line by line if it fails."""
        answer = self._chat(self._batch_messages(texts, source_lang, target_lang))
        if answer.usable:
            parsed = _parse_numbered(answer.content, texts)
            if parsed is not None:
                return parsed
            # The model merged or dropped a line. Rather than guessing which
            # translation belongs to which line, ask one line at a time:
            # slower, but every line is matched exactly.
            logger.warning(
                f"GigaChat answer did not match {len(texts)} numbered lines, "
                "falling back to one request per line"
            )
        else:
            # A refusal is usually caused by one line but covers the whole
            # batch. Line by line, only that line is lost. A cut-off batch
            # is retried the same way: shorter answers fit.
            logger.warning(
                f"GigaChat batch answer finished with '{answer.finish_reason}', "
                "falling back to one request per line"
            )
        return [
            self._translate_single(text, source_lang, target_lang)
            for text in texts
        ]

    def _translate_single(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        answer = self._chat(self._single_messages(text, source_lang, target_lang))
        if not answer.usable:
            # Not an error: the rest of the screen is translated, this line
            # stays in the original (see translate_batch about None).
            logger.warning(
                f"GigaChat did not translate a line: finish_reason '{answer.finish_reason}'"
            )
            return None
        result = _clean_translation(answer.content, text)
        if not result:
            logger.error("GigaChat returned an empty translation")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE)
        return result

    def _system_prompt(self, source_lang: str, target_lang: str) -> str:
        target_name = _language_name(target_lang)
        prompt = (
            f"Ты переводчик. Переводи текст на {target_name}. "
            "Сохраняй имена, термины и стиль оригинала. "
            "Не добавляй пояснений и не пересказывай."
        )
        if source_lang and source_lang != "auto":
            prompt += f" Исходный язык: {_language_name(source_lang)}."
        return prompt

    def _batch_messages(self, texts: List[str], source_lang: str, target_lang: str) -> List[dict]:
        target_name = _language_name(target_lang)
        lines = "\n".join(
            f"{number}. {_flatten(text)}" for number, text in enumerate(texts, start=1)
        )
        return [
            {"role": "system", "content": self._system_prompt(source_lang, target_lang)},
            {
                "role": "user",
                "content": (
                    f"Ниже {len(texts)} строк текста из игры. Переведи каждую строку на {target_name}.\n"
                    f"Ответь ровно {len(texts)} строками в формате «<номер>. <перевод>», "
                    "в том же порядке. Ничего больше не добавляй — ни исходного текста, ни пояснений.\n\n"
                    f"{lines}"
                ),
            },
        ]

    def _single_messages(self, text: str, source_lang: str, target_lang: str) -> List[dict]:
        return [
            {"role": "system", "content": self._system_prompt(source_lang, target_lang)},
            {
                "role": "user",
                "content": (
                    "Переведи текст ниже. Ответь только переводом, без пояснений и кавычек.\n\n"
                    f"{_flatten(text)}"
                ),
            },
        ]

    @staticmethod
    def _payload(messages: List[dict], model: str) -> dict:
        return {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
        }

    def _fetch_models(self, token: str) -> Optional[List[str]]:
        """
        Return chat model ids in the order the API lists them.

        None means the list could not be fetched; an empty list means the API
        answered but offered no chat models. Embeds are filtered out: they are
        listed next to chat models but cannot translate.
        """
        try:
            response = requests.get(
                _MODELS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=_MODELS_TIMEOUT,
                verify=_CA_BUNDLE,
            )
        except requests.exceptions.Timeout as e:
            logger.warning(f"GigaChat model list timeout: {e}")
            return None
        except requests.exceptions.SSLError as e:
            logger.warning(f"GigaChat model list TLS error: {e}")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"GigaChat model list connection error: {e}")
            return None
        except Exception as e:
            logger.warning(f"GigaChat model list error: {e}")
            return None

        if response.status_code != 200:
            logger.warning(f"GigaChat model list: HTTP {response.status_code}")
            return None

        try:
            payload = response.json()
        except ValueError:
            logger.warning("GigaChat model list returned invalid JSON")
            return None

        models: List[str] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            if "embedding" in model_id.lower():
                continue
            models.append(model_id)

        logger.info(f"GigaChat model list: {len(models)} chat models available")
        return models

    def _get_models(self, token: str, force: bool = False) -> Optional[List[str]]:
        """
        Return the cached model list, refreshing it at most once per TTL.

        The probe and translation share this cache: a translation must not
        re-read the list on every request, and a model rename is still picked
        up without a plugin update.
        """
        with self._model_lock:
            now = time.time()
            if not force and now < self._models_refresh_at:
                return self._models

            models = self._fetch_models(token)
            if models is None:
                self._models_refresh_at = now + _MODELS_RETRY_AFTER_FAILURE
                logger.warning("GigaChat model list unavailable, keeping previous list")
                return self._models
            self._models_refresh_at = now + _MODELS_TTL
            self._models = models
            return models

    def _ensure_model(self, token: str, force: bool = False) -> str:
        """Return the first available chat model."""
        models = self._get_models(token, force=force)
        with self._model_lock:
            if models:
                if models[0] != self._model:
                    logger.info(f"GigaChat model selected: {models[0]}")
                self._model = models[0]
                return self._model

            # The list is unavailable: try the previous choice or the last
            # known model name rather than failing before the chat request.
            if not self._model:
                self._model = _FALLBACK_MODEL
            logger.warning(
                f"GigaChat model list unavailable, using model {self._model}"
            )
            return self._model

    def _chat(self, messages: List[dict]) -> _ChatAnswer:
        """Send one chat request and return the assistant's text and finish_reason."""
        token = self._ensure_token()
        model = self._ensure_model(token)
        response = self._post_chat(token, self._payload(messages, model))

        # The token can expire or be revoked between the check and the
        # request; one refresh is cheap and avoids a failed translation.
        if response.status_code == 401:
            logger.info("GigaChat token rejected, refreshing it once")
            self._invalidate_token()
            token = self._ensure_token()
            response = self._post_chat(token, self._payload(messages, model))

        # The model list changes: a name can be removed or replaced. Re-read
        # the list and retry once with the current first model.
        if response.status_code == 404:
            logger.warning(f"GigaChat model {model} not found, refreshing the list")
            new_model = self._ensure_model(token, force=True)
            if new_model != model:
                logger.info(f"GigaChat retry with model {new_model}")
                response = self._post_chat(token, self._payload(messages, new_model))

        if response.status_code != 200:
            self._handle_chat_error(response)

        try:
            body = response.json()
        except ValueError as e:
            logger.error("GigaChat returned invalid JSON")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE) from e

        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            logger.info(f"GigaChat tokens: {usage.get('total_tokens', '?')}")

        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            logger.error(f"Unexpected GigaChat response structure: {str(body)[:500]}")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE)

        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = ""
        # Checked before the content: a refused or cut-off answer is handled
        # by the caller even when its text is empty, it is not a broken reply.
        if finish_reason in _UNUSABLE_FINISH_REASONS:
            return _ChatAnswer(content if isinstance(content, str) else "", finish_reason)

        if not isinstance(content, str) or not content.strip():
            logger.error("GigaChat response has no translation text")
            raise NetworkError(_INVALID_RESPONSE_MESSAGE)
        return _ChatAnswer(content, finish_reason)

    def _post_chat(self, token: str, payload: dict):
        return self._post(
            _CHAT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            timeout=_CHAT_TIMEOUT,
        )

    def _post(self, url: str, headers: dict, timeout: float, data=None, json=None):
        try:
            return requests.post(
                url,
                headers=headers,
                data=data,
                json=json,
                timeout=timeout,
                verify=_CA_BUNDLE,
            )
        except requests.exceptions.Timeout as e:
            logger.error(f"GigaChat timeout: {e}")
            raise NetworkError("Превышено время ожидания GigaChat") from e
        except requests.exceptions.SSLError as e:
            logger.error(f"GigaChat TLS error: {e}")
            raise NetworkError(
                "Сертификат GigaChat не прошёл проверку. Обновите плагин"
            ) from e
        except requests.exceptions.ConnectionError as e:
            logger.error(f"GigaChat connection error: {e}")
            raise NetworkError("Нет подключения к интернету") from e
        except Exception as e:
            logger.error(f"GigaChat request error: {e}")
            raise NetworkError(f"Ошибка запроса к GigaChat: {type(e).__name__}") from e

    # -- OAuth token --

    def _invalidate_token(self) -> None:
        with self._token_lock:
            self._access_token = ""
            self._token_expires_at = 0.0

    def _ensure_token(self) -> str:
        """Return a valid access token, requesting a new one when needed."""
        with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_MARGIN:
                return self._access_token

            token, expires_at = self._fetch_token()
            self._access_token = token
            self._token_expires_at = expires_at
            logger.info("GigaChat access token refreshed")
            return token

    def _fetch_token(self) -> Tuple[str, float]:
        response = self._post(
            _OAUTH_URL,
            headers={
                # The Authorization Key is already Base64 of Client ID and
                # Client Secret; GigaChat expects it in the Basic header.
                "Authorization": f"Basic {self._api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                # Required header, a fresh UUID for every request.
                "RqUID": str(uuid.uuid4()),
            },
            data={"scope": _OAUTH_SCOPE},
            timeout=_OAUTH_TIMEOUT,
        )

        if response.status_code != 200:
            self._handle_oauth_error(response)

        try:
            payload = response.json()
        except ValueError as e:
            logger.error("GigaChat OAuth returned invalid JSON")
            raise NetworkError("GigaChat вернул некорректный ответ при авторизации") from e

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            logger.error(f"GigaChat OAuth response has no access_token: {str(payload)[:300]}")
            raise NetworkError("GigaChat вернул некорректный ответ при авторизации")

        now = time.time()
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)):
            # The API has returned both seconds and milliseconds over time.
            if expires_at > 1e12:
                expires_at /= 1000.0
            expires_at = min(float(expires_at), now + _TOKEN_TTL)
        else:
            expires_at = now + _TOKEN_TTL
        return token, expires_at

    def _handle_oauth_error(self, response) -> None:
        logger.error(f"GigaChat OAuth error: {response.status_code}")
        if response.status_code in (400, 401, 403):
            raise ApiKeyError("Неверный Authorization Key GigaChat")
        if response.status_code == 429:
            raise RateLimitError("Превышен лимит запросов GigaChat. Попробуйте позже")
        if response.status_code >= 500:
            raise NetworkError(
                f"Сервис GigaChat временно недоступен (код {response.status_code})"
            )
        raise NetworkError(f"Ошибка авторизации GigaChat (код {response.status_code})")

    # -- errors --

    def _handle_chat_error(self, response) -> None:
        body = (response.text or "")[:500]
        logger.error(f"GigaChat API error: {response.status_code}")
        if body:
            logger.error(f"Response: {body}")

        if response.status_code == 401:
            raise ApiKeyError("Неверный Authorization Key GigaChat")
        if response.status_code == 403:
            raise ApiKeyError("Доступ к GigaChat запрещён. Проверьте ключ и тариф")
        if response.status_code == 402:
            raise RateLimitError(
                "Закончился бесплатный лимит токенов GigaChat. Проверьте лимит в личном кабинете"
            )
        if response.status_code == 429:
            raise RateLimitError("Превышен лимит запросов GigaChat. Попробуйте позже")
        if response.status_code >= 500:
            raise NetworkError(
                f"Сервис GigaChat временно недоступен (код {response.status_code})"
            )
        if response.status_code == 404:
            raise NetworkError("Модель GigaChat не найдена. Повторите позже или обновите плагин")
        if response.status_code == 400:
            reason = _error_message(response)
            if reason:
                raise NetworkError(f"GigaChat отклонил запрос: {reason}")
            raise NetworkError("GigaChat отклонил запрос (код 400)")
        raise NetworkError(f"Ошибка GigaChat (код {response.status_code})")

    async def test_network(self) -> tuple:
        if not self._api_key:
            return False, "Требуется Authorization Key GigaChat"

        def _probe():
            try:
                _check_bundle_fresh()
                token = self._ensure_token()
            except ApiKeyError:
                return False, "Неверный Authorization Key"
            except RateLimitError:
                return False, "Превышен лимит запросов GigaChat"
            except NetworkError as e:
                return False, str(e) or "Сеть недоступна"

            models = self._get_models(token)
            if models is None:
                return False, "Не удалось получить список моделей GigaChat"
            if not models:
                return False, "Нет доступных моделей GigaChat"

            # The probe and the first translation must use the same model.
            with self._model_lock:
                if models[0] != self._model:
                    logger.info(f"GigaChat model selected: {models[0]}")
                self._model = models[0]
            return True, ""

        return await asyncio.to_thread(_probe)
