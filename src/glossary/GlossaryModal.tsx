// src/glossary/GlossaryModal.tsx - окно словаря ИИ-перевода: просмотр, удаление записи, сброс
//
// Словарь имён и терминов, который ИИ-метод перевода с контекстом ведёт для каждой
// игры и пары языков и хранит на бэкенде между перезапусками. Окно показывает словарь
// игры, запущенной сейчас, и даёт удалить неудачную запись или сбросить словарь целиком.
//
// Пока такой метод один — GigaChat+ (пилот): RPC get_glossary / delete_glossary_entry /
// reset_glossary на бэкенде ходят в его провайдер. Само окно о методе не знает.

import { showModal, ModalRoot, DialogButton, Focusable, Router } from "@decky/ui";
import { VFC, useState, useEffect } from "react";
import { call, toaster } from "@decky/api";
import { HiTrash, HiXCircle, HiExclamationTriangle } from "react-icons/hi2";

// Запись словаря в том виде, в каком её отдаёт бэкенд (get_glossary).
interface GlossaryEntryView {
    matchKey: string;
    term: string;
    translation: string;
}

// Языки словаря: коды уходят на бэкенд, подписи показываются в окне.
export interface GlossaryLanguages {
    sourceLang: string;
    targetLang: string;
    sourceLabel: string;
    targetLabel: string;
}

// Окно словаря для игры, запущенной сейчас. Игру и языки окно получает
// при открытии, а не берёт из последнего перевода: открытое в новой игре до первого
// перевода, оно должно показать словарь этой игры, а не предыдущей.
const GlossaryModal: VFC<GlossaryLanguages & {
    closeModal?: () => void;
    gameId: string;
    gameName: string;
}> = ({ closeModal, gameId, gameName, sourceLang, targetLang, sourceLabel, targetLabel }) => {
    const [entries, setEntries] = useState<GlossaryEntryView[] | null>(null);
    const [loadError, setLoadError] = useState("");
    const [confirmReset, setConfirmReset] = useState(false);

    useEffect(() => {
        if (!gameId) {
            setEntries([]);
            return;
        }
        call<[string, string, string], any>('get_glossary', gameId, sourceLang, targetLang)
            .then((result) => {
                if (result?.success) {
                    setEntries(result.entries || []);
                } else {
                    setLoadError(result?.error || "неизвестная ошибка");
                    setEntries([]);
                }
            })
            .catch((e) => {
                setLoadError(String(e));
                setEntries([]);
            });
    }, [gameId, sourceLang, targetLang]);

    // Бэкенд отдаёт записи по давности использования (так их вытесняет словарь),
    // а человеку проще найти имя по алфавиту.
    const sorted = [...(entries || [])].sort((a, b) => a.term.localeCompare(b.term));

    const showError = (action: string, error: unknown) => {
        toaster.toast({ title: "Словарь игры", body: `Не удалось ${action}: ${error}`, duration: 4000 });
    };

    const removeEntry = async (entry: GlossaryEntryView) => {
        try {
            const result = await call<[string, string, string, string], any>(
                'delete_glossary_entry', gameId, sourceLang, targetLang, entry.matchKey
            );
            if (!result?.success) {
                showError("удалить запись", result?.error || "неизвестная ошибка");
                return;
            }
            setEntries(prev => prev ? prev.filter(x => x.matchKey !== entry.matchKey) : prev);
        } catch (e) {
            showError("удалить запись", e);
        }
    };

    // Как «Очистить всё» в окне кэша: первое нажатие взводит кнопку, второе сбрасывает.
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
        } catch (e) {
            showError("сбросить словарь", e);
        }
    };

    const emptyText = () => {
        if (!gameId) return "Игра не запущена. Откройте это окно во время игры — в нём будет словарь этой игры.";
        if (loadError) return `Не удалось загрузить словарь: ${loadError}`;
        return "Словарь пока пуст. Он пополняется, пока вы переводите экраны в этой игре.";
    };

    return (
        <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
            <Focusable style={{ display: "flex", flexDirection: "column", maxHeight: "70vh", boxSizing: "border-box", minWidth: "540px" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "10px", marginBottom: "4px", flexShrink: 0 }}>
                    <div style={{ fontSize: "16px", fontWeight: "bold" }}>
                        Словарь игры{gameName && <> «{gameName}»</>}
                        {entries !== null && entries.length > 0 && <span style={{ color: "rgba(255,255,255,0.45)" }}> ({entries.length})</span>}
                    </div>
                    {sorted.length > 0 && (
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
                    <div style={{ fontSize: "11px", color: "#8b929a", marginBottom: "10px", flexShrink: 0 }}>
                        {sourceLabel} → {targetLabel}. У каждой пары языков свой словарь.
                    </div>
                )}
                <style>{`.dt-rowfocus { background: rgba(103,160,255,0.22) !important; }`}</style>
                {entries === null && <p style={{ color: "#aaa" }}>Загрузка...</p>}
                {entries !== null && sorted.length === 0 && <p style={{ color: "#aaa" }}>{emptyText()}</p>}
                {sorted.length > 0 && (
                    // Записей не больше 60 (предел словаря на бэкенде), поэтому без страниц:
                    // фокус с геймпада сам прокручивает список к выбранной строке.
                    <Focusable style={{ flex: "1 1 auto", minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: "6px", paddingRight: "8px", paddingTop: "4px", paddingBottom: "8px" }}>
                        {sorted.map((entry) => (
                            <Focusable
                                key={entry.matchKey}
                                focusWithinClassName="dt-rowfocus"
                                style={{ display: "flex", gap: "10px", alignItems: "center", padding: "6px 10px", background: "rgba(255,255,255,0.04)", borderRadius: "4px", scrollMargin: "8px" }}
                            >
                                <div style={{ flex: "1 1 auto", minWidth: 0, fontSize: "13px" }}>
                                    <span style={{ color: "#dcdedf" }}>{entry.term}</span>
                                    <span style={{ color: "rgba(255,255,255,0.35)" }}> → </span>
                                    <span style={{ color: "#6fcf97" }}>{entry.translation}</span>
                                </div>
                                <DialogButton
                                    onClick={() => removeEntry(entry)}
                                    style={{ minWidth: "32px", width: "32px", height: "32px", padding: "0", flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center" }}
                                >
                                    <HiTrash />
                                </DialogButton>
                            </Focusable>
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
