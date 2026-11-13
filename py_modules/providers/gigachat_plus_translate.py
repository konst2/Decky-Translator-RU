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
# The glossary is kept per game and language pair. With a GlossaryStore and
# a game id (the Steam appid) it is saved to disk and loaded back when the
# game is played again, so names stay consistent across plugin restarts.
# Without them (no store, game id unknown) it lives in memory only and is
# lost on restart, as before.

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from typing import List, NamedTuple, Optional, Tuple

from .base import ProviderType, RateLimitError
from .gigachat_client import GigaChatClient, TokenQuotaError
from .glossary_store import (
    REJECTED_BY_CLEANUP,
    REJECTED_BY_USER,
    GlossaryEntry,
    GlossaryKey,
    GlossaryStore,
    RejectedTerm,
)
from .gigachat_translate import (
    GigaChatTranslateProvider,
    _flatten,
    _language_name,
    _normalize_language,
    _NUMBERED_LINE_RE,
    _current_screen,
    _parse_numbered,
)

logger = logging.getLogger(__name__)

# The glossary is sent with every request, so its size is the extra prompt
# cost. 60 short entries is about 2 000 characters: enough for the cast and
# the key terms of a game, small next to the 4 000-character batch limit.
_MAX_GLOSSARY_ENTRIES = 60

# How many new glossary entries one answer may add. A narrative screen
# introduces 0-3 new names: a reader cannot hold more at once (working
# memory is about 4 new items), so writers do not do it. +1 for a place or
# a term next to a new character. A list screen (a roster, the credits)
# would bring dozens and evict the active characters that are not on it.
# A dropped name is not lost: the model proposes it again when it shows up
# in the text. Better to learn a name on its second appearance than to
# throw away what the glossary has built up.
_MAX_NEW_ENTRIES_PER_ANSWER = 5

# «Ты» or «вы» for Russian. History: early live runs (GigaChat-2) mixed
# them within one game, so the prompt said "always «вы» to the player". On
# GigaChat-3-Ultra that rule leaked into dialogue between characters: a
# playful girl to her friend "Хаку, вы покраснели!", a sister to her
# brother "Брат, вы в порядке?". A live comparison of rule variants showed
# that choosing by the relationship fixes the dialogue and keeps system
# messages and narration on «вы» from screen to screen; the explicit «вы»
# for system text and "the same for one pair" stay as a safeguard for
# weaker models.
_RUSSIAN_ADDRESS_RULE = (
    " «Ты» или «вы» выбирай по отношениям говорящего и собеседника, как в хорошей художественной "
    "локализации: друзья, спутники, родные, ровесники, а также грубость и презрение — «ты»; слуга к "
    "господину, к правителю, к незнакомцу, вежливый торговец или стражник — «вы». Это касается и "
    "обращений персонажей к игроку. Системные сообщения, подсказки и повествование, обращённые к "
    "игроку, — всегда на «вы». Держи выбранное обращение одинаковым для одной пары собеседников."
)

# The model appends this to an entry it re-lists to fix the form of the
# translation ("Ukon → Укон (форма)" for an entry stored as "Уконом"). An
# explicit mark, because the model also writes oblique forms by mistake:
# a single plain re-listing must not turn a good "Укон" into "Укону".
# Without the mark a fix needs repeated answers (_FORM_FIX_VOTES).
_FORM_FIX_MARK = "(форма)"

# A form fix may change only the ending of each word: the stem must match
# for at least this many letters, and the differing tails be this short.
# "Уконом → Укон", "Минтару → Минтара" pass; a respelling such as
# "Оштор → Осутор" (seen across live runs) does not, so the mark cannot
# become a way for the name to drift.
_FORM_FIX_MIN_STEM = 3
_FORM_FIX_MAX_TAIL = 3

# A form fix without the mark is applied when the model proposes the same
# form in this many different answers. Live runs: the model did not use the
# mark, but re-listed "Minthara → Минтара (ж)" for a stored "Минтарой (ж)"
# on its own; it copied correct entries from the glossary as they were, and
# wrote oblique forms only when creating an entry. A one-off slip is
# unlikely to repeat for an existing entry, a real fix repeats.
_FORM_FIX_VOTES = 2

# Speech manner of characters ("грубо, коротко, на «ты»"). Live runs: kept
# inside the glossary ("translate these names exactly so") the note was
# copied from screen to screen and never updated when a rough warrior began
# to speak with deference; moved to a separate block "how they spoke before
# (may change)" with the request to describe THESE lines, the change was
# caught at once. The model then describes the manner on every screen in its
# own words, so notes rarely match literally: a note is replaced when the
# description shares no word with it on this many screens in a row where
# the character speaks. Synonyms may trigger a harmless replacement
# ("грубо, самоуверенно" -> "пренебрежительно, коротко"); a lag of two
# screens did not spoil the translation of the new manner in live runs.
_SPEECH_PREFIX = "речь:"
_SPEECH_CHANGE_SCREENS = 2
# 3-6 words are asked for; a longer "note" is the model retelling the scene.
_MAX_SPEECH_LENGTH = 60
# Between the name and the note; the model writes an em dash, but a plain
# hyphen or an en dash is what it may fall back to.
_SPEECH_DASHES = (" — ", " – ", " - ")
# Words shorter than this ("на", "ты", "вы", "и") do not count when notes are
# compared: "грубо, на «ты»" and "почтительно, на «вы»" must not look alike.
_SPEECH_MIN_WORD = 3

# Words of interface labels. A proposed term that contains one of them as a
# whole word is dropped before it reaches the glossary. The prompt forbids
# interface labels, yet live runs on visual novels still let the button bar
# through ("lb Back", "BHide Window", "SaveBack"), and once in the glossary a
# label is re-listed on most screens and evicts real names: it shows up on
# every screen, so it always counts as recently used. Only words that are
# almost never part of a name are listed; the rest ("New Game", "Gallery")
# is left to the model. The cost is a rare real name with such a word in it
# ("Load Bearing Tower"), accepted as unlikely (decision of the user,
# 13.11.2026). Whole words only: "Backstreet" or "Saveria" must pass.
# Glued labels are split into words first (_term_words). Single gamepad
# letters (A, B, X, Y) are not listed: "Agent Y", "X-23" are real names,
# and in a glued label the letter comes with a listed word anyway ("BHide").
_INTERFACE_WORDS = frozenset({
    # Visual novel button bar and system menu.
    "auto", "automode", "backlog", "log", "skip", "save", "load", "qsave", "qload",
    "hide", "window", "config", "settings", "options", "menu", "system",
    "next", "back", "pause", "quit", "cancel", "confirm",
    # Gamepad bumpers, triggers and Switch buttons that OCR glues to labels.
    "lb", "rb", "lt", "rt", "zl", "zr",
})

