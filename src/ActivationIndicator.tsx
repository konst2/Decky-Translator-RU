import { VFC } from "react";
import { findModuleChild } from "@decky/ui";

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
// the viewport, and SVG clips it flat at the widest points of the rotating arc
const ARC_RADIUS = CENTER - STROKE_WIDTH / 2;
const DOT_RADIUS = 8;
const ARC_COLOR = "#ffffff";
// Exactly half of the circle; flat (butt) caps make the ends look like the Steam Deck mark
const ARC_LENGTH = Math.PI * ARC_RADIUS;
const ARC_GAP = 2 * Math.PI * ARC_RADIUS;

interface ActivationIndicatorProps {
    visible: boolean;
    text?: string;
    forDismiss?: boolean; // true when dismissing overlay
    holdMs?: number; // hold duration; the arc makes one turn in that time
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
    const spinMs = Math.max(holdMs ?? 1000, 200);

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
                @keyframes deckArcSpin {
                    from { transform: rotate(-90deg); }
                    to { transform: rotate(270deg); }
                }
            `}</style>
            <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} style={{ overflow: "visible" }}>
                <circle
                    cx={CENTER}
                    cy={CENTER}
                    r={DOT_RADIUS}
                    fill={dotColor}
                />
                <circle
                    cx={CENTER}
                    cy={CENTER}
                    r={ARC_RADIUS}
                    fill="none"
                    stroke={ARC_COLOR}
                    strokeWidth={STROKE_WIDTH}
                    strokeLinecap="butt"
                    strokeDasharray={`${ARC_LENGTH} ${ARC_GAP}`}
                    style={{
                        transform: "rotate(-90deg)",
                        transformOrigin: `${CENTER}px ${CENTER}px`,
                        animation: visible ? `deckArcSpin ${spinMs}ms linear infinite` : "none",
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
