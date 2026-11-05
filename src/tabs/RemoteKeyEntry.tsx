// src/tabs/RemoteKeyEntry.tsx - entering a key from a phone or PC
//
// Typing a long API key with the gamepad is the hardest part of setting up
// the plugin. This block of the key dialog starts a small web page on the
// Deck (backend: py_modules/providers/key_entry_server.py) where the key can
// be pasted from another device in the same network.

import { DialogButton, Focusable } from "@decky/ui";
import { VFC, useState, useEffect, useRef } from "react";
import { call } from "@decky/api";
import { QRCodeSVG } from "qrcode.react";
import { HiExclamationTriangle } from "react-icons/hi2";

// Backend settings that the key entry page (key_entry_server.py) saves for
// each method: the main field and, for Yandex, folder_id. Must match
// METHOD_FIELDS there. Kept here so the dialog knows which of its two fields
// a value received from the page belongs to.
export type KeyEntryMethod = 'googlecloud' | 'gemini_vision' | 'yandex' | 'gigachat' | 'mymemory';
const KEY_ENTRY_SETTINGS: Record<KeyEntryMethod, { key: string; folderId?: string }> = {
    googlecloud: { key: 'google_api_key' },
    gemini_vision: { key: 'gemini_api_key' },
    yandex: { key: 'yandex_api_key', folderId: 'yandex_folder_id' },
    gigachat: { key: 'gigachat_api_key' },
    mymemory: { key: 'mymemory_email' },
};

// How often the dialog asks the backend whether something came from the page.
// A person switches from the phone back to the Deck in a few seconds, so
// 1.5 s feels instant and costs nothing.
const KEY_ENTRY_POLL_MS = 1500;

export type ReceivedValues = { key?: string; folderId?: string };

// Picks this method's values out of what the backend returns
// ({setting_key: value}) and names them by dialog field.
const pickReceived = (method: KeyEntryMethod, saved: Record<string, string>): ReceivedValues => {
    const settingNames = KEY_ENTRY_SETTINGS[method];
    const received: ReceivedValues = {};
    if (settingNames.key in saved) received.key = saved[settingNames.key];
    if (settingNames.folderId && settingNames.folderId in saved) {
        received.folderId = saved[settingNames.folderId];
    }
    return received;
};

const hasReceived = (values: ReceivedValues) =>
    values.key !== undefined || values.folderId !== undefined;

// Values saved from the page that nobody has taken yet. Called by the poll
// below and once more by the dialog's Save button: between two polls the
// field still holds the old text, and saving it would overwrite the key
// that has just come from the phone.
export const takeReceivedValues = async (method: KeyEntryMethod): Promise<ReceivedValues> => {
    const saved = await call<[], Record<string, string>>('get_key_entry_saved');
    return pickReceived(method, saved);
};

type KeyEntryState =
    | { status: 'off' }
    | { status: 'starting' }
    | { status: 'running'; url: string }
    | { status: 'no_network' }
    | { status: 'error'; message: string };

