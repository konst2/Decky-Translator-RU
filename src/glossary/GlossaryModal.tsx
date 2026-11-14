// src/glossary/GlossaryModal.tsx - окно словаря ИИ-перевода: просмотр, удаление и запрет записи,
// отбракованные термины, сброс
//
// Словарь имён и терминов, который ИИ-метод перевода с контекстом ведёт для каждой
// игры и пары языков и хранит на бэкенде между перезапусками. Окно показывает словарь
// игры, запущенной сейчас. Запись можно удалить (неудачный перевод: модель предложит
// её заново) или запретить (не имя вовсе, а, например, кнопка интерфейса: модель её
// больше не добавит). Запрещённое — и то, что убрала автоматическая чистка словаря, —
// показано отдельным разделом «Отбракованные», откуда термин можно вернуть.
//
// Пока такой метод один — GigaChat+ (пилот): RPC get_glossary / delete_glossary_entry /
// forbid_glossary_entry / allow_rejected_term / reset_glossary на бэкенде ходят в его
// провайдер. Само окно о методе не знает.

import { showModal, ModalRoot, DialogButton, Focusable, Router } from "@decky/ui";
import { VFC, ReactNode, useState, useEffect } from "react";
import { call, toaster } from "@decky/api";
import { HiTrash, HiNoSymbol, HiArrowUturnLeft, HiXCircle, HiExclamationTriangle } from "react-icons/hi2";

type Gender = "м" | "ж" | "";

// Запись словаря в том виде, в каком её отдаёт бэкенд (get_glossary).
// Имя, пол и манера приходят раздельно: формат знака пола («(ж)») знает только
// бэкенд.
interface GlossaryEntryView {
    matchKey: string;
    term: string;
    name: string;
    gender: Gender;
    speech: string;
}

// Термин из чёрного списка. reason — кто запретил: игрок кнопкой «Запретить»
// или автоматическая чистка словаря. Чистку видно отдельно: если она ошиблась
// и выкинула имя, игрок должен это заметить.
interface RejectedTermView {
    matchKey: string;
    term: string;
    name: string;
    gender: Gender;
    reason: "user" | "cleanup";
}

const genderLabel = (gender: Gender) =>
    gender === "м" ? "муж." : gender === "ж" ? "жен." : "";

// Языки словаря: коды уходят на бэкенд, подписи показываются в окне.
export interface GlossaryLanguages {
    sourceLang: string;
    targetLang: string;
    sourceLabel: string;
    targetLabel: string;
}

const ICON_BUTTON_STYLE = {
    minWidth: "32px", width: "32px", height: "32px", padding: "0", flexShrink: 0,
    display: "flex", alignItems: "center", justifyContent: "center",
} as const;

const ROW_STYLE = {
    display: "flex", gap: "10px", alignItems: "center", padding: "6px 10px",
    background: "rgba(255,255,255,0.04)", borderRadius: "4px", scrollMargin: "8px",
} as const;

const SMALL_BADGE_STYLE = {
    fontSize: "10px", color: "#b8bcbf", border: "1px solid rgba(255,255,255,0.2)",
    borderRadius: "3px", padding: "0 4px",
} as const;

const HINT_STYLE = { fontSize: "11px", color: "#8b929a" } as const;

