// DebugInfoBox.tsx - On-screen panel for diagnostic values.
//
// What for: checking numbers right on the device (Steam Deck), where reading logs
// is awkward. Shows "label: value" lines in a screen corner.
//
// How to use:
//   1. Import it in the component whose values you want to see:
//        import { DebugInfoBox } from "./DebugInfoBox";
//   2. Mount it in that component's JSX, under a condition and a TEMPORARY comment,
//      so the mount is easy to find and remove:
//        {/* TEMPORARY: <what is being checked> */}
//        {visible && (
//            <DebugInfoBox
//                title="Snapshot / screen"
//                corner="top-left"
//                rows={[
//                    { label: "shot", value: `${width}×${height}` },
//                    { label: "dpr", value: String(dpr) },
//                ]}
//            />
//        )}
//   3. When done, delete the mount and the import. Leave this file: it is reusable.
//
// Rules and pitfalls:
//   - Values are strings: format numbers yourself (toFixed, `${w}×${h}`).
//   - Labels must be unique within one box: they are used as React keys.
//   - Mount it inside the overlay tree, not elsewhere: the plugin's own `window` is
//     Steam's hidden 1×1 context, and only a mount inside the overlay ends up on screen.
//   - The box does not shift the layout under it (position: fixed) and lets taps
//     through (pointer-events: none), so it is safe to put over what you are debugging.
//   - zIndex 7010 is above the overlay (7002–7003). Raise it if the box is hidden.
//   - Not for release builds: remove every mount before committing.

import { VFC } from "react";

export type DebugInfoRow = {
    label: string;
    value: string;
};

export type DebugInfoCorner = "top-left" | "top-right" | "bottom-left" | "bottom-right";

const cornerPosition = (corner: DebugInfoCorner, margin: number) => {
    switch (corner) {
        case "top-left":
            return { top: margin, left: margin };
        case "top-right":
            return { top: margin, right: margin };
        case "bottom-left":
            return { bottom: margin, left: margin };
        case "bottom-right":
            return { bottom: margin, right: margin };
    }
};

export const DebugInfoBox: VFC<{
    rows: DebugInfoRow[];
    title?: string;
    corner?: DebugInfoCorner;
}> = ({ rows, title, corner = "top-left" }) => (
    <div style={{
        // Fixed, not absolute: the panel sits on the screen itself, independent of
        // whatever box it is mounted in, so it never shifts the layout being debugged
        position: "fixed",
        ...cornerPosition(corner, 8),
        zIndex: 7010,
        // Must not swallow the tap that dismisses the overlay underneath
        pointerEvents: "none",
        background: "rgba(0, 0, 0, 0.75)",
        color: "#7CFC7C",
        fontFamily: "monospace",
        fontSize: "11px",
        lineHeight: "1.35",
        padding: "6px 8px",
        borderRadius: "4px",
        whiteSpace: "pre",
    }}>
        {title && <div style={{ color: "#ffffff", marginBottom: "2px" }}>{title}</div>}
        {rows.map((row) => (
            <div key={row.label}>{`${row.label}: ${row.value}`}</div>
        ))}
    </div>
);