// "Enter from another device" block of the key dialog: starts a small web
// page on the Deck, shows its address and QR code, and hands values saved
// there to the dialog. The page stops when the dialog closes (unmount).
export const RemoteKeyEntry: VFC<{
    method: KeyEntryMethod;
    onReceived: (values: ReceivedValues) => void;
}> = ({ method, onReceived }) => {
    const [state, setState] = useState<KeyEntryState>({ status: 'off' });

    // Whether the dialog is still open. Starting takes a moment; if the
    // dialog closes meanwhile, the unmount cleanup's stop may reach the
    // backend before the start finishes, so the start stops the page itself.
    const mountedRef = useRef(true);

    // The poll timer below lives across renders; through a ref it always
    // calls the latest onReceived, not the one from the first render.
    const onReceivedRef = useRef(onReceived);
    onReceivedRef.current = onReceived;

    const start = async () => {
        setState({ status: 'starting' });
        try {
            const result = await call<[string], { ok: boolean; ip?: string | null; port?: number; error?: string }>(
                'start_key_entry', method
            );
            if (!mountedRef.current) {
                call('stop_key_entry').catch(() => { /* plugin may be unloading */ });
                return;
            }
            if (!result.ok) {
                setState({ status: 'error', message: result.error || 'неизвестная ошибка' });
            } else if (!result.ip) {
                // The server runs, but there is no address to reach it from
                // outside: stop it rather than leave it listening for nothing.
                await call('stop_key_entry');
                setState({ status: 'no_network' });
            } else {
                setState({ status: 'running', url: `http://${result.ip}:${result.port}` });
            }
        } catch (error) {
            setState({ status: 'error', message: String(error) });
        }
    };

    const isRunning = state.status === 'running';

    // Poll for values saved from the page while it runs.
    useEffect(() => {
        if (!isRunning) return;
        const timer = setInterval(async () => {
            try {
                const received = await takeReceivedValues(method);
                if (hasReceived(received)) {
                    onReceivedRef.current(received);
                }
            } catch {
                // A missed poll is harmless: the values wait for the next one.
            }
        }, KEY_ENTRY_POLL_MS);
        return () => clearInterval(timer);
    }, [isRunning]);

    // Stop the page when the dialog closes, whatever button closed it.
    // stop returns what was saved after the last poll (the dialog was closed
    // right after sending from the phone); it still goes to onReceived, so
    // the plugin state (key indicator, GigaChat key check) learns about it.
    // The dialog's own field updates are then for an unmounted component,
    // React ignores them; what matters is onSave inside onReceived.
    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            call<[], Record<string, string>>('stop_key_entry')
                .then((saved) => {
                    const received = pickReceived(method, saved || {});
                    if (hasReceived(received)) onReceivedRef.current(received);
                })
                .catch(() => { /* plugin may be unloading */ });
        };
    }, []);

    const warning = (
        <div style={{ display: "flex", gap: "6px", color: "#e0b050", fontSize: "12px", marginTop: "8px" }}>
            <HiExclamationTriangle style={{ flexShrink: 0, marginTop: "2px" }} />
            <span>
                Не включайте в сетях, которым не доверяете (кафе, отель, общий Wi-Fi):
                страницу сможет открыть любой в этой сети.
            </span>
        </div>
    );

    if (state.status === 'running') {
        return (
            <div style={{ marginTop: "16px", display: "flex", gap: "14px", alignItems: "center" }}>
                {/* Not showQrModal: a second modal would cover this dialog,
                    and it must stay visible to show the received values. */}
                <QRCodeSVG value={state.url} size={128} includeMargin style={{ flexShrink: 0 }} />
                <div style={{ fontSize: "13px", color: "#aaa" }}>
                    <div>Откройте в браузере на телефоне или компьютере в той же сети:</div>
                    <div style={{ fontSize: "18px", color: "#fff", margin: "6px 0", fontFamily: "monospace" }}>
                        {state.url}
                    </div>
                    <div>Сохранённое там сразу появится в полях выше.</div>
                    {warning}
                </div>
            </div>
        );
    }

    return (
        <div style={{ marginTop: "16px" }}>
            <Focusable>
                <DialogButton onClick={start} disabled={state.status === 'starting'}>
                    {state.status === 'starting' ? "Запуск…" : "Ввести с телефона или компьютера"}
                </DialogButton>
            </Focusable>
            {state.status === 'no_network' && (
                <div style={{ color: "#ff6b6b", fontSize: "12px", marginTop: "8px" }}>
                    Дека не подключена к сети. Подключитесь к Wi-Fi и нажмите снова.
                </div>
            )}
            {state.status === 'error' && (
                <div style={{ color: "#ff6b6b", fontSize: "12px", marginTop: "8px" }}>
                    Не удалось запустить: {state.message}
                </div>
            )}
            {warning}
        </div>
    );
};
