import { VFC } from "react";
import { findModuleChild } from "@decky/ui";
import { SETTINGS_DEFAULTS } from "./settingsDefaults";

// UI Composition layers provided by Decky
enum UIComposition {
    Hidden = 0,
    Notification = 1,
    Overlay = 2,
    Opaque = 3,
    OverlayKeyboard = 4,
}

// Hook into Decky's UI composition to ensure our indicator renders above the game or overlay
const useUIComposition: (composition: UIComposition) => void = findModuleChild(
    (m) => {
        if (typeof m !== "object") return undefined;
        for (let prop in m) {
            const fn = (m as any)[prop];
            if (
                typeof fn === "function" &&
                fn.toString().includes("AddMinimumCompositionStateRequest") &&
                fn.toString().includes("ChangeMinimumCompositionStateRequest") &&
                fn.toString().includes("RemoveMinimumCompositionStateRequest") &&
                !fn.toString().includes("m_mapCompositionStateRequests")
            ) {
                return fn;
            }
        }
    }
);

const SIZE = 36;
const CENTER = SIZE / 2;
const STROKE_WIDTH = 5.5;
// Outer edge lands exactly on the 36 px box: a larger radius makes the stroke poke past
// the viewport, and SVG clips it flat at its widest points
const ARC_RADIUS = CENTER - STROKE_WIDTH / 2;
const DOT_RADIUS = 8;
const ARC_COLOR = "#ffffff";
// Faint copy of the finished arc under the growing one: shows where the arc is heading,
// so the user sees how much of the hold is left and that the end is "the mark is whole"
const ARC_TARGET_COLOR = "rgba(255, 255, 255, 0.2)";
// Exactly half of the circle; flat (butt) caps make the ends look like the Steam Deck mark
const ARC_LENGTH = Math.PI * ARC_RADIUS;
const ARC_GAP = 2 * Math.PI * ARC_RADIUS;
// An SVG circle's stroke starts at 3 o'clock and runs clockwise. Turned by 90° it starts
// at 6 o'clock and runs through 9 to 12: the finished arc is the left half, with the dot
// to its right, like the Steam Deck mark.
const ARC_START_ROTATION = "rotate(90deg)";

interface ActivationIndicatorProps {
    visible: boolean;
    text?: string;
    forDismiss?: boolean; // true when dismissing overlay
    holdMs?: number; // hold duration; the arc grows from nothing to a half circle in that time
}

// Mountable component that holds a composition state request.
// When unmounted, the hook cleanup removes the request entirely.
const CompositionRequest: VFC<{ level: UIComposition }> = ({ level }) => {
    useUIComposition(level);
    return null;
};

export const ActivationIndicator: VFC<ActivationIndicatorProps> = ({ visible, text, forDismiss, holdMs }) => {
    const layer = forDismiss ? UIComposition.Overlay : UIComposition.Notification;

    const dotColor = forDismiss ? "#ff9a3c" : "#1a9fff";
    // The arc grows on a CSS timer of the hold length, not from the progress value Input
    // reports: that one comes from button polling in steps and would make the arc jerk.
    // Releasing early hides the whole indicator, so the two never visibly disagree.
    const growMs = Math.max(holdMs ?? SETTINGS_DEFAULTS.holdTimeTranslate, 100);

    return (
        <>
        {visible && <CompositionRequest level={layer} />}
        <div style={{
            position: "fixed",
            bottom: "20px",
            left: "20px",
            zIndex: 8003,
            display: "flex",
            flexDirection: "row",
            alignItems: "center",
            background: "rgba(0, 0, 0, 0.7)",
            padding: '8px 12px',
            borderRadius: '20px',
            boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
            opacity: visible ? 1 : 0,
            pointerEvents: visible ? "auto" : "none",
        }}>
            {/* This component is a separate React root, so Overlay.tsx keyframes are not visible here */}
            <style>{`
                @keyframes deckArcGrow {
                    from { stroke-dashoffset: ${ARC_LENGTH}; }
                    to { stroke-dashoffset: 0; }
                }
            `}</style>
            <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} style={{ overflow: "visible" }}>
                <circle
                    cx={CENTER}
                    cy={CENTER}
                    r={DOT_RADIUS}
                    fill={dotColor}
                />
                {/* Target: the finished half arc, faint */}
                <circle
                    cx={CENTER}
                    cy={CENTER}
                    r={ARC_RADIUS}
                    fill="none"
                    stroke={ARC_TARGET_COLOR}
                    strokeWidth={STROKE_WIDTH}
                    strokeLinecap="butt"
                    strokeDasharray={`${ARC_LENGTH} ${ARC_GAP}`}
                    style={{
                        transform: ARC_START_ROTATION,
                        transformOrigin: `${CENTER}px ${CENTER}px`,
                    }}
                />
                {/* Progress: the same half arc revealed from its start. The dash offset runs
                    from ARC_LENGTH (nothing shown) to 0 (the whole half). "forwards" keeps it
                    full at the end; switching the animation off while hidden restarts it on
                    the next hold. */}
                <circle
                    cx={CENTER}
                    cy={CENTER}
                    r={ARC_RADIUS}
                    fill="none"
                    stroke={ARC_COLOR}
                    strokeWidth={STROKE_WIDTH}
                    strokeLinecap="butt"
                    strokeDasharray={`${ARC_LENGTH} ${ARC_GAP}`}
                    strokeDashoffset={ARC_LENGTH}
                    style={{
                        transform: ARC_START_ROTATION,
                        transformOrigin: `${CENTER}px ${CENTER}px`,
                        animation: visible ? `deckArcGrow ${growMs}ms linear forwards` : "none",
                    }}
                />
            </svg>
            {text && (
                <div style={{
                    marginLeft: "10px",
                    color: "#ffffff",
                    fontSize: "14px",
                    whiteSpace: "nowrap"
                }}>
                    {text}
                </div>
            )}
        </div>
        </>
    );
};