# Glossary cleanup: from time to time the glossary is sent to the model in a
# separate request, "which of these are interface labels?", and the labels
# found are moved to the black list. The stop list above catches only the
# usual words; a short separate task is easier for the model than one more
# rule in the long translation prompt. Live runs on GigaChat-3-Ultra
# (5 games, 26 requests): no real name or term deleted, every menu item and
# glued label found ("New Game", "Return to Title", "SaveBack", "EchoAuto");
# code names made of ordinary words ("Joker", "Queen") and game terms that
# look like ordinary words ("Darkness", "Boon") kept. OCR junk ("Tl1e") is
# not found and not hunted: OCR is good and the user can delete it by hand.
# Off switch: a stronger model may stop listing interface labels at all.
_GLOSSARY_CLEANUP_ENABLED = True
# The cleanup runs when this many new entries came since the last one: junk
# only ever arrives with new entries, so a glossary that does not grow is
# not sent again.
_CLEANUP_AFTER_NEW_ENTRIES = 10
# ...or this many once the glossary is more than 2/3 full. Labels are on
# every screen, so they always count as recently used, and a full glossary
# evicts real characters instead of them.
_CLEANUP_AFTER_NEW_ENTRIES_FULL = 3
_CLEANUP_FULL_SHARE = 2 / 3
# The cleanup starts only after this long without translations, and never
# while one is running. Requests go one at a time anyway (GigaChatClient.chat),
# but a cleanup squeezed between two quick screens would make the second one
# wait and would look like a burst of requests to the service.
_CLEANUP_IDLE_SECONDS = 5
# Not more often than this, even if the trigger fires again: a model that
# keeps leaving the junk in must not cause a request every few screens. A
# failed cleanup waits for this as well — it is not retried at once.
_CLEANUP_MIN_INTERVAL_SECONDS = 600
# An answer naming more than this share of the glossary is not trusted: the
# model has misread the task, a real glossary is mostly names.
_CLEANUP_MAX_DELETE_SHARE = 0.5
# The wording that won the live runs (answer with numbers only; labelling
# every entry was longer, slower and dropped lines). A remark about OCR junk
# was tried and left out: it found junk unreliably and "names with distorted
# letters" invites deleting unusual real names.
_CLEANUP_INSTRUCTION = (
    "В словарь должны попадать только имена персонажей, названия мест, фракций, божеств, "
    "особых предметов, существ и понятий мира этой игры. Элементы интерфейса — это кнопки, "
    "пункты меню, подсказки управления и системные надписи (например, Auto Mode, Backlog, "
    "Skip, Save, Options, Continue), в том числе с буквой или названием кнопки геймпада впереди "
    "(A Next, Y Auto Mode, lb Back) и склеенные при распознавании текста (AutoMode, BHide Window).\n"
    "Найди в словаре записи, которые являются элементами интерфейса, а не частью мира игры. "
    "Ответь только номерами таких записей через запятую, без пояснений. "
    "Если таких записей нет, ответь «нет»."
)
_CLEANUP_NOTHING = "нет"

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


def name_and_gender(translation: str) -> Tuple[str, str]:
    """
    "Шэдоухарт (ж)" -> ("Шэдоухарт", "ж"); no mark -> (translation, "").

    For the glossary window: it shows the gender apart from the name, and
    the format of the mark stays known in this module only.
    """
    name, mark = _split_gender(translation)
    return name, mark.strip("()")


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


def _game_id_text(game_id) -> str:
    """
    The game id as text: "" when unknown.

    The frontend sends Steam's appid. Its declared type is a string, but
    Steam's other app types use numbers; a number here must not break the
    translation (str.strip on an int raises).
    """
    if game_id is None:
        return ""
    return str(game_id).strip()


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


def _starts_new_word(term: str, index: int) -> bool:
    """
    Whether the capital letter at `index` begins a new word of a glued label.

    "SaveBack": a capital after a small letter. "QSave", "BHide": a capital
    after a capital, followed by a small letter — the first capital is a
    word of its own (a gamepad letter glued by OCR). "HP" stays one word.
    """
    char = term[index]
    previous = term[index - 1]
    if not char.isupper():
        return False
    if previous.islower():
        return True
    following = term[index + 1] if index + 1 < len(term) else ""
    return previous.isupper() and following.islower()


def _term_words(term: str) -> List[str]:
    """
    Words of a term, case-folded: split on anything that is not a letter or
    a digit, and inside glued labels on capitals (_starts_new_word).

    "lb Back" -> ["lb", "back"]; "QSaveQLoad" -> ["q", "save", "q", "load"];
    "Q.Save" -> ["q", "save"]; "LeBlanc" -> ["le", "blanc"].
    """
    words: List[str] = []
    current = ""
    for index, char in enumerate(term):
        if not char.isalnum():
            if current:
                words.append(current)
                current = ""
            continue
        if current and _starts_new_word(term, index):
            words.append(current)
            current = ""
        current += char
    if current:
        words.append(current)
    return [word.casefold() for word in words]


def _is_interface_term(term: str) -> bool:
    """
    Whether a proposed term is an interface label or OCR junk, not a name.

    A term without a single letter (">>", "42") is never a name either.
    """
    if not any(char.isalpha() for char in term):
        return True
    return any(word in _INTERFACE_WORDS for word in _term_words(term))


def _parse_cleanup_answer(content: str, count: int) -> Optional[List[int]]:
    """
    Entry numbers from the cleanup answer, or None if it cannot be trusted.

    Strict on purpose: the answer must be numbers and commas only, or
    "нет". Picking every number out of free text would be wrong: if the
    model echoed the entries ("3. 2B → Ту-Би, 7. X-23 → Икс-23"), the digits
    of the names (2, 23) would delete other entries. A number out of range
    or repeated also means the model is confused, and the whole answer is
    dropped: deleting nothing is safe, deleting a wrong entry is not.
    """
    text = content.strip().rstrip(".").strip()
    if not text:
        return None
    if text.casefold() == _CLEANUP_NOTHING:
        return []
    numbers: List[int] = []
    for part in text.split(","):
        part = part.strip()
        # isascii: str.isdigit also accepts "²" and other digits int() rejects.
        if not (part.isascii() and part.isdigit()):
            return None
        number = int(part)
        if number < 1 or number > count or number in numbers:
            return None
        numbers.append(number)
    return numbers


def _is_protected_from_cleanup(entry: GlossaryEntry) -> bool:
    """
    Whether the cleanup must keep the entry whatever the model says.

    A gender or a speech manner is set only for characters, so the entry
    is a name for sure; losing a character is worse than keeping a label.
    """
    return bool(_split_gender(entry.translation)[1]) or bool(entry.speech)


def _start_timer(seconds: float, callback) -> threading.Timer:
    """A daemon timer: a pending cleanup must not keep the plugin from unloading."""
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


class ProposedEntry(NamedTuple):
    """A glossary entry as the model proposed it, after parsing."""

    term: str
    translation: str
    # The model marked it as a fix of the form of an existing entry.
    form_fix: bool


