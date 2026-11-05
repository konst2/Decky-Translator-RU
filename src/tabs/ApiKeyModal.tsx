// src/tabs/ApiKeyModal.tsx - dialog for entering an API key (and Yandex folder_id)
//
// One dialog for every method that needs a key or other personal value;
// also reused for the MyMemory email. The value can be typed on the Deck or,
// for methods with keyEntryMethod, sent from a phone or PC (RemoteKeyEntry).

import { ModalRoot, DialogButton, TextField, Focusable } from "@decky/ui";
import { VFC, useState, useRef } from "react";
import { RemoteKeyEntry, KeyEntryMethod, ReceivedValues, takeReceivedValues } from "./RemoteKeyEntry";

export const ApiKeyModal: VFC<{
    currentKey: string;
    onSave: (key: string) => void;
    currentFolderId?: string;
    onSaveFolderId?: (folderId: string) => void;
    closeModal?: () => void;
    title?: string;
    description?: string;
    // Reused for fields that are not API keys (e.g. the MyMemory email):
    // own label, and shown in plain text since such a value is not a secret.
    fieldLabel?: string;
    isPassword?: boolean;
    // Offer entering the value from another device for this method.
    keyEntryMethod?: KeyEntryMethod;
}> = ({ currentKey, onSave, currentFolderId, onSaveFolderId, closeModal, title, description, fieldLabel = "API-ключ", isPassword = true, keyEntryMethod }) => {
    const [apiKey, setApiKeyState] = useState(currentKey || "");
    const [folderId, setFolderIdState] = useState(currentFolderId || "");
    const [receivedNote, setReceivedNote] = useState("");

    // The same values in refs. The Save button awaits the backend before
    // saving, and meanwhile a poll may put a value from the phone into the
    // fields; the click handler's own apiKey/folderId are from the render
    // it was created in and would save the old text. Refs always hold the
    // latest value, so every field change goes through these setters.
    const apiKeyRef = useRef(apiKey);
    const folderIdRef = useRef(folderId);
    const setApiKey = (value: string) => {
        apiKeyRef.current = value;
        setApiKeyState(value);
    };
    const setFolderId = (value: string) => {
        folderIdRef.current = value;
        setFolderIdState(value);
    };

    // The backend has already saved these values. They go through the same
    // onSave as the Save button anyway: that updates plugin state (key
    // indicator, GigaChat key check), and the fields here must show them,
    // or pressing Save afterwards would overwrite the new key with the old
    // text from the field.
    const handleReceived = (values: ReceivedValues) => {
        const labels: string[] = [];
        if (values.key !== undefined) {
            setApiKey(values.key);
            onSave(values.key);
            labels.push(fieldLabel);
        }
        if (values.folderId !== undefined) {
            setFolderId(values.folderId);
            onSaveFolderId?.(values.folderId);
            labels.push("folder_id");
        }
        setReceivedNote(`Получено и сохранено: ${labels.join(", ")}`);
    };

    return (
        <ModalRoot onCancel={closeModal} onEscKeypress={closeModal}>
            <div style={{ padding: "20px", minWidth: "400px" }}>
                <h2 style={{ marginBottom: "15px" }}>{title || "API-ключ"}</h2>
                <p style={{ marginBottom: "15px", color: "#aaa", fontSize: "13px" }}>
                    {description || "Введите ваш API-ключ."}
                </p>
                <TextField
                    label={fieldLabel}
                    value={apiKey}
                    bIsPassword={isPassword}
                    bShowClearAction={true}
                    onChange={(e) => setApiKey(e.target.value)}
                />
                {onSaveFolderId && (
                    <div style={{ marginTop: "12px" }}>
                        <TextField
                            label="folder_id"
                            value={folderId}
                            bShowClearAction={true}
                            onChange={(e) => setFolderId(e.target.value)}
                        />
                    </div>
                )}
                {receivedNote && (
                    <div style={{ color: "#4caf50", fontSize: "13px", marginTop: "10px" }}>
                        {receivedNote}
                    </div>
                )}
                {keyEntryMethod && (
                    <RemoteKeyEntry method={keyEntryMethod} onReceived={handleReceived} />
                )}
                <Focusable
                    style={{ display: "flex", gap: "10px", marginTop: "20px", justifyContent: "flex-end" }}
                >
                    <DialogButton onClick={closeModal}>
                        Отмена
                    </DialogButton>
                    <DialogButton
                        onClick={async () => {
                            // A value sent from the phone after the last poll
                            // wins over the field text: the field just has
                            // not caught up with it yet.
                            let received: ReceivedValues = {};
                            if (keyEntryMethod) {
                                try {
                                    received = await takeReceivedValues(keyEntryMethod);
                                } catch {
                                    // Backend unreachable: save what is in the fields.
                                }
                            }
                            onSave(received.key ?? apiKeyRef.current.trim());
                            onSaveFolderId?.(received.folderId ?? folderIdRef.current.trim());
                            closeModal?.();
                        }}
                    >
                        Сохранить
                    </DialogButton>
                </Focusable>
            </div>
        </ModalRoot>
    );
};
