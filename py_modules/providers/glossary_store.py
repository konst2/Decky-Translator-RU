# providers/glossary_store.py
# Хранилище словарей имён и терминов для ИИ-перевода с контекстом.
#
# Словарь принадлежит одному методу перевода, одной игре и одной языковой
# паре. На диске он переживает перезапуск плагина, поэтому имена и термины
# игры остаются одинаковыми не только в пределах одной сессии.
#
# Хранилище не знает о конкретном методе: ни о промптах, ни о том, как
# нормализуется термин. Имя метода и ключ поиска каждой записи (match_key)
# передаёт вызывающий код.
#
# Отдельный файл, а не таблица в базе кэша переводов: кэш чистят целиком
# (с VACUUM), а словарь ценнее кэша и не должен пропадать вместе с ним.

import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GlossaryKey:
    """Чей словарь: один метод, одна игра, одна языковая пара."""

    method: str
    game_id: str
    source_lang: str
    target_lang: str


@dataclass(frozen=True)
class GlossaryEntry:
    """
    Один термин и его перевод.

    match_key — нормализованная вызывающим кодом форма термина (регистр,
    апострофы); по ней запись узнаётся внутри словаря. last_used_at задаёт
    порядок давности: когда словарь переполнен, вызывающий код вытесняет
    самую давно использованную запись.
    """

    match_key: str
    term: str
    translation: str
    last_used_at: int
    # Манера речи персонажа («грубо, коротко, на «ты»»), "" — неизвестна.
    # Последним полем со значением по умолчанию: записи без манеры создаются
    # как раньше, четырьмя полями.
    speech: str = ""


# Кто отбраковал термин. Хранится, чтобы в окне словаря было видно, что
# удалила чистка: если она ошиблась и выкинула имя, это надо заметить.
REJECTED_BY_USER = "user"
REJECTED_BY_CLEANUP = "cleanup"


@dataclass(frozen=True)
class RejectedTerm:
    """
    Термин из чёрного списка словаря: модель больше не может его добавить.

    Чаще всего это элемент интерфейса игры («lb Back», «Auto Mode»), который
    модель выписывает снова и снова. translation — каким был перевод в
    словаре, чтобы в окне было понятно, что запрещено. reason — кто запретил
    (REJECTED_BY_USER или REJECTED_BY_CLEANUP).
    """

    match_key: str
    term: str
    translation: str
    rejected_at: int
    reason: str


class GlossaryStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        # Перевод идёт в рабочем потоке, RPC настроек — в цикле событий.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # AUTOINCREMENT: без него SQLite может выдать новому словарю номер
        # только что удалённого (если у того был наибольший номер), и записи,
        # которые почему-то пережили свой словарь, прилипнут к новому.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glossaries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                method       TEXT NOT NULL,
                game_id      TEXT NOT NULL,
                game_name    TEXT,
                source_lang  TEXT NOT NULL,
                target_lang  TEXT NOT NULL,
                UNIQUE (method, game_id, source_lang, target_lang)
            )
            """
        )
        # Без REFERENCES: SQLite проверяет внешние ключи, только если на
        # каждом соединении включён PRAGMA foreign_keys, а объявленная, но не
        # проверяемая связь вводит читателя в заблуждение. Вместо каскада
        # записи без словаря удаляются явно (_delete_orphan_entries).
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glossary_entries (
                glossary_id  INTEGER NOT NULL,
                match_key    TEXT NOT NULL,
                term         TEXT NOT NULL,
                translation  TEXT NOT NULL,
                last_used_at INTEGER,
                speech       TEXT,
                PRIMARY KEY (glossary_id, match_key)
            )
            """
        )
        # Чёрный список словаря. Привязан к той же строке glossaries, что и
        # записи: у каждой игры и пары языков свой список, и сброс словаря
        # удаляет его вместе с записями. Связь без REFERENCES — по той же
        # причине, что у записей; подчистка — _delete_orphan_entries.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glossary_rejected (
                glossary_id  INTEGER NOT NULL,
                match_key    TEXT NOT NULL,
                term         TEXT NOT NULL,
                translation  TEXT NOT NULL,
                rejected_at  INTEGER NOT NULL,
                reason       TEXT NOT NULL,
                PRIMARY KEY (glossary_id, match_key)
            )
            """
        )
        self._add_missing_columns()
        self._conn.commit()

    def load_entries(self, key: GlossaryKey) -> List[GlossaryEntry]:
        """Записи словаря, самые давно использованные первыми; [] если словаря нет."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT e.match_key, e.term, e.translation, e.last_used_at,
                       COALESCE(e.speech, '')
                FROM glossary_entries e
                JOIN glossaries g ON g.id = e.glossary_id
                WHERE g.method = ? AND g.game_id = ?
                  AND g.source_lang = ? AND g.target_lang = ?
                ORDER BY e.last_used_at ASC, e.rowid ASC
                """,
                self._key_params(key),
            ).fetchall()
        return [GlossaryEntry(*row) for row in rows]

    def replace_entries(
        self, key: GlossaryKey, game_name: str, entries: List[GlossaryEntry]
    ) -> None:
        """
        Сделать сохранённый словарь ровно `entries`, одной транзакцией.

        Полная замена вместо построчного слияния: в словаре несколько
        десятков записей, переписать их дёшево, и вызывающему коду не нужно
        следить, что добавилось, изменилось или вытеснилось с прошлого раза.

        Пустой список удаляет записи, а строку словаря — только если и чёрный
        список пуст: строка создаётся, только когда есть что хранить, поэтому
        пустые словари не копятся, а запреты не теряются оттого, что из
        словаря ушла последняя запись.
        Название игры обновляется при каждом сохранении (Steam может
        переименовать игру).
        """
        if not entries:
            with self._lock, self._conn:
                self._conn.execute(
                    f"DELETE FROM glossary_entries WHERE glossary_id = ({self._GLOSSARY_ID_BY_KEY})",
                    self._key_params(key),
                )
                self._delete_glossary_if_empty(key)
            return

        rows = [
            (entry.match_key, entry.term, entry.translation, entry.last_used_at, entry.speech)
            for entry in entries
        ]
        with self._lock, self._conn:
            glossary_id = self._upsert_glossary(key, game_name)
            self._conn.execute(
                "DELETE FROM glossary_entries WHERE glossary_id = ?", [glossary_id]
            )
            self._conn.executemany(
                """
                INSERT INTO glossary_entries
                    (glossary_id, match_key, term, translation, last_used_at, speech)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(glossary_id, *row) for row in rows],
            )

    def delete_entry(self, key: GlossaryKey, match_key: str) -> bool:
        """
        Удалить одну запись; False, если такой записи не было.

        Если это была последняя запись и чёрный список пуст, удаляется и
        строка словаря — как при сохранении пустого списка: пустые словари
        не копятся, и «сбросить всё» по пустому словарю не считается удалением.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"""
                DELETE FROM glossary_entries
                WHERE match_key = ? AND glossary_id = ({self._GLOSSARY_ID_BY_KEY})
                """,
                [match_key, *self._key_params(key)],
            )
            deleted = cursor.rowcount > 0
            self._delete_glossary_if_empty(key)
        return deleted

    # -- чёрный список --

    def load_rejected(self, key: GlossaryKey) -> List[RejectedTerm]:
        """Чёрный список словаря, недавно отбракованные первыми; [] если словаря нет."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.match_key, r.term, r.translation, r.rejected_at, r.reason
                FROM glossary_rejected r
                JOIN glossaries g ON g.id = r.glossary_id
                WHERE g.method = ? AND g.game_id = ?
                  AND g.source_lang = ? AND g.target_lang = ?
                ORDER BY r.rejected_at DESC, r.rowid DESC
                """,
                self._key_params(key),
            ).fetchall()
        return [RejectedTerm(*row) for row in rows]

    def reject_entries(
        self, key: GlossaryKey, game_name: Optional[str], rejected: List[RejectedTerm]
    ) -> None:
        """
        Внести термины в чёрный список и удалить их записи из словаря —
        одной транзакцией.

        Одной, потому что по отдельности сбой между шагами оставил бы либо
        запись, которую модель уже не сможет исправить, либо запрет без
        удаления. Повторный запрет того же термина обновляет его строку.
        """
        if not rejected:
            return
        with self._lock, self._conn:
            glossary_id = self._upsert_glossary(key, game_name)
            for item in rejected:
                self._conn.execute(
                    "DELETE FROM glossary_entries WHERE glossary_id = ? AND match_key = ?",
                    [glossary_id, item.match_key],
                )
                self._conn.execute(
                    """
                    INSERT INTO glossary_rejected
                        (glossary_id, match_key, term, translation, rejected_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (glossary_id, match_key) DO UPDATE SET
                        term = excluded.term,
                        translation = excluded.translation,
                        rejected_at = excluded.rejected_at,
                        reason = excluded.reason
                    """,
                    [glossary_id, item.match_key, item.term, item.translation,
                     item.rejected_at, item.reason],
                )

    def delete_rejected(self, key: GlossaryKey, match_key: str) -> bool:
        """
        Убрать термин из чёрного списка (модель снова может его добавить);
        False, если его там не было. Опустевший словарь удаляется, как в
        delete_entry.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"""
                DELETE FROM glossary_rejected
                WHERE match_key = ? AND glossary_id = ({self._GLOSSARY_ID_BY_KEY})
                """,
                [match_key, *self._key_params(key)],
            )
            deleted = cursor.rowcount > 0
            self._delete_glossary_if_empty(key)
        return deleted

    def delete_glossary(self, key: GlossaryKey) -> bool:
        """
        Удалить словарь со всеми записями и чёрным списком; False, если
        словаря не было.

        По ключу удаляется только строка словаря. Его записи убирает
        подчистка в той же транзакции — вместе с записями, которые могла
        оставить какая-нибудь прошлая ошибка.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM glossaries
                WHERE method = ? AND game_id = ?
                  AND source_lang = ? AND target_lang = ?
                """,
                self._key_params(key),
            )
            self._delete_orphan_entries()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info(f"Glossary deleted: {key.method}, game {key.game_id}")
        return deleted

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- внутреннее (вызывающий держит _lock и открытую транзакцию) --

    def _add_missing_columns(self) -> None:
        """
        Миграция базы, созданной прежней версией плагина.

        CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, поэтому
        колонку, появившуюся позже, нужно добавить самим. Старые записи
        получают NULL, при чтении он превращается в "".
        """
        columns = [row[1] for row in self._conn.execute("PRAGMA table_info(glossary_entries)")]
        if "speech" not in columns:
            self._conn.execute("ALTER TABLE glossary_entries ADD COLUMN speech TEXT")
            logger.info("Glossary store migrated: speech column added")

    # id словаря по ключу; параметры — _key_params. Подзапрос вернёт NULL,
    # если словаря нет, и тогда сравнение с ним ничего не удалит.
    _GLOSSARY_ID_BY_KEY = (
        "SELECT id FROM glossaries "
        "WHERE method = ? AND game_id = ? AND source_lang = ? AND target_lang = ?"
    )

    @staticmethod
    def _key_params(key: GlossaryKey) -> list:
        return [key.method, key.game_id, key.source_lang, key.target_lang]

    def _delete_glossary_if_empty(self, key: GlossaryKey) -> None:
        """Удалить строку словаря, если в нём нет ни записей, ни запретов."""
        self._conn.execute(
            """
            DELETE FROM glossaries
            WHERE method = ? AND game_id = ?
              AND source_lang = ? AND target_lang = ?
              AND NOT EXISTS (
                  SELECT 1 FROM glossary_entries WHERE glossary_id = glossaries.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM glossary_rejected WHERE glossary_id = glossaries.id
              )
            """,
            self._key_params(key),
        )

    def _upsert_glossary(self, key: GlossaryKey, game_name: Optional[str]) -> int:
        """
        Создать строку словаря, если её нет, обновить название игры, вернуть id.

        game_name=None — название не трогать: так вызывают, когда меняют
        словарь не запущенной сейчас игры (из окна словаря) и названия не знают.
        """
        # Два запроса вместо INSERT ... RETURNING: RETURNING требует
        # SQLite 3.35, а такая свежая версия в Python на Деке не гарантирована.
        # Сам upsert (3.24) уже используется в кэше переводов.
        self._conn.execute(
            """
            INSERT INTO glossaries (method, game_id, game_name, source_lang, target_lang)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (method, game_id, source_lang, target_lang)
            DO UPDATE SET game_name = COALESCE(excluded.game_name, game_name)
            """,
            [key.method, key.game_id, game_name, key.source_lang, key.target_lang],
        )
        row = self._conn.execute(
            """
            SELECT id FROM glossaries
            WHERE method = ? AND game_id = ? AND source_lang = ? AND target_lang = ?
            """,
            self._key_params(key),
        ).fetchone()
        return row[0]

    def _delete_orphan_entries(self) -> None:
        """
        Удалить записи и запреты, чьего словаря больше нет.

        Заменяет каскад (см. описание таблицы). Ловушка NOT IN с NULL (если
        подзапрос вернёт NULL, NOT IN не совпадёт ни с чем и ничего не
        удалит) здесь не грозит: glossaries.id — первичный ключ, NULL там
        не бывает.
        """
        self._conn.execute(
            "DELETE FROM glossary_entries "
            "WHERE glossary_id NOT IN (SELECT id FROM glossaries)"
        )
        self._conn.execute(
            "DELETE FROM glossary_rejected "
            "WHERE glossary_id NOT IN (SELECT id FROM glossaries)"
        )
