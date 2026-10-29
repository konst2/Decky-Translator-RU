// src/tabs/DataFlowBadge.tsx - What leaves the device for each provider

import { VFC } from "react";
import { BsCloudSlash, BsCloudArrowUp } from "react-icons/bs";
import { Settings } from "../SettingsContext";

// Where the user's data goes when a provider is used:
// - local: nothing leaves the device;
// - text: the recognized text is sent to a cloud service;
// - screenshot: the whole screenshot is sent (it may show chat, nicknames,
//   addresses), so it is the most sensitive case and is shown in orange.
export type DataFlow = "local" | "text" | "screenshot";

// Two separate maps because the same id means different things: "googlecloud"
// receives a screenshot as an OCR provider but only text as a translation one.
// Typed by the provider unions from Settings: adding a provider there without
// a label here is a TypeScript error, so a new provider can't ship unlabeled.
export const OCR_DATA_FLOW: Record<Settings["ocrProvider"], DataFlow> = {
    rapidocr: "local",
    chromescreenai: "local",
    ocrspace: "screenshot",
    googlecloud: "screenshot",
    gemini_vision: "screenshot",
};

export const TRANSLATION_DATA_FLOW: Record<Settings["translationProvider"], DataFlow> = {
    ct2: "local",
    opusmt: "local",
    freegoogle: "text",
    googlecloud: "text",
    yandex: "text",
    mymemory: "text",
};

const DATA_FLOW_LABELS: Record<DataFlow, string> = {
    local: "Всё на устройстве",
    text: "В облако уходит текст",
    screenshot: "В облако уходит скриншот",
};

// Not a lock icon for "local": HiLockClosed already means "unavailable"
// (Gemini Vision in the translation list), a lock meaning "private" would confuse.
const DATA_FLOW_COLORS: Record<DataFlow, string> = {
    local: "#8b929a",
    text: "#8b929a",
    screenshot: "#ffa726",
};

const DataFlowIconGlyph: VFC<{ flow: DataFlow }> = ({ flow }) => {
    const style = { fontSize: "13px", color: DATA_FLOW_COLORS[flow], flexShrink: 0 };
    if (flow === "local") {
        return <BsCloudSlash style={style} />;
    }
    return <BsCloudArrowUp style={style} />;
};

// Icon with a label: for provider descriptions on the translation tab.
export const DataFlowBadge: VFC<{ flow: DataFlow }> = ({ flow }) => (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "6px", color: DATA_FLOW_COLORS[flow] }}>
        <DataFlowIconGlyph flow={flow} />
        <span>{DATA_FLOW_LABELS[flow]}</span>
    </div>
);

// Icon only: for the status rows on the main tab, where the row already names
// the provider. The label is kept as a tooltip for mouse/desktop mode.
export const DataFlowIcon: VFC<{ flow: DataFlow }> = ({ flow }) => (
    <span title={DATA_FLOW_LABELS[flow]} style={{ display: "inline-flex", alignItems: "center", marginLeft: "6px" }}>
        <DataFlowIconGlyph flow={flow} />
    </span>
);
