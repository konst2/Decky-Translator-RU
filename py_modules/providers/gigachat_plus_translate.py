# providers/gigachat_plus_translate.py
# GigaChat+ : GigaChat translation with context.
#
# Each request carries the game name and a glossary of names and terms built
# up during the session, so a character's name and gender and the game's
# terms are translated the same way from screen to screen. The model proposes
# new glossary entries after the translations; the code merges them.
#
# Only the glossary travels between requests, never the raw game lines: one
# "sensitive" line in a raw history would make GigaChat refuse every following
# request until it dropped out.
#
# The glossary lives in memory: it is reset when the game or the language
# pair changes and when the plugin restarts. The longer the session, the more
# consistent the translation.

import logging
import threading
from collections import OrderedDict
from typing import List, Optional, Tuple

from .base import ProviderType, RateLimitError
from .gigachat_client import GigaChatClient, TokenQuotaError
from .gigachat_translate import (
    GigaChatTranslateProvider,
    _flatten,
    _language_name,
    _NUMBERED_LINE_RE,
    _parse_numbered,
)

logger = logging.getLogger(__name__)

# The glossary is sent with every request, so its size is the extra prompt
# cost. 60 short entries is about 2 000 characters: enough for the cast and
# the key terms of a game, small next to the 4 000-character batch limit.
_MAX_GLOSSARY_ENTRIES = 60

# A real name or term is short. Longer "entries" are the model retelling a
# line instead of naming something, and would bloat every following prompt.
_MAX_TERM_LENGTH = 60
_MAX_TERM_TRANSLATION_LENGTH = 80

# The model writes the new glossary entries after this line.
_GLOSSARY_SEPARATOR = "---"

# The model does not always copy the separator exactly: "----", "———",
# "***" were seen as well. A line of 3+ repeats of one of these characters
# is taken as the separator; a real translation is never such a line.
_SEPARATOR_CHARS = "-—–*=_"
_MIN_SEPARATOR_LENGTH = 3

# Accepted between a term and its translation. The prompt asks for "→";
# "->" is what the model writes when it falls back to ASCII.
_ARROWS = ("→", "->")

# Bullets the model sometimes puts before list items.
_BULLET_CHARS = "-*• "

# Gender marks the prompt asks for after a character's translation. The
# model sometimes puts them after the original name as well.
_GENDER_MARKS = ("(м)", "(ж)")

# Typographic apostrophes (OCR, the model) vs the ASCII one: "Lae’zel" in
# the game text and "Lae'zel" in the answer are the same name.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def _split_gender(text: str) -> Tuple[str, str]:
    """Split "Шэдоухарт (ж)" into ("Шэдоухарт", "(ж)"); no mark -> (text, "")."""
    value = text.strip()
    for mark in _GENDER_MARKS:
        if value.endswith(mark):
            return value[:-len(mark)].strip(), mark
    return value, ""


def _model_generation(name: str) -> Tuple[int, ...]:
    """
    Generation number from a model name: "GigaChat-3-Ultra" -> (3,),
    "GigaChat-3.1-Pro" -> (3, 1); no number -> (0,).

    The name is cut by "-" and the first all-digit part is taken, so a future
    "GigaChat-4-Ultra" compares above "GigaChat-3-Ultra" without code changes.
    """
    for part in name.split("-"):
        pieces = part.split(".")
        if part and all(piece.isdigit() for piece in pieces):
            return tuple(int(piece) for piece in pieces)
    return (0,)


def _model_class(name: str) -> str:
    """"ultra", "max", "pro" or "" — a whole name part, not a substring."""
    for part in name.split("-"):
        lowered = part.casefold()
        if lowered in ("ultra", "max", "pro"):
            return lowered
    return ""


def _preferred_model(models: List[str]) -> Optional[str]:
    """
    The model GigaChat+ uses: the strongest one available to the key.

    1. Ultra, the newest generation.
    2. Otherwise Max or Pro: the newest generation first, Max before Pro
       within one generation. Generation goes first because a live run
       showed GigaChat-3-Pro better and faster than GigaChat-2-Max.
    3. Otherwise any model of the newest generation. This is where a light
       model (GigaChat-3-Lightning) can be chosen — only if nothing above.
    Ties keep the API's order (max() returns the first of equal items).
    """
    ultras = [name for name in models if _model_class(name) == "ultra"]
    if ultras:
        return max(ultras, key=_model_generation)

    strong = [name for name in models if _model_class(name) in ("max", "pro")]
    if strong:
        return max(strong, key=lambda name: (_model_generation(name), _model_class(name) == "max"))

    if models:
        return max(models, key=_model_generation)
    return None


