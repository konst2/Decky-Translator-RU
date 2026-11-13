# providers/gigachat_translate.py
# GigaChat (Sber) translation provider: prompts, batching, answer parsing.
# Network, OAuth and the model list live in gigachat_client.py.

import asyncio
import logging
import re
from contextvars import ContextVar
from typing import List, Optional, Tuple

from .base import TranslationProvider, ProviderType, NetworkError
from .gigachat_client import ChatAnswer, GigaChatClient, INVALID_RESPONSE_MESSAGE

logger = logging.getLogger(__name__)

# The number of the screen being translated (GigaChatClient.begin_screen),
# for every request of that screen: its batches, the line-by-line fallback,
# GigaChat+'s retry without context. A context variable rather than a
# parameter: the requests are made several calls deep, in a worker thread
# (asyncio.to_thread copies the variable there), and a parameter would have
# to go through every method of both providers. Two screens translated at
# once are two asyncio tasks, each with its own value. Anything that is not
# a screen (the glossary cleanup) sends None and is never dropped as stale.
_current_screen: ContextVar[Optional[int]] = ContextVar("gigachat_current_screen", default=None)

# One request carries several lines, which is faster than one request per
# line (the free plan generates in a single thread). The limits keep the
# prompt and the answer small enough that the model does not merge or drop
# lines.
_MAX_TEXTS_PER_REQUEST = 20
_MAX_CHARS_PER_REQUEST = 4000

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


class GigaChatTranslateProvider(TranslationProvider):
    """Translation provider using the GigaChat chat completions API."""

    SUPPORTED_LANGUAGES = [
        'auto', 'en', 'ja', 'zh', 'ko', 'de', 'fr', 'es', 'it',
        'pt', 'ru', 'ar', 'nl', 'no', 'pl', 'tr', 'uk', 'hi', 'el', 'th', 'vi', 'fi', 'id', 'ro', 'bg', 'hr',
        'cs', 'hu', 'sv', 'da'
    ]

    def __init__(self, api_key: str = "", client: Optional[GigaChatClient] = None):
        """
        Initialize the GigaChat provider.

        ProviderManager passes the client it shares between both GigaChat
        methods; without one (tests, a one-off key check) a private client is
        created from api_key.
        """
        self._client = client if client is not None else GigaChatClient(api_key)
        logger.debug("GigaChatTranslateProvider initialized")

    def set_api_key(self, api_key: str) -> None:
        """Update the Authorization Key (on the shared client, if any)."""
        self._client.set_api_key(api_key)

    @property
    def name(self) -> str:
        return "GigaChat"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GIGACHAT

    def is_available(self, source_lang: str, target_lang: str) -> bool:
        """Check if translation is available for the language pair."""
        if not self._client.api_key:
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
        # Key and CA bundle first: a missing one is reported even for an
        # empty request, so the user sees the real problem.
        self._client.check_ready()
        if not texts:
            return []

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
        screen_token = _current_screen.set(self._client.begin_screen())
        try:
            for batch in _make_batches(to_translate):
                batch_texts = [to_translate[i] for i in batch]
                logger.debug(f"Translating {len(batch_texts)} texts via GigaChat: {source} -> {target}")
                # Any failure raises: returning the original text here would look
                # like a successful translation to the user. A screen made stale
                # by a newer one raises TranslationSupersededError (see chat).
                results = await asyncio.to_thread(
                    self._translate_request, batch_texts, source, target
                )
                for index, result in zip(batch, results):
                    translated[positions[index]] = result
        finally:
            _current_screen.reset(screen_token)

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
            raise NetworkError(INVALID_RESPONSE_MESSAGE)
        return result

    def _choose_model(self, models: List[str]) -> Optional[str]:
        """No preference: the client's default, the first model of the list."""
        return None

    def _chat(self, messages: List[dict]) -> ChatAnswer:
        """
        Every request of this method goes through here, so a subclass that
        prefers another model gets it for all of them, fallbacks included.
        A request of a screen that became stale is not sent (_current_screen).
        """
        return self._client.chat(
            messages, choose_model=self._choose_model, screen=_current_screen.get()
        )

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

    async def test_network(self) -> tuple:
        return await asyncio.to_thread(self._client.probe)