// «term → имя [пол]» — общая строка для записи словаря и отбракованного термина.
const TermLine: VFC<{ term: string; name: string; gender: Gender; dimmed?: boolean; badge?: ReactNode }> = (
    { term, name, gender, dimmed, badge }
) => (
    <div style={{ fontSize: "13px", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
        <span style={{ color: dimmed ? "#9aa0a6" : "#dcdedf" }}>{term}</span>
        <span style={{ color: "rgba(255,255,255,0.35)" }}>→</span>
        <span style={{ color: dimmed ? "#8b929a" : "#6fcf97" }}>{name}</span>
        {gender && <span style={SMALL_BADGE_STYLE}>{genderLabel(gender)}</span>}
        {badge}
    </div>
);

const EntryRow: VFC<{
    entry: GlossaryEntryView;
    onDelete: () => void;
    onForbid: () => void;
}> = ({ entry, onDelete, onForbid }) => (
    <Focusable focusWithinClassName="dt-rowfocus" style={ROW_STYLE}>
        <div style={{ flex: "1 1 auto", minWidth: 0, display: "flex", flexDirection: "column", gap: "2px" }}>
            <TermLine term={entry.term} name={entry.name} gender={entry.gender} />
            {entry.speech && (
                <div style={{ ...HINT_STYLE, fontStyle: "italic" }}>речь: {entry.speech}</div>
            )}
        </div>
        <DialogButton onClick={onDelete} style={ICON_BUTTON_STYLE}>
            <HiTrash />
        </DialogButton>
        <DialogButton onClick={onForbid} style={ICON_BUTTON_STYLE}>
            <HiNoSymbol />
        </DialogButton>
    </Focusable>
);

const RejectedRow: VFC<{ item: RejectedTermView; onAllow: () => void }> = ({ item, onAllow }) => (
    <Focusable focusWithinClassName="dt-rowfocus" style={ROW_STYLE}>
        <div style={{ flex: "1 1 auto", minWidth: 0 }}>
            <TermLine
                term={item.term}
                name={item.name}
                gender={item.gender}
                dimmed
                badge={<span style={SMALL_BADGE_STYLE}>{item.reason === "cleanup" ? "чистка" : "вы"}</span>}
            />
        </div>
        <DialogButton onClick={onAllow} style={ICON_BUTTON_STYLE}>
            <HiArrowUturnLeft />
        </DialogButton>
    </Focusable>
);

// Окно словаря для игры, запущенной сейчас. Игру и языки окно получает
// при открытии, а не берёт из последнего перевода: открытое в новой игре до первого
// перевода, оно должно показать словарь этой игры, а не предыдущей.
const GlossaryModal: VFC<GlossaryLanguages & {
    closeModal?: () => void;
    gameId: string;
    gameName: string;
}> = ({ closeModal, gameId, gameName, sourceLang, targetLang, sourceLabel, targetLabel }) => {
    const [entries, setEntries] = useState<GlossaryEntryView[] | null>(null);
    const [rejected, setRejected] = useState<RejectedTermView[]>([]);
    // Ёмкость и порог «почти полон» приходят с бэкенда, а не задаются здесь числом:
    // иначе при смене предела словаря окно показывало бы неправду. 0 — ещё не
    // загружено (тогда счётчик не показывается).
    const [capacity, setCapacity] = useState(0);
    const [nearlyFullAbove, setNearlyFullAbove] = useState(0);
    const [loadError, setLoadError] = useState("");
    const [confirmReset, setConfirmReset] = useState(false);

    // Загрузить словарь с бэкенда. Зовётся при открытии и после правки, которая
    // не нашла запись: пока окно открыто, переводы и чистка словаря продолжают
    // его менять, и тогда показанное окном устарело.
    const loadGlossary = () => {
        if (!gameId) {
            setEntries([]);
            return;
        }
        call<[string, string, string], any>('get_glossary', gameId, sourceLang, targetLang)
            .then((result) => {
                if (result?.success) {
                    setEntries(result.entries || []);
                    setRejected(result.rejected || []);
                    setCapacity(result.capacity || 0);
                    setNearlyFullAbove(result.nearlyFullAbove || 0);
                } else {
                    setLoadError(result?.error || "неизвестная ошибка");
                    setEntries([]);
                }
            })
            .catch((e) => {
                setLoadError(String(e));
                setEntries([]);
            });
    };

    useEffect(loadGlossary, [gameId, sourceLang, targetLang]);

    // Бэкенд отдаёт записи по давности использования (так их вытесняет словарь),
    // а человеку проще найти имя по алфавиту. Отбракованные — как пришли: свежие
    // первыми, чтобы сразу было видно, что убрала последняя чистка.
    const sorted = [...(entries || [])].sort((a, b) => a.term.localeCompare(b.term));
    const count = entries?.length ?? 0;
    const nearlyFull = capacity > 0 && count > nearlyFullAbove;

    const showError = (action: string, error: unknown) => {
        toaster.toast({ title: "Словарь игры", body: `Не удалось ${action}: ${error}`, duration: 4000 });
    };

    // Вызов RPC правки словаря: true — запись изменена, окно может поправить
    // свои списки само. false — ошибка (показан тост) или записи на бэкенде уже
    // нет: её вытеснили или убрала чистка, пока окно было открыто. Тогда окно
    // перечитывает словарь, а не показывает то, чего не было: «запрещённую»
    // запись, которую модель на самом деле добавит снова.
    // doneField — поле ответа, которое говорит, что запись изменена
    // (deleted / forbidden / allowed); success означает лишь «ошибки не было».
    const callEdit = async (
        method: string, action: string, matchKey: string, doneField: string
    ): Promise<boolean> => {
        try {
            const result = await call<[string, string, string, string], any>(
                method, gameId, sourceLang, targetLang, matchKey
            );
            if (!result?.success) {
                showError(action, result?.error || "неизвестная ошибка");
                return false;
            }
            if (!result[doneField]) {
                loadGlossary();
                return false;
            }
            return true;
        } catch (e) {
            showError(action, e);
            return false;
        }
    };

    const dropEntry = (matchKey: string) =>
        setEntries(prev => prev ? prev.filter(x => x.matchKey !== matchKey) : prev);

    const removeEntry = async (entry: GlossaryEntryView) => {
        if (await callEdit('delete_glossary_entry', "удалить запись", entry.matchKey, "deleted")) {
            dropEntry(entry.matchKey);
        }
    };

    const forbidEntry = async (entry: GlossaryEntryView) => {
        if (!(await callEdit('forbid_glossary_entry', "запретить запись", entry.matchKey, "forbidden"))) {
            return;
        }
        dropEntry(entry.matchKey);
        const item: RejectedTermView = {
            matchKey: entry.matchKey, term: entry.term, name: entry.name, gender: entry.gender, reason: "user",
        };
        setRejected(prev => [item, ...prev.filter(x => x.matchKey !== entry.matchKey)]);
    };

    const allowTerm = async (item: RejectedTermView) => {
        if (await callEdit('allow_rejected_term', "вернуть термин", item.matchKey, "allowed")) {
            setRejected(prev => prev.filter(x => x.matchKey !== item.matchKey));
        }
    };

    // Как «Очистить всё» в окне кэша: первое нажатие взводит кнопку, второе сбрасывает.
    // Сбрасывается и чёрный список: на бэкенде он часть словаря.
    const handleReset = async () => {
        if (!confirmReset) {
            setConfirmReset(true);
            return;
        }
        setConfirmReset(false);
        try {
            const result = await call<[string, string, string], any>(
                'reset_glossary', gameId, sourceLang, targetLang
            );
            if (!result?.success) {
                showError("сбросить словарь", result?.error || "неизвестная ошибка");
                return;
            }
            setEntries([]);
            setRejected([]);
        } catch (e) {
            showError("сбросить словарь", e);
        }
    };

    const emptyText = () => {
        if (!gameId) return "Игра не запущена. Откройте это окно во время игры — в нём будет словарь этой игры.";
        if (loadError) return `Не удалось загрузить словарь: ${loadError}`;
        return "Словарь пока пуст. Он пополняется, пока вы переводите экраны в этой игре.";
    };

    const hasAnything = sorted.length > 0 || rejected.length > 0;

    return (
        <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
            <Focusable style={{ display: "flex", flexDirection: "column", maxHeight: "70vh", boxSizing: "border-box", minWidth: "540px" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "4px", flexShrink: 0 }}>
                    <div style={{ fontSize: "16px", fontWeight: "bold" }}>
                        Словарь игры{gameName && <> «{gameName}»</>}
                    </div>
                    {hasAnything && (
                        <DialogButton
                            onClick={handleReset}
                            style={{ width: "auto", minWidth: "auto", height: "32px", padding: "0 12px", whiteSpace: "nowrap", display: "flex", alignItems: "center", justifyContent: "center", gap: "6px", fontSize: "13px", color: confirmReset ? "#ff4d4d" : undefined }}
                        >
                            {confirmReset ? <HiExclamationTriangle style={{ fontSize: "16px" }} /> : <HiXCircle style={{ fontSize: "16px" }} />}
                            {confirmReset ? "Вы уверены?" : "Сбросить всё"}
                        </DialogButton>
                    )}
                </div>
                {gameId && (
                    // Словарь хранится отдельно для каждой пары: после смены языка в
                    // настройках здесь другой словарь, а прежний не пропал.
                    <div style={{ ...HINT_STYLE, marginBottom: "4px", flexShrink: 0 }}>
                        {sourceLabel} → {targetLabel}. У каждой пары языков свой словарь.
                    </div>
                )}
                {capacity > 0 && (
                    <div style={{ fontSize: "12px", color: nearlyFull ? "#e8b44c" : "#b8bcbf", marginBottom: "4px", flexShrink: 0 }}>
                        Записей: {count} из {capacity}
                        {nearlyFull && <> — словарь почти заполнен: записи, которые давно не встречались, будут вытесняться</>}
                    </div>
                )}
                {sorted.length > 0 && (
                    // На Деке нет подсказок при наведении: что значат значки, сказано здесь.
                    <div style={{ ...HINT_STYLE, marginBottom: "8px", flexShrink: 0, display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }}>
                        <HiTrash /> удалить (неудачный вариант);
                        <HiNoSymbol style={{ marginLeft: "4px" }} /> запретить (мусор)
                    </div>
                )}
                <style>{`.dt-rowfocus { background: rgba(103,160,255,0.22) !important; }`}</style>
                {entries === null && <p style={{ color: "#aaa" }}>Загрузка...</p>}
                {entries !== null && sorted.length === 0 && <p style={{ color: "#aaa" }}>{emptyText()}</p>}
                {hasAnything && (
                    // Записей не больше 60 (предел словаря на бэкенде), поэтому без страниц:
                    // фокус с геймпада сам прокручивает список к выбранной строке. Словарь и
                    // отбракованные — в одной прокрутке, чтобы крестовина проходила всё окно.
                    <Focusable style={{ flex: "1 1 auto", minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: "6px", paddingRight: "8px", paddingTop: "4px", paddingBottom: "8px" }}>
                        {sorted.map((entry) => (
                            <EntryRow
                                key={entry.matchKey}
                                entry={entry}
                                onDelete={() => removeEntry(entry)}
                                onForbid={() => forbidEntry(entry)}
                            />
                        ))}
                        {rejected.length > 0 && (
                            <div style={{ marginTop: sorted.length > 0 ? "12px" : "0" }}>
                                <div style={{ fontSize: "14px", fontWeight: "bold" }}>Отбракованные ({rejected.length})</div>
                                <div style={{ ...HINT_STYLE, marginTop: "2px" }}>
                                    Попало ранее по ошибке. «Чистка» — убрано автоматически,
                                    «Вы» — запрещено вами. <HiArrowUturnLeft style={{ verticalAlign: "middle" }} /> Убрать из списка.
                                </div>
                            </div>
                        )}
                        {rejected.map((item) => (
                            <RejectedRow key={`rejected-${item.matchKey}`} item={item} onAllow={() => allowTerm(item)} />
                        ))}
                    </Focusable>
                )}
            </Focusable>
        </ModalRoot>
    );
};

// Открыть словарь игры, запущенной сейчас, для пары языков из настроек. Ключ игры —
// Steam appid, как при снимке экрана (Translator.tsx): так окно найдёт словарь,
// под которым метод перевода его сохранил.
export const openGlossaryModal = (languages: GlossaryLanguages) => {
    const app = Router.MainRunningApp;
    showModal(
        <GlossaryModal
            // String(): тип appid объявлен строкой, но в других типах Steam это
            // число; бэкенд ждёт текст (так же сделано в Translator.tsx).
            gameId={String(app?.appid ?? "")}
            gameName={app?.display_name || ""}
            {...languages}
        />
    );
};