def _parse_speech(text: str) -> List[Tuple[str, str]]:
    """
    Parse "Речь: <name> — <note>" lines of the glossary part: (name, note).

    The name is the original or the translation (the model writes both);
    _merge_glossary resolves it to an entry. Malformed or too long lines
    are skipped: the note is a bonus, it never fails the translation.
    """
    notes: List[Tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip(_BULLET_CHARS).strip()
        if not line.casefold().startswith(_SPEECH_PREFIX):
            continue
        rest = line[len(_SPEECH_PREFIX):].strip()
        name, note = "", ""
        for dash in _SPEECH_DASHES:
            if dash in rest:
                name, _, note = rest.partition(dash)
                break
        name = name.strip().strip("«»\"'")
        note = note.strip().rstrip(".").strip()
        if not name or not note or len(note) > _MAX_SPEECH_LENGTH:
            continue
        notes.append((name, note))
    return notes


def _pull_speech_lines(translations_text: str) -> Tuple[str, str]:
    """
    Take unnumbered "Речь:" lines out of the translations part.

    The model is asked to write them after the separator, but if one slips
    in among the translations, the numbered parser would glue it to the
    previous line as its wrapped continuation and it would reach the screen
    ("Привет Речь: Ukon — грубо"). A translation always starts with its
    number, so an unnumbered "Речь:" line is never one; a numbered line
    that starts with "Речь:" is a game line and stays. Returns the cleaned
    translations and the pulled lines (parsed as speech notes).
    """
    kept: List[str] = []
    pulled: List[str] = []
    for line in translations_text.splitlines():
        unnumbered = _NUMBERED_LINE_RE.match(line) is None
        bare = line.strip().lstrip(_BULLET_CHARS).strip()
        if unnumbered and bare.casefold().startswith(_SPEECH_PREFIX):
            pulled.append(line)
        else:
            kept.append(line)
    return "\n".join(kept), "\n".join(pulled)


def _speech_words(note: str) -> set:
    """Meaningful words of a speech note, for "is it the same manner" checks."""
    letters_only = "".join(char if char.isalpha() else " " for char in note.casefold())
    return {word for word in letters_only.split() if len(word) >= _SPEECH_MIN_WORD}


def _split_form_fix_mark(translation: str) -> Tuple[str, bool]:
    """
    Remove the form-fix mark: "Укон (форма)" -> ("Укон", True).

    The mark is looked for anywhere, not only at the end: with a gender the
    model may write "Мунэтика (форма) (ж)" as well as "Мунэтика (ж) (форма)".
    """
    if _FORM_FIX_MARK not in translation:
        return translation, False
    cleaned = " ".join(translation.replace(_FORM_FIX_MARK, " ").split())
    return cleaned, True


def _is_ending_change(old: str, new: str) -> bool:
    """
    Whether `new` differs from `old` only in word endings (see _FORM_FIX_*).

    Compared word by word, case ignored; the number of words must match and
    at least one word must differ.
    """
    old_words = old.casefold().split()
    new_words = new.casefold().split()
    if len(old_words) != len(new_words) or old_words == new_words:
        return False
    for old_word, new_word in zip(old_words, new_words):
        if old_word == new_word:
            continue
        stem = 0
        for old_char, new_char in zip(old_word, new_word):
            if old_char != new_char:
                break
            stem += 1
        if stem < _FORM_FIX_MIN_STEM:
            return False
        if len(old_word) - stem > _FORM_FIX_MAX_TAIL or len(new_word) - stem > _FORM_FIX_MAX_TAIL:
            return False
    return True


def _gendered_translation(translation: str, source_gender: str) -> str:
    """
    The translation in one form, "Name (ж)" or "Name"; "" if there is no name.

    The gender belongs to the translation side, but the model sometimes puts
    it after the original ("Kuon (ж) → Куон"). Dropping it there lost a
    gender the model did give, so it is moved over. If both sides have one,
    the translation side wins: that is where the prompt asks for it.
    The mark is re-attached with one space, so "Куон(ж)" and "Куон (ж)" are
    stored alike and the "same name" check of _update_gender holds.
    """
    name, gender = _split_gender(translation)
    gender = gender or source_gender
    if not name:
        return ""
    return f"{name} {gender}" if gender else name


def _parse_glossary(text: str, sources: List[str]) -> List[ProposedEntry]:
    """
    Parse "term → translation" lines; anything else is skipped.

    A term is accepted only if it occurs in the lines of this batch. Live
    runs showed the model echoing the format example ("оригинал → перевод")
    and listing names that were not in the text; neither must reach the
    glossary, where the first translation sticks for the whole session.

    Interface labels are dropped as well (_INTERFACE_WORDS) and logged: the
    log shows whether a real name was ever caught by the list.

    The glossary is a bonus: a malformed line is dropped, it never fails the
    translation.
    """
    batch_text = _match_key(" ".join(sources))
    entries: List[ProposedEntry] = []
    interface_terms: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip(_BULLET_CHARS).strip()
        pair = _split_on_arrow(line)
        if pair is None:
            continue
        source, translation = pair
        translation, form_fix = _split_form_fix_mark(translation)
        source, source_gender = _split_gender(source)
        translation = _gendered_translation(translation, source_gender)
        if not source or not translation:
            continue
        if len(source) > _MAX_TERM_LENGTH or len(translation) > _MAX_TERM_TRANSLATION_LENGTH:
            continue
        if _match_key(source) not in batch_text:
            continue
        if _is_interface_term(source):
            interface_terms.append(source)
            continue
        entries.append(ProposedEntry(source, translation, form_fix))
    if interface_terms:
        logger.info(f"GigaChat+ glossary: interface labels dropped: {', '.join(interface_terms)}")
    return entries


class GigaChatPlusTranslateProvider(GigaChatTranslateProvider):
    """GigaChat translation with the game name and a session glossary."""

    def __init__(
        self,
        api_key: str = "",
        client: Optional[GigaChatClient] = None,
        glossary_store: Optional[GlossaryStore] = None,
    ):
        super().__init__(api_key=api_key, client=client)
        self._game = ""
        # Steam appid of the running game: the key of the saved glossary.
        # Empty = unknown, and the glossary is not saved.
        self._game_id = ""
        self._glossary_store = glossary_store
        # The pair the glossary was built (or loaded) for: terms for en→ru
        # are useless (and misleading) for ja→ru. None = no glossary loaded
        # yet for the current game; the next translation loads it.
        self._glossary_pair: Optional[Tuple[str, str]] = None
        # _match_key(term) -> entry. Order is recency of use: the oldest
        # unused entry is evicted first when the glossary is full. The
        # entry's last_used_at mirrors this order, so it survives a save.
        self._glossary: "OrderedDict[str, GlossaryEntry]" = OrderedDict()
        # The black list of the same glossary: _match_key(term) -> rejected
        # term. The model may not add these back. Without it a removed
        # interface label returns on the next screen: the button bar is on
        # every screen, and the model keeps listing it. Loaded, switched and
        # reset together with the glossary.
        self._rejected: "dict[str, RejectedTerm]" = {}
        # Unmarked form fixes waiting for more votes (_consider_form):
        # _match_key(term) -> (proposed name, votes). Memory only: after a
        # restart the votes start again, which only delays a fix.
        self._form_candidates: "dict[str, Tuple[str, int]]" = {}
        # Speech notes waiting to replace the stored one (_consider_speech):
        # _match_key(term) -> (latest different description, screens in a
        # row). Memory only, like the form votes.
        self._speech_candidates: "dict[str, Tuple[str, int]]" = {}
        # The saved glossary of the current game and pair could not be read.
        # Saving is then off until the game or the pair changes: the empty
        # in-memory glossary would otherwise replace everything on disk at
        # the first save.
        self._glossary_load_failed = False
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
        # -- glossary cleanup (see _GLOSSARY_CLEANUP_ENABLED); under _glossary_lock --
        # New entries since the last cleanup of this glossary.
        self._new_since_cleanup = 0
        # A non-empty saved glossary was loaded and has not been cleaned in
        # this session: the counter above starts from zero after a restart,
        # and junk gathered before would wait for ten new entries.
        self._cleanup_after_load = False
        # Clock reading of the last cleanup start; None = none yet.
        self._last_cleanup_at: Optional[float] = None
        # Translations running now, and the clock when the last one ended:
        # the cleanup waits for a pause in translations.
        self._translations_running = 0
        self._last_translation_at = 0.0
        self._cleanup_running = False
        self._cleanup_timer = None
        # Replaced in tests, so they need not wait for real seconds.
        self._clock = time.monotonic
        self._timer_factory = _start_timer

    @property
    def name(self) -> str:
        return "GigaChat+"

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.GIGACHAT_PLUS

    def set_game(self, game: str, game_id: str = "") -> None:
        """
        Set the running game; a different game switches the glossary.

        The new game's saved glossary is loaded on its first translation,
        not here: the glossary is per language pair, and the pair is known
        only when a translation comes.
        """
        game = (game or "").strip()
        game_id = _game_id_text(game_id)
        with self._glossary_lock:
            if game == self._game and game_id == self._game_id:
                return
            logger.info(
                f"GigaChat+ game changed, glossary switched ({len(self._glossary)} entries unloaded)"
            )
            self._game = game
            self._game_id = game_id
            self._glossary.clear()
            self._rejected.clear()
            self._reset_cleanup_state()
            self._form_candidates.clear()
            self._speech_candidates.clear()
            self._glossary_pair = None
            self._glossary_epoch += 1

    def glossary_entries(self) -> List[Tuple[str, str]]:
        """Current glossary as (term, translation), oldest first (for logs and tests)."""
        with self._glossary_lock:
            return [(entry.term, entry.translation) for entry in self._glossary.values()]

    # -- glossary form: view, delete an entry, reset --
    #
    # The form names the game and the pair itself instead of relying on the
    # provider's current game: opened in a new game before its first
    # translation, it must show that game's glossary, not the previous one's.
    # Language codes are normalized the same way translate_batch does it, or
    # "zh-CN" from the settings would miss the glossary saved under "zh".

    def glossary_for(self, game_id: str, source_lang: str, target_lang: str) -> List[GlossaryEntry]:
        """
        Entries of the game's glossary for the pair, least recently used first.

        The glossary in memory when it is the one being used (it is exactly
        what goes into the prompt, saved or not); otherwise the saved one.
        """
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return []
        with self._glossary_lock:
            if self._is_current_glossary(key):
                return list(self._glossary.values())
        if self._glossary_store is None:
            return []
        return self._glossary_store.load_entries(key)

    def delete_glossary_entry(
        self, game_id: str, source_lang: str, target_lang: str, match_key: str
    ) -> bool:
        """Delete one entry from memory (if it is the current glossary) and from the store."""
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return False
        with self._glossary_lock:
            # The store first, under the same lock: a failed write raises
            # before memory is touched, so memory and disk never disagree;
            # and a translation saving its snapshot in between cannot bring
            # the entry back.
            deleted = False
            if self._glossary_store is not None:
                deleted = self._glossary_store.delete_entry(key, match_key)
            if self._is_current_glossary(key):
                deleted = (self._glossary.pop(match_key, None) is not None) or deleted
                self._form_candidates.pop(match_key, None)
                self._speech_candidates.pop(match_key, None)
                # A request in flight was built with the old glossary; its
                # new terms are dropped so as not to mix with the edit.
                self._glossary_epoch += 1
        if deleted:
            logger.info(f"GigaChat+ glossary entry deleted by the user, game {game_id}")
        return deleted

    def reset_glossary(self, game_id: str, source_lang: str, target_lang: str) -> bool:
        """Delete the whole glossary of the game for the pair, in memory and in the store."""
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return False
        with self._glossary_lock:
            # The store first, for the same reasons as in delete_glossary_entry.
            deleted = False
            if self._glossary_store is not None:
                deleted = self._glossary_store.delete_glossary(key)
            if self._is_current_glossary(key):
                deleted = bool(self._glossary) or bool(self._rejected) or deleted
                self._glossary.clear()
                self._rejected.clear()
                self._form_candidates.clear()
                self._speech_candidates.clear()
                self._glossary_epoch += 1
        if deleted:
            logger.info(f"GigaChat+ glossary reset by the user, game {game_id}")
        return deleted

    # -- black list: view, forbid an entry, allow a term again --
    #
    # The same rules as the glossary form above: the caller names the game
    # and the pair; the store is written first, under the glossary lock.

    def rejected_for(self, game_id: str, source_lang: str, target_lang: str) -> List[RejectedTerm]:
        """Black list of the game's glossary for the pair, most recently rejected first."""
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return []
        with self._glossary_lock:
            if self._is_current_glossary(key):
                return sorted(self._rejected.values(), key=lambda item: item.rejected_at, reverse=True)
        if self._glossary_store is None:
            return []
        return self._glossary_store.load_rejected(key)

    def forbid_glossary_entry(
        self, game_id: str, source_lang: str, target_lang: str, match_key: str
    ) -> bool:
        """
        Delete an entry and put its term on the black list.

        Unlike delete_glossary_entry, the model cannot add the term back.
        Plain deletion is for a bad translation (a wrong gender, a clumsy
        name): the model proposes the term again and may do better. Forbid
        is for what is not a name at all — an interface label.
        """
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return False
        with self._glossary_lock:
            current = self._is_current_glossary(key)
            entry = self._find_entry(key, match_key, current)
            if entry is None:
                return False
            rejected = RejectedTerm(
                entry.match_key, entry.term, entry.translation, int(time.time()), REJECTED_BY_USER
            )
            if self._glossary_store is not None:
                # The name of a game that is not running is unknown here;
                # None keeps the stored one.
                game_name = self._game if current else None
                self._glossary_store.reject_entries(key, game_name, [rejected])
            if current:
                self._glossary.pop(match_key, None)
                self._form_candidates.pop(match_key, None)
                self._speech_candidates.pop(match_key, None)
                self._rejected[match_key] = rejected
                # A request in flight was built with the old glossary; its
                # new terms are dropped so as not to mix with the edit.
                self._glossary_epoch += 1
        logger.info(f"GigaChat+ glossary entry rejected by the user: {entry.term}, game {game_id}")
        return True

    def allow_rejected_term(
        self, game_id: str, source_lang: str, target_lang: str, match_key: str
    ) -> bool:
        """Take a term off the black list: the model may propose it again."""
        key = self._form_key(game_id, source_lang, target_lang)
        if key is None:
            return False
        with self._glossary_lock:
            allowed = False
            if self._glossary_store is not None:
                allowed = self._glossary_store.delete_rejected(key, match_key)
            if self._is_current_glossary(key):
                allowed = (self._rejected.pop(match_key, None) is not None) or allowed
        if allowed:
            logger.info(f"GigaChat+ rejected term allowed again by the user, game {game_id}")
        return allowed

    def _find_entry(self, key: GlossaryKey, match_key: str, current: bool) -> Optional[GlossaryEntry]:
        """
        An entry of the glossary `key` names: from memory if it is the
        current one, else from the store. Caller holds _glossary_lock.
        """
        if current:
            return self._glossary.get(match_key)
        if self._glossary_store is None:
            return None
        for entry in self._glossary_store.load_entries(key):
            if entry.match_key == match_key:
                return entry
        return None

    def _form_key(self, game_id: str, source_lang: str, target_lang: str) -> Optional[GlossaryKey]:
        """Key for a form request; None without a game (nothing is saved for it)."""
        game_id = _game_id_text(game_id)
        if not game_id:
            return None
        return GlossaryKey(
            self.provider_type.value,
            game_id,
            _normalize_language(source_lang),
            _normalize_language(target_lang),
        )

    def _is_current_glossary(self, key: GlossaryKey) -> bool:
        """Whether the in-memory glossary is the one `key` names. Caller holds _glossary_lock."""
        return (
            key.game_id == self._game_id
            and (key.source_lang, key.target_lang) == self._glossary_pair
        )

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

        Every batch, with context or not, is counted as a running
        translation: the glossary cleanup waits for a pause between them.
        """
        with self._glossary_lock:
            self._translations_running += 1
        try:
            return self._translate_with_context(texts, source_lang, target_lang)
        except TokenQuotaError as e:
            if not self._own_model or e.model != self._own_model:
                raise
            raise RateLimitError(
                f"Закончился бесплатный лимит токенов модели {e.model} для GigaChat+. "
                "Переключитесь на метод GigaChat"
            ) from e
        finally:
            self._translation_finished()

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
            self._switch_glossary_if_pair_changed(source_lang, target_lang)
            if self._touch_used_entries(texts):
                # Recency decides what is evicted; without saving it, a
                # restart would forget which names are still in use.
                self._save_glossary()
            game = self._game
            glossary = [(entry.term, entry.translation) for entry in self._glossary.values()]
            speech_notes = self._speech_notes_for(texts)
            epoch = self._glossary_epoch

        messages = self._context_messages(
            texts, source_lang, target_lang, game, glossary, speech_notes
        )
        answer = self._chat(messages)
        if answer.usable:
            split = _split_answer(answer.content)
            parsed = None
            stray_speech = ""
            if split is not None:
                translations_text, glossary_text = split
                translations_text, stray_speech = _pull_speech_lines(translations_text)
                parsed = _parse_numbered(translations_text, texts)
            if parsed is not None:
                self._merge_glossary(
                    _parse_glossary(glossary_text, texts),
                    epoch,
                    speech_notes=_parse_speech(stray_speech + "\n" + glossary_text),
                    batch_text=_match_key(" ".join(texts)),
                )
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
        speech_notes: Optional[List[Tuple[str, str]]] = None,
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
            system += _RUSSIAN_ADDRESS_RULE

        parts = []
        if glossary:
            glossary_lines = "\n".join(f"{term} → {translation}" for term, translation in glossary)
            parts.append(
                "Словарь игры — переводи эти имена и термины именно так:\n"
                f"{glossary_lines}"
            )
        if speech_notes:
            # Not inside the glossary: there the model takes a note as fixed
            # and copies it (see _SPEECH_PREFIX).
            speech_lines = "\n".join(f"{term} — {note}" for term, note in speech_notes)
            parts.append(
                "Как персонажи говорили раньше (манера может меняться):\n"
                f"{speech_lines}"
            )

        lines = "\n".join(
            f"{number}. {_flatten(text)}" for number, text in enumerate(texts, start=1)
        )
        # The glossary rules come from live runs: the model listed ordinary
        # words (cleric → жрец), which then stuck for the whole session; put
        # a gender on places; skipped minor characters; wrote a name in the
        # case of the sentence ("Ukon → Уконом"), which then stuck as "the"
        # translation. Gender: the model sets it when it knows the character
        # rather than from pronouns, and did not re-list an entry when the
        # gender became clear, because "do not list entries already in the
        # glossary" contradicted "list it again with the gender". The rules
        # now allow knowledge and make the gender correction the explicit
        # exception. The format is shown by a real-looking example: a
        # placeholder like «оригинал → перевод» was echoed back. The speech
        # examples use words rare in real notes ("с прибаутками"): the model
        # copied the example word for word ("почтительно, официально,
        # церемонно"), and a common word from it ("коротко") showing up in
        # many notes would make every description share a word with the
        # stored one and block the replacement. Interface labels: a visual
        # novel's button bar ("System Y Auto Mode Backlog A Next B Hide
        # Window RE Skip", glued differently by OCR from screen to screen)
        # reached the model alone when the dialogue line came from the
        # translation cache, and the model listed every button as a term;
        # once in the glossary, it re-listed them on most screens. With the
        # rule, live runs gave no interface entries and no re-listing.
        parts.append(
            f"Ниже {len(texts)} строк текста из игры. Переведи каждую строку на {target_name}.\n"
            f"Ответь ровно {len(texts)} строками в формате «<номер>. <перевод>», в том же порядке.\n"
            f"Затем напиши строку «{_GLOSSARY_SEPARATOR}» и под ней записи для словаря, "
            "по одной в строке: имя или термин как в исходном тексте, стрелка «→», твой перевод "
            "в начальной форме — как в словаре, а не в форме из этой фразы "
            "(Geralt → Геральт, а не Геральта).\n"
            "Что выписывать: все имена персонажей из этих строк, даже второстепенных; "
            "названия мест, фракций, божеств, особых предметов и существ этой игры.\n"
            "Что не выписывать: обычные слова (например, cleric, sword, camp, battle, gun, goblin), "
            "даже если они встречаются в тексте; записи, которые уже есть в словаре, "
            "кроме исправлений пола и формы (см. ниже).\n"
            "Не выписывай элементы интерфейса игры: кнопки, меню, подсказки управления и системные надписи "
            "(например, Auto Mode, Backlog, Next, Hide Window, Skip, Save, Load, System, Options), "
            "в том числе с буквой кнопки геймпада впереди (A Next, Y Auto Mode) и склеенные при "
            "распознавании (AutoMode, BHide Window).\n"
            "Пол — только для персонажей: (ж) или (м) после перевода в записи словаря. "
            "Ставь его, если пол виден из этих строк (she/her, he/his) или ты точно знаешь "
            "этого персонажа игры; если не уверен — не пиши. В переводах строк пол не пиши никогда.\n"
            "Исправление пола: если у персонажа из словаря пола нет или он указан неверно, "
            "а по этим строкам пол понятен, — выпиши запись ещё раз с тем же переводом и верным полом.\n"
            "Исправление формы: если перевод в записи словаря не в начальной форме (имя или термин "
            f"в другом падеже или числе), выпиши запись ещё раз в начальной форме и допиши в конце «{_FORM_FIX_MARK}», "
            f"например: Geralt → Геральт {_FORM_FIX_MARK}. Меняй только окончание, написание имени не меняй; "
            f"пол, если он есть, пиши перед «{_FORM_FIX_MARK}».\n"
            "Манера речи: после записей словаря для каждого персонажа, который говорит в этих строках, "
            "напиши строку «Речь: <имя как в исходном тексте> — <3–6 слов>», как он говорит именно в этих "
            "строках (например: насмешливо, с прибаутками, на «ты»). Описывай только эти строки; без сюжета и "
            "событий. Сравни с тем, как персонаж говорил раньше: если в этих строках он говорит иначе "
            "(вежливее, грубее, официальнее, мягче), опиши новую манеру. Например, раньше «весело, с "
            "прибаутками», а здесь персонаж в ярости — пиши «зло, отрывисто».\n"
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

    def _switch_glossary_if_pair_changed(self, source_lang: str, target_lang: str) -> None:
        """
        Make the in-memory glossary the one for this game and pair.

        Caller holds _glossary_lock. Also runs after set_game, which resets
        the pair to None. The old glossary is not saved here: every change
        to it was saved when it happened.
        """
        pair = (source_lang, target_lang)
        if pair == self._glossary_pair:
            return
        if self._glossary:
            logger.info("GigaChat+ language pair changed, glossary switched")
        self._glossary.clear()
        self._rejected.clear()
        self._form_candidates.clear()
        self._speech_candidates.clear()
        self._reset_cleanup_state()
        self._glossary_pair = pair
        self._glossary_epoch += 1
        self._glossary_load_failed = False
        self._load_glossary()

    def _store_key(self) -> Optional[GlossaryKey]:
        """
        Key of the saved glossary, or None when it is not saved.

        Caller holds _glossary_lock. Without a game id there is nothing to
        key on: a name alone may change (a renamed shortcut, a localized
        title), and an empty one would merge all unknown games into one.
        """
        if self._glossary_store is None or not self._game_id or self._glossary_pair is None:
            return None
        source_lang, target_lang = self._glossary_pair
        return GlossaryKey(self.provider_type.value, self._game_id, source_lang, target_lang)

    def _load_glossary(self) -> None:
        """Caller holds _glossary_lock and has cleared the glossary."""
        key = self._store_key()
        if key is None:
            return
        try:
            entries = self._glossary_store.load_entries(key)
            rejected = self._glossary_store.load_rejected(key)
        except sqlite3.Error as e:
            # The glossary is a bonus: translate without it rather than fail.
            # Not saving protects the stored glossary (see __init__).
            logger.warning(
                f"GigaChat+ glossary not loaded, translating with an empty one "
                f"and not saving it until the game or language pair changes: {e}"
            )
            self._glossary_load_failed = True
            return
        for item in rejected:
            self._rejected[item.match_key] = item
        # The store returns the least recently used first, the same order
        # the in-memory glossary keeps. A rejected term never has an entry
        # (the store removes it in the same transaction); the check only
        # guards against a database edited by hand.
        for entry in entries:
            if entry.match_key not in self._rejected:
                self._glossary[entry.match_key] = entry
        # A glossary saved by a version with a larger limit is trimmed here.
        self._evict_overflow()
        self._cleanup_after_load = bool(self._glossary)
        if entries:
            logger.info(f"GigaChat+ glossary loaded: {len(self._glossary)} entries")

    def _save_glossary(self) -> None:
        """
        Write the whole in-memory glossary to the store.

        Caller holds _glossary_lock: the write is done under it so that two
        translations cannot save their snapshots out of order. A write is a
        few dozen rows, a few milliseconds. A failed write is logged and
        ignored: the translation itself has succeeded.
        """
        key = self._store_key()
        if key is None or self._glossary_load_failed:
            return
        try:
            self._glossary_store.replace_entries(key, self._game, list(self._glossary.values()))
        except sqlite3.Error as e:
            logger.warning(f"GigaChat+ glossary not saved: {e}")

    def _touch_used_entries(self, texts: List[str]) -> bool:
        """
        Move entries whose term occurs in this batch to the recent end.

        Caller holds _glossary_lock. A plain substring check: cheap, and a
        rare false hit only keeps an entry a little longer. Returns whether
        any entry was touched.
        """
        batch_text = _match_key(" ".join(texts))
        now = int(time.time())
        touched = False
        for key in list(self._glossary):
            if key in batch_text:
                self._glossary[key] = replace(self._glossary[key], last_used_at=now)
                self._glossary.move_to_end(key)
                touched = True
        return touched

    def _merge_glossary(
        self,
        entries: List[ProposedEntry],
        epoch: int,
        speech_notes: Tuple[Tuple[str, str], ...] = (),
        batch_text: str = "",
    ) -> None:
        """
        Add new entries; never overwrite an existing one, with two exceptions.

        The first translation of a name sticks: if the model renders it
        differently later, that is the inconsistency the glossary exists to
        prevent. Exceptions: the gender (_update_gender) and the form of the
        name, marked or confirmed by repeated answers (_consider_form).
        """
        if not entries and not speech_notes:
            return
        with self._glossary_lock:
            if epoch != self._glossary_epoch:
                logger.info("GigaChat+ glossary was reset during the request, new terms dropped")
                return
            added = 0
            # Existing entries whose gender or form was fixed.
            updated = 0
            dropped: List[str] = []
            # Proposed again although the user or the cleanup rejected them.
            rejected: List[str] = []
            # One answer is one vote for a form, even if it lists a term twice.
            voted: set = set()
            now = int(time.time())
            for entry in entries:
                # The same normalization as the "is it in the text" check:
                # "Lae'zel" and "Lae’zel" must be one entry, or the first
                # translation would not stick.
                key = _match_key(entry.term)
                if key in self._rejected:
                    rejected.append(entry.term)
                    continue
                existing = self._glossary.get(key)
                if existing is None:
                    # The limit is on new entries only: a fix of an existing
                    # one below does not take a place. A form-fix mark on a
                    # new entry means nothing: it is added as it is.
                    if added >= _MAX_NEW_ENTRIES_PER_ANSWER:
                        dropped.append(entry.term)
                        continue
                    self._glossary[key] = GlossaryEntry(key, entry.term, entry.translation, now)
                    added += 1
                    continue
                if self._consider_form(key, existing, entry.translation, entry.form_fix, voted):
                    updated += 1
                    continue
                if self._update_gender(key, existing, entry.translation):
                    updated += 1
            # After the entries: a character first named on this screen gets
            # its entry above and can take its first speech note right away.
            speech_voted: set = set()
            for name, note in speech_notes:
                key = self._speaker_key(name, batch_text)
                if key is not None and self._consider_speech(key, note, speech_voted):
                    updated += 1
            if rejected:
                logger.info(f"GigaChat+ glossary: rejected terms not added: {', '.join(rejected)}")
            if dropped:
                logger.info(
                    f"GigaChat+ glossary: answer proposed more than "
                    f"{_MAX_NEW_ENTRIES_PER_ANSWER} new entries, dropped: {', '.join(dropped)}"
                )
            self._evict_overflow()
            self._new_since_cleanup += added
            if added or updated:
                logger.info(
                    f"GigaChat+ glossary: +{added}, fixed {updated}, "
                    f"{len(self._glossary)} entries"
                )
                self._save_glossary()

    def _evict_overflow(self) -> None:
        """
        Drop the least recently used entries above the limit and log them.

        Caller holds _glossary_lock. The log is for the pilot: it shows
        whether real games hit the limit, and with the glossary kept across
        restarts an evicted name is gone for good.
        """
        evicted = []
        while len(self._glossary) > _MAX_GLOSSARY_ENTRIES:
            _, entry = self._glossary.popitem(last=False)
            evicted.append(entry.term)
        if evicted:
            logger.info(
                f"GigaChat+ glossary full ({_MAX_GLOSSARY_ENTRIES}), evicted: {', '.join(evicted)}"
            )

    def _speech_notes_for(self, texts: List[str]) -> List[Tuple[str, str]]:
        """
        Stored speech notes of the characters on this screen, for the prompt.

        Caller holds _glossary_lock. Only the characters whose name occurs in
        the batch: a note helps with the lines of its speaker, the others
        would only cost tokens.
        """
        batch_text = _match_key(" ".join(texts))
        return [
            (entry.term, entry.speech)
            for key, entry in self._glossary.items()
            if entry.speech and key in batch_text
        ]

    def _speaker_key(self, name: str, batch_text: str) -> Optional[str]:
        """
        The glossary key a "Речь:" line is about, or None.

        Caller holds _glossary_lock. The model names the character by the
        original ("Ukon") or by the translation ("Укон"); both are accepted.
        The character must be on this screen, as glossary terms must.
        """
        key = _match_key(name)
        if key not in self._glossary:
            key = None
            wanted = name.casefold()
            for candidate_key, entry in self._glossary.items():
                if _split_gender(entry.translation)[0].casefold() == wanted:
                    key = candidate_key
                    break
        if key is None or key not in batch_text:
            return None
        return key

    def _consider_speech(self, key: str, note: str, voted: set) -> bool:
        """
        Take a speech note for an entry if the rules allow; return whether
        the entry changed.

        Caller holds _glossary_lock. The first note is taken at once. Later a
        description that shares a word with the stored note is the same
        manner and breaks the run of different ones; a word-for-word copy
        of the stored note is an echo and changes nothing; one that shares none
        counts, and after _SPEECH_CHANGE_SCREENS such screens in a row the
        latest description replaces the note (see _SPEECH_PREFIX). One
        answer is one screen, even if it repeats a "Речь:" line.
        """
        if key in voted:
            return False
        voted.add(key)
        entry = self._glossary[key]
        if not entry.speech:
            logger.info(f"GigaChat+ glossary: speech of {entry.term} set: {note}")
            self._glossary[key] = replace(entry, speech=note)
            return True
        if note.casefold() == entry.speech.casefold():
            # An echo, not an observation: live runs showed the model copying
            # the stored note word for word for a character only mentioned
            # on the screen ("Ukon walks beside the palanquin"). Counted as
            # "same manner", it reset the run, and narration between two
            # lines of a changed character blocked the replacement.
            return False
        if _speech_words(note) & _speech_words(entry.speech):
            self._speech_candidates.pop(key, None)
            return False
        _, screens = self._speech_candidates.get(key, ("", 0))
        screens += 1
        if screens < _SPEECH_CHANGE_SCREENS:
            self._speech_candidates[key] = (note, screens)
            logger.info(
                f"GigaChat+ glossary: speech of {entry.term} differs: {note} "
                f"({screens} of {_SPEECH_CHANGE_SCREENS})"
            )
            return False
        self._speech_candidates.pop(key, None)
        logger.info(f"GigaChat+ glossary: speech of {entry.term} changed {entry.speech} -> {note}")
        self._glossary[key] = replace(entry, speech=note)
        return True

    def _consider_form(
        self,
        key: str,
        existing: GlossaryEntry,
        translation: str,
        marked: bool,
        voted: set,
    ) -> bool:
        """
        Replace the form of the name ("Уконом" -> "Укон") if allowed; return
        whether the entry changed. The gender is kept if the fix has none.

        Caller holds _glossary_lock. Only an ending change is ever accepted
        (_is_ending_change). With the form-fix mark it is applied at once;
        without it, after _FORM_FIX_VOTES answers proposed the same form (a
        different proposal starts the count again). Live runs showed the
        model storing a name in the case of the sentence, and the wrong
        entry then broke the lines on screen ("Уконом поднимает веер");
        decision of the user, 08.11.2026. The same name with another gender
        is left to _update_gender.
        """
        old_name, old_gender = _split_gender(existing.translation)
        new_name, new_gender = _split_gender(translation)
        if old_name.casefold() == new_name.casefold():
            return False
        if not _is_ending_change(old_name, new_name):
            if marked:
                logger.info(
                    f"GigaChat+ glossary: form fix of {existing.term} rejected, "
                    f"not just an ending: {old_name} -> {new_name}"
                )
            return False
        if not marked:
            if key in voted:
                return False
            voted.add(key)
            candidate, votes = self._form_candidates.get(key, ("", 0))
            votes = votes + 1 if candidate.casefold() == new_name.casefold() else 1
            if votes < _FORM_FIX_VOTES:
                self._form_candidates[key] = (new_name, votes)
                logger.info(
                    f"GigaChat+ glossary: form of {existing.term} proposed {old_name} -> {new_name} "
                    f"({votes} of {_FORM_FIX_VOTES})"
                )
                return False
        self._form_candidates.pop(key, None)
        gender = new_gender or old_gender
        fixed = f"{new_name} {gender}" if gender else new_name
        logger.info(f"GigaChat+ glossary: form of {existing.term} fixed {old_name} -> {new_name}")
        self._glossary[key] = replace(existing, translation=fixed)
        return True

    def _update_gender(
        self, key: str, existing: GlossaryEntry, translation: str
    ) -> bool:
        """
        One of the two exceptions to "never overwrite" (the other is the
        form, _consider_form): the gender of a character, added or
        corrected, with the same translation of the name.

        Caller holds _glossary_lock. The gender often becomes clear only on
        a later screen (the first line has no she/he), and a first guess
        may be wrong; the prompt asks the model to re-list the entry then
        (decision of the user, 08.11.2026). A different translation of the
        name is still ignored: the first translation sticks. An answer
        without a gender never removes one.
        """
        old_name, old_gender = _split_gender(existing.translation)
        new_name, new_gender = _split_gender(translation)
        if not new_gender or new_gender == old_gender:
            return False
        if old_name.casefold() != new_name.casefold():
            return False
        if old_gender:
            logger.info(f"GigaChat+ glossary: gender of {existing.term} changed {old_gender} -> {new_gender}")
        self._glossary[key] = replace(existing, translation=f"{old_name} {new_gender}")
        return True

    # -- glossary cleanup (see _GLOSSARY_CLEANUP_ENABLED) --
    #
    # The order of events: a translation ends -> if a cleanup is due, a timer
    # is (re)started for _CLEANUP_IDLE_SECONDS -> every new translation
    # restarts it -> when it fires with no translation running and none for
    # that long, the cleanup runs in the timer's thread. Its request goes
    # through the same one-at-a-time client lock as translations, so a
    # hotkey pressed during the cleanup waits for it (1-2 s in live runs).

    def _reset_cleanup_state(self) -> None:
        """A new glossary (game or pair changed): start counting anew. Caller holds _glossary_lock."""
        self._new_since_cleanup = 0
        self._cleanup_after_load = False
        self._last_cleanup_at = None

    def _translation_finished(self) -> None:
        """Count a translation as ended and restart the cleanup timer if a cleanup is due."""
        with self._glossary_lock:
            self._translations_running -= 1
            self._last_translation_at = self._clock()
            if not self._cleanup_due():
                return
            if self._cleanup_timer is not None:
                self._cleanup_timer.cancel()
            self._cleanup_timer = self._timer_factory(_CLEANUP_IDLE_SECONDS, self._run_cleanup_if_idle)

    def _cleanup_due(self) -> bool:
        """Whether the glossary should be cleaned now. Caller holds _glossary_lock."""
        if not _GLOSSARY_CLEANUP_ENABLED or not self._glossary:
            return False
        if self._last_cleanup_at is not None:
            if self._clock() - self._last_cleanup_at < _CLEANUP_MIN_INTERVAL_SECONDS:
                return False
        if self._cleanup_after_load:
            return True
        if len(self._glossary) > _MAX_GLOSSARY_ENTRIES * _CLEANUP_FULL_SHARE:
            threshold = _CLEANUP_AFTER_NEW_ENTRIES_FULL
        else:
            threshold = _CLEANUP_AFTER_NEW_ENTRIES
        return self._new_since_cleanup >= threshold

    def _run_cleanup_if_idle(self) -> None:
        """
        The timer callback: clean the glossary if translations are idle.

        Runs in the timer's thread. Any failure is only logged: the cleanup
        is a bonus, and the user must not see its errors (a used-up token
        pool is reported by the next translation anyway).
        """
        with self._glossary_lock:
            self._cleanup_timer = None
            if self._cleanup_running or self._translations_running:
                return
            if self._clock() - self._last_translation_at < _CLEANUP_IDLE_SECONDS:
                return
            if not self._cleanup_due():
                return
            self._cleanup_running = True
            # Counted from the start: a failed cleanup also waits the full
            # interval instead of being retried at once.
            self._last_cleanup_at = self._clock()
            self._cleanup_after_load = False
            # Entries that arrive during the cleanup count towards the next one.
            self._new_since_cleanup = 0
            snapshot = list(self._glossary.values())
            game = self._game
            epoch = self._glossary_epoch
        try:
            self._clean_glossary(snapshot, game, epoch)
        except Exception as e:  # noqa: BLE001 — any failure only skips this cleanup
            logger.warning(f"GigaChat+ glossary cleanup failed: {type(e).__name__}: {e}")
        finally:
            with self._glossary_lock:
                self._cleanup_running = False

    def _clean_glossary(self, snapshot: List[GlossaryEntry], game: str, epoch: int) -> None:
        """Ask the model which entries are interface labels and reject them."""
        logger.info(f"GigaChat+ glossary cleanup: checking {len(snapshot)} entries")
        # Not a screen: a newer screen must not drop this request as stale.
        # Set explicitly, because a thread may inherit the context it was
        # started from (an option of newer Python), and the timer is started
        # while a screen is being translated.
        screen_token = _current_screen.set(None)
        try:
            answer = self._chat(self._cleanup_messages(game, snapshot))
        finally:
            _current_screen.reset(screen_token)
        if not answer.usable:
            logger.warning(f"GigaChat+ glossary cleanup: answer finished with '{answer.finish_reason}'")
            return
        numbers = _parse_cleanup_answer(answer.content, len(snapshot))
        if numbers is None:
            logger.warning(f"GigaChat+ glossary cleanup: answer not understood: {answer.content[:200]!r}")
            return
        if len(numbers) > len(snapshot) * _CLEANUP_MAX_DELETE_SHARE:
            logger.warning(
                f"GigaChat+ glossary cleanup: answer names {len(numbers)} of {len(snapshot)} entries, "
                "not trusted"
            )
            return
        named = [snapshot[number - 1] for number in numbers]
        protected = [entry.term for entry in named if _is_protected_from_cleanup(entry)]
        if protected:
            logger.info(f"GigaChat+ glossary cleanup: kept characters: {', '.join(protected)}")
        labels = [entry for entry in named if not _is_protected_from_cleanup(entry)]
        if not labels:
            logger.info("GigaChat+ glossary cleanup: no interface labels found")
            return
        self._reject_by_cleanup(labels, epoch)

    def _cleanup_messages(self, game: str, snapshot: List[GlossaryEntry]) -> List[dict]:
        """
        The cleanup request: the instruction and the numbered glossary.

        One entry per line, so a term can never break the numbering: terms
        come from single answer lines and hold no line breaks.
        """
        if game:
            system = f"Ты помогаешь вести словарь имён и терминов для перевода игры «{game}»."
        else:
            system = "Ты помогаешь вести словарь имён и терминов для перевода игры."
        numbered = "\n".join(
            f"{number}. {entry.term} → {entry.translation}"
            for number, entry in enumerate(snapshot, start=1)
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{_CLEANUP_INSTRUCTION}\n\nСловарь:\n{numbered}"},
        ]

    def _reject_by_cleanup(self, labels: List[GlossaryEntry], epoch: int) -> None:
        """
        Move the labels found by the cleanup to the black list.

        Skipped as a whole if the glossary was reset, edited by the user or
        switched during the request (the epoch changed): the numbers belong
        to the old glossary. The cleanup itself does not bump the epoch: it
        only removes entries, so the new terms of a translation that ended
        meanwhile stay valid, and a removed label proposed again is stopped
        by the black list. An entry already gone is skipped. The store is
        written first; if that fails, memory is left as it was.
        """
        with self._glossary_lock:
            if epoch != self._glossary_epoch:
                logger.info("GigaChat+ glossary cleanup: glossary changed during the request, result dropped")
                return
            now = int(time.time())
            rejected = [
                RejectedTerm(entry.match_key, entry.term, self._glossary[entry.match_key].translation,
                             now, REJECTED_BY_CLEANUP)
                for entry in labels
                if entry.match_key in self._glossary
            ]
            if not rejected:
                return
            key = self._store_key()
            if key is not None and not self._glossary_load_failed:
                self._glossary_store.reject_entries(key, self._game, rejected)
            for item in rejected:
                self._glossary.pop(item.match_key, None)
                self._form_candidates.pop(item.match_key, None)
                self._speech_candidates.pop(item.match_key, None)
                self._rejected[item.match_key] = item
        logger.info(
            f"GigaChat+ glossary cleanup: rejected {len(rejected)}: "
            f"{', '.join(item.term for item in rejected)}"
        )