def _strip_gender_mark(line: str) -> str:
    """
    Remove a gender mark the model appended to a translated line.

    Seen live on the stronger models: "…хранит своё прошлое в тайне. (ж)" —
    the glossary instruction "add the gender after the translation" was
    applied to the line itself, and the mark would reach the screen. A real
    game line does not end with "(м)"/"(ж)", so cutting it is safe.
    """
    text, mark = _split_gender(line)
    # A line that is nothing but the mark stays as it is: an empty
    # translation would show an empty caption.
    return text if mark and text else line


def _match_key(text: str) -> str:
    """Form used to look a term up in the game text: case and apostrophes ignored."""
    return text.translate(_APOSTROPHES).casefold()


def _split_on_arrow(line: str) -> Optional[Tuple[str, str]]:
    """Split "term → translation" into its two halves, or None."""
    for arrow in _ARROWS:
        if arrow in line:
            source, _, translation = line.partition(arrow)
            return source.strip(), translation.strip()
    return None


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return (
        len(stripped) >= _MIN_SEPARATOR_LENGTH
        and len(set(stripped)) == 1
        and stripped[0] in _SEPARATOR_CHARS
    )


def _split_answer(content: str) -> Optional[Tuple[str, str]]:
    """
    Split the answer into the numbered translations and the glossary part.

    None means the answer cannot be split safely: there is no separator, yet
    something follows the last numbered line. That something would be glued
    to the last translation as its wrapped continuation and reach the screen
    ("Тав пришёл Словарь:"); guessing where the glossary starts once let such
    junk through, so the caller retries without context instead.
    """
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if _is_separator(line):
            return "\n".join(lines[:index]), "\n".join(lines[index + 1:])

    last_numbered = -1
    for index, line in enumerate(lines):
        if _NUMBERED_LINE_RE.match(line):
            last_numbered = index
    trailing = [line for line in lines[last_numbered + 1:] if line.strip()]
    if trailing:
        return None
    return content, ""


