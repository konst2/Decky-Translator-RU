import { VFC } from "react";

// A quiet mark on the translated shot: "this is a captured frame, not the game;
// this button closes it". The game is not paused by default and keeps taking
// the buttons, so without a mark the user may press "next" into a game that has
// already moved on behind a frame that looks exactly like it.
//
// Pictogram instead of words: four small corners repeat the frame corners and tie
// the mark to them. The button glyph tells how to leave; that it has to be held,
// the user learns from the hold indicator that already exists.

export type BadgeCorner = "top-left" | "top-right" | "bottom-right";

export type Rect = { left: number; top: number; width: number; height: number };

// Room reserved for the badge when looking for a free corner. Fixed and generous
// rather than measured: the real width depends on the hotkey label, and a slightly
// pessimistic box only makes the choice more careful.
const BADGE_RESERVE_WIDTH = 150;
const BADGE_RESERVE_HEIGHT = 36;

// Distance from the frame edges. The frame corner arms run 12–38 px along the
// edges, so at 20 px the badge sits inside the "L" of the arm without touching it.
export const BADGE_INSET = 20;

// Order of preference when corners are equally free. Top first: dialogue boxes and
// subtitles are usually at the bottom. Bottom-left is not a candidate at all — the
// status pill and the "translations hidden" eye already live there.
const CORNER_PREFERENCE: BadgeCorner[] = ["top-right", "top-left", "bottom-right"];

function overlapArea(a: Rect, b: Rect): number {
    const width = Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left);
    const height = Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top);
    if (width <= 0 || height <= 0) {
        return 0;
    }
    return width * height;
}

function reserveBox(corner: BadgeCorner, frameWidth: number, frameHeight: number): Rect {
    const left = corner === "top-left" ? BADGE_INSET : frameWidth - BADGE_INSET - BADGE_RESERVE_WIDTH;
    const top = corner === "bottom-right" ? frameHeight - BADGE_INSET - BADGE_RESERVE_HEIGHT : BADGE_INSET;
    return { left, top, width: BADGE_RESERVE_WIDTH, height: BADGE_RESERVE_HEIGHT };
}

// The corner whose badge box is covered least by translation plates.
//
// Plates are passed as their text blocks on screen. A plate may grow wider than its
// block (allowLabelGrowth) or taller (font scale); that is not chased here — the
// badge is drawn under the plates, so in the worst case a plate covers part of it,
// and the edge shade still marks the shot.
//
// Pure function of the plates and the frame size: the same shot always gets the same
// corner, so the badge does not jump when translations are hidden and shown again.
export function pickBadgeCorner(plates: Rect[], frameWidth: number, frameHeight: number): BadgeCorner {
    let bestCorner = CORNER_PREFERENCE[0];
    let bestOverlap = Infinity;
    for (const corner of CORNER_PREFERENCE) {
        const box = reserveBox(corner, frameWidth, frameHeight);
        let overlap = 0;
        for (const plate of plates) {
            overlap += overlapArea(box, plate);
        }
        // Strictly less: on a tie the earlier corner in the preference order wins
        if (overlap < bestOverlap) {
            bestCorner = corner;
            bestOverlap = overlap;
        }
    }
    return bestCorner;
}

// Four small corners, the same picture as the frame corners
const SnapshotIcon: VFC = () => (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"
         stroke="rgba(255, 255, 255, 0.85)" strokeWidth="1.6" strokeLinecap="round">
        <path d="M1 5V1h4" />
        <path d="M11 1h4v4" />
        <path d="M15 11v4h-4" />
        <path d="M5 15H1v-4" />
    </svg>
);

export const SnapshotBadge: VFC<{ corner: BadgeCorner; hotkeyLabel: string }> = ({ corner, hotkeyLabel }) => {
    const position = {
        top: corner === "bottom-right" ? undefined : BADGE_INSET,
        bottom: corner === "bottom-right" ? BADGE_INSET : undefined,
        left: corner === "top-left" ? BADGE_INSET : undefined,
        right: corner === "top-left" ? undefined : BADGE_INSET,
    };

    return (
        <div style={{
            position: "absolute",
            ...position,
            display: "flex",
            alignItems: "center",
            gap: "8px",
            padding: "5px 9px",
            borderRadius: "16px",
            // Quieter than the status pill (0.7): it stays on screen the whole time
            background: "rgba(0, 0, 0, 0.5)",
            pointerEvents: "none",
            animation: "fadeInTranslation 0.35s ease-out both",
        }}>
            <SnapshotIcon />
            {hotkeyLabel && (
                // Drawn like a button glyph: the label in a thin rounded box
                <span style={{
                    color: "rgba(255, 255, 255, 0.85)",
                    fontSize: "12px",
                    lineHeight: "1",
                    fontWeight: 600,
                    padding: "2px 5px",
                    border: "1px solid rgba(255, 255, 255, 0.6)",
                    borderRadius: "4px",
                    whiteSpace: "nowrap",
                }}>
                    {hotkeyLabel}
                </span>
            )}
        </div>
    );
};
