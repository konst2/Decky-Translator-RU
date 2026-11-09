import { VFC } from "react";
import { DataFlowIconGlyph } from "./tabs/DataFlowBadge";
import type { DataFlow } from "./tabs/DataFlowBadge";
import { INDICATOR_Z_INDEX } from "./overlayLayers";

// Status pill in the bottom-left corner: a sweeping line while a run is working,
// plain text for short messages ("no text found", errors).
//
// Rendered as a sibling of #translation-overlay, not its child. The overlay creates
// its own stacking context at OVERLAY_Z_INDEX, so anything inside it stays under
// Steam's hints bar and keyboard whatever its own z-index. Next to the overlay the
// pill competes with Steam's layers directly and stays visible. That is fine only
// because the pill is short-lived: it shows for the duration of a run, and every
// message after it closes by itself within a few seconds.
//
// Being outside the overlay, the pill does not inherit two things from it, so they
// are explicit here: it hides together with the overlay (`visible`), and it lets a
// tap through (pointerEvents: none) to the overlay underneath, where the tap closes it.
//
// The sweep keyframes live here and not in Overlay.tsx: the pill must not depend on
// a <style> block of another component that may be edited without knowing about it.

type StatusPillProps = {
    visible: boolean;    // the overlay is shown
    step: string;        // current stage or message; empty -- no pill
    detail: string;      // small second line while working (method name)
    isError: boolean;
    loading: boolean;    // a run is in progress: "..." after the text and the sweep
    dataFlow: DataFlow | null;
};

export const StatusPill: VFC<StatusPillProps> = ({ visible, step, detail, isError, loading, dataFlow }) => {
    if (!visible || !step) {
        return null;
    }

    const isWorking = loading && !isError;

    return (
        <>
        <style>{`
            @keyframes statusSweep {
                0% { transform: translateX(-100%); }
                100% { transform: translateX(333%); }
            }
        `}</style>
        <div style={{
            display: "flex",
            flexDirection: "row",
            alignItems: "center",
            position: "fixed",
            bottom: "20px",
            left: "20px",
            overflow: "hidden",
            color: "#ffffff",
            background: "rgba(0, 0, 0, 0.7)",
            padding: '8px 12px',
            borderRadius: '20px',
            maxWidth: "420px",
            boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
            // One below the hotkey hold sign, which sits in the same corner:
            // while the user holds the hotkey to close, the sign must be on top
            zIndex: INDICATOR_Z_INDEX - 1,
            pointerEvents: "none",
        }}>
            {isError && (
                <svg
                    width="18"
                    height="18"
                    viewBox="0 0 24 24"
                    fill="#ff6b6b"
                    style={{ flexShrink: 0, marginRight: "10px" }}
                >
                    <path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z" />
                </svg>
            )}
            {/* Where data goes: the current stage while working, the whole run on
                final statuses and errors (after the error icon: the error matters more).
                Shape and color only, no label: a local stage can be over in a fraction
                of a second. */}
            {dataFlow && (
                <span style={{ display: "inline-flex", flexShrink: 0, marginRight: "10px" }}>
                    <DataFlowIconGlyph flow={dataFlow} size={16} />
                </span>
            )}
            <div style={{ fontSize: "14px", whiteSpace: "pre-line", lineHeight: "1.3" }}>
                {isWorking ? `${step}...` : step}
                {!isError && detail && (
                    <div style={{ fontSize: "10px", opacity: 0.7 }}>{detail}</div>
                )}
            </div>
            {isWorking && (
                <>
                    <div style={{
                        position: "absolute",
                        left: 0,
                        right: 0,
                        bottom: 0,
                        height: "2px",
                        background: "rgba(255, 255, 255, 0.10)",
                    }} />
                    <div style={{
                        position: "absolute",
                        bottom: 0,
                        left: 0,
                        width: "30%",
                        height: "2px",
                        borderRadius: "2px",
                        background: "#1a9fff",
                        animation: "statusSweep 1.3s ease-in-out infinite",
                    }} />
                </>
            )}
        </div>
        </>
    );
};