def _parse_glossary(text: str, sources: List[str]) -> List[Tuple[str, str]]:
    """
    Parse "term → translation" lines; anything else is skipped.

    A term is accepted only if it occurs in the lines of this batch. Live
    runs showed the model echoing the format example ("оригинал → перевод")
    and listing names that were not in the text; neither must reach the
    glossary, where the first translation sticks for the whole session.

    The glossary is a bonus: a malformed line is dropped, it never fails the
    translation.
    """
    batch_text = _match_key(" ".join(sources))
    entries: List[Tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip(_BULLET_CHARS).strip()
        pair = _split_on_arrow(line)
        if pair is None:
            continue
        source, translation = pair
        # The gender belongs to the translation side only.
        source, _ = _split_gender(source)
        if not source or not translation:
            continue
        if len(source) > _MAX_TERM_LENGTH or len(translation) > _MAX_TERM_TRANSLATION_LENGTH:
            continue
        if _match_key(source) not in batch_text:
            continue
        entries.append((source, translation))
    return entries


class GigaChatPlusTranslateProvider(GigaChatTranslateProvider):
    """GigaChat translation with the game name and a session glossary."""

    def __init__(self, api_key: str = "", client: Optional[GigaChatClient] = None):
        super().__init__(api_key=api_key, client=client)
        self._game = ""
        # The pair the glossary was built for: terms for en→ru are useless
        # (and misleading) for ja→ru.
        self._glossary_pair: Optional[Tuple[str, str]] = None
        # _match_key(term) -> (term, translation). Order is recency of use:
        # the oldest unused entry is evicted first when the glossary is full.
        self._glossary: "OrderedDict[str, Tuple[str, str]]" = OrderedDict()
        # set_game comes from the event loop, translations from a worker
        # thread.
        self._glossary_lock = threading.Lock()
        # Bumped on every reset. A request snapshots it before going to the
        # network; if the game or the language pair changed meanwhile, the
        # answer's terms belong to the old glossary and are dropped.
        self._glossary_epoch = 0
        # The model chosen last time, for logging a change and for the 402
        # message. "own" = differs from the direct method's (first) model.
        self._chosen_model = ""
        self._own_model = ""

    @property
    def name(self) -> str:
        return "GigaChat+"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GIGACHAT_PLUS

    def set_game(self, game: str) -> None:
        """Set the running game's name; a different game starts a new glossary."""
        game = (game or "").strip()
        with self._glossary_lock:
            if game == self._game:
                return
            logger.info(
                f"GigaChat+ game changed, glossary reset ({len(self._glossary)} entries dropped)"
            )
            self._game = game
            self._glossary.clear()
            self._glossary_epoch += 1

    def glossary_entries(self) -> List[Tuple[str, str]]:
        """Current glossary, oldest first (for logs and tests)."""
        with self._glossary_lock:
            return list(self._glossary.values())

    # -- model --

    def _choose_model(self, models: List[str]) -> Optional[str]:
        """The strongest model from the shared list (see _preferred_model)."""
        chosen = _preferred_model(models)
        if chosen and chosen != self._chosen_model:
            logger.info(f"GigaChat+ model selected: {chosen}")
        self._chosen_model = chosen or ""
        own = chosen if chosen and models and chosen != models[0] else ""
        self._own_model = own
        return chosen

    # -- request --

    def _translate_request(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[Optional[str]]:
        """
        Translate with context; a used-up token pool of GigaChat+'s own model
        is reported with the way out.

        Each model has its own free pool. When the strong model's pool ends,
        the direct method on the first model still works: the user is told
        to switch to it rather than silently getting a weaker translation.
        """
        try:
            return self._translate_with_context(texts, source_lang, target_lang)
        except TokenQuotaError as e:
            if not self._own_model or e.model != self._own_model:
                raise
            raise RateLimitError(
                f"Закончился бесплатный лимит токенов модели {e.model} для GigaChat+. "
                "Переключитесь на метод GigaChat"
            ) from e

    def _translate_with_context(
        self, texts: List[str], source_lang: str, target_lang: str
    ) -> List[Optional[str]]:
        """
        Translate a batch with context; on any trouble, translate it as the
        direct method does.

        Falling back without context also covers a refusal caused by the
        game name or a glossary entry rather than by the lines themselves.
        """
        with self._glossary_lock:
            self._reset_glossary_if_pair_changed(source_lang, target_lang)
            self._touch_used_entries(texts)
            game = self._game
            glossary = list(self._glossary.values())
            epoch = self._glossary_epoch

        messages = self._context_messages(texts, source_lang, target_lang, game, glossary)
        answer = self._chat(messages)
        if answer.usable:
            split = _split_answer(answer.content)
            parsed = None
            if split is not None:
                translations_text, glossary_text = split
                parsed = _parse_numbered(translations_text, texts)
            if parsed is not None:
                self._merge_glossary(_parse_glossary(glossary_text, texts), epoch)
                return [_strip_gender_mark(line) for line in parsed]
            logger.warning(
                f"GigaChat+ answer did not match {len(texts)} numbered lines "
                "or had text after them without a separator, retrying without context"
            )
        else:
            logger.warning(
                f"GigaChat+ answer finished with '{answer.finish_reason}', "
                "retrying without context"
            )
        return super()._translate_request(texts, source_lang, target_lang)

    def _context_messages(
        self,
        texts: List[str],
        source_lang: str,
        target_lang: str,
        game: str,
        glossary: List[Tuple[str, str]],
    ) -> List[dict]:
        target_name = _language_name(target_lang)
        system = self._system_prompt(source_lang, target_lang)
        if game:
            system += (
                f" Это текст из игры «{game}»: учитывай её мир, персонажей и устоявшиеся термины."
                f" Если уверенно знаешь, как имена и термины переведены в официальной локализации"
                f" этой игры на {target_name}, используй этот перевод."
            )
        if target_lang == "ru":
            # Live runs mixed «ты» and «вы» within one game. «Вы» is the most
            # common choice of Russian RPG localizations; fixing it keeps
            # every screen in the same register.
            system += " К игроку всегда обращайся на «вы»."

        parts = []
        if glossary:
            glossary_lines = "\n".join(f"{term} → {translation}" for term, translation in glossary)
            parts.append(
                "Словарь игры — переводи эти имена и термины именно так:\n"
                f"{glossary_lines}"
            )

        lines = "\n".join(
            f"{number}. {_flatten(text)}" for number, text in enumerate(texts, start=1)
        )
        # The glossary rules come from live runs: the model listed ordinary
        # words (cleric → жрец), which then stuck for the whole session; put
        # a gender on places and guessed it against "she"/"her" in the text;
        # skipped minor characters. The format is shown by a real-looking
        # example: a placeholder like «оригинал → перевод» was echoed back.
        parts.append(
            f"Ниже {len(texts)} строк текста из игры. Переведи каждую строку на {target_name}.\n"
            f"Ответь ровно {len(texts)} строками в формате «<номер>. <перевод>», в том же порядке.\n"
            f"Затем напиши строку «{_GLOSSARY_SEPARATOR}» и под ней записи для словаря, "
            "по одной в строке: имя или термин как в исходном тексте, стрелка «→», твой перевод.\n"
            "Что выписывать: все имена персонажей из этих строк, даже второстепенных; "
            "названия мест, фракций, божеств, особых предметов и существ этой игры.\n"
            "Что не выписывать: обычные слова (например, cleric, sword, camp, battle, gun), "
            "даже если они встречаются в тексте; записи, которые уже есть в словаре.\n"
            "Пол — только для персонажей и только если он понятен из текста "
            "(she/her — (ж), he/his — (м)): допиши его после перевода в записи словаря. "
            "Если не понятен — не пиши. В переводах строк пол не пиши никогда. "
            "Если у персонажа из словаря не было пола, а теперь он понятен — выпиши его ещё раз "
            "с тем же переводом и полом.\n"
            "Пример записи: Geralt → Геральт (м)\n"
            f"Если записывать нечего, после «{_GLOSSARY_SEPARATOR}» ничего не пиши. "
            "Больше ничего не добавляй.\n\n"
            f"{lines}"
        )

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    # -- glossary (callers hold _glossary_lock where noted) --

    def _reset_glossary_if_pair_changed(self, source_lang: str, target_lang: str) -> None:
        """Caller holds _glossary_lock."""
        pair = (source_lang, target_lang)
        if pair != self._glossary_pair:
            if self._glossary:
                logger.info("GigaChat+ language pair changed, glossary reset")
            self._glossary.clear()
            self._glossary_pair = pair
            self._glossary_epoch += 1

    def _touch_used_entries(self, texts: List[str]) -> None:
        """
        Move entries whose term occurs in this batch to the recent end.

        Caller holds _glossary_lock. A plain substring check: cheap, and a
        rare false hit only keeps an entry a little longer.
        """
        batch_text = _match_key(" ".join(texts))
        for key in list(self._glossary):
            if key in batch_text:
                self._glossary.move_to_end(key)

    def _merge_glossary(self, entries: List[Tuple[str, str]], epoch: int) -> None:
        """
        Add new entries; never overwrite an existing one.

        The first translation of a name sticks: if the model renders it
        differently later, that is the inconsistency the glossary exists to
        prevent. The flip side is that a wrong first entry sticks too, until
        the glossary is reset.
        """
        if not entries:
            return
        with self._glossary_lock:
            if epoch != self._glossary_epoch:
                logger.info("GigaChat+ glossary was reset during the request, new terms dropped")
                return
            added = 0
            gendered = 0
            for term, translation in entries:
                # The same normalization as the "is it in the text" check:
                # "Lae'zel" and "Lae’zel" must be one entry, or the first
                # translation would not stick.
                key = _match_key(term)
                existing = self._glossary.get(key)
                if existing is None:
                    self._glossary[key] = (term, translation)
                    added += 1
                    continue
                if self._add_missing_gender(key, existing, translation):
                    gendered += 1
            while len(self._glossary) > _MAX_GLOSSARY_ENTRIES:
                self._glossary.popitem(last=False)
            if added or gendered:
                logger.info(
                    f"GigaChat+ glossary: +{added}, gender added {gendered}, "
                    f"{len(self._glossary)} entries"
                )

    def _add_missing_gender(
        self, key: str, existing: Tuple[str, str], translation: str
    ) -> bool:
        """
        The one exception to "never overwrite": a gender for a character
        whose entry had none, with the same translation of the name.

        Caller holds _glossary_lock. The gender often becomes clear only on
        a later screen (the first line has no she/he). A different
        translation or a different gender is still ignored: the first
        translation of a name sticks.
        """
        term, old_translation = existing
        old_name, old_gender = _split_gender(old_translation)
        new_name, new_gender = _split_gender(translation)
        if old_gender or not new_gender:
            return False
        if old_name.casefold() != new_name.casefold():
            return False
        self._glossary[key] = (term, f"{old_name} {new_gender}")
        return True
