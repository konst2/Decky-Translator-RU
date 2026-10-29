// src/tabs/BottomSpacer.tsx - Last focus stop at the bottom of every tab

import { Focusable } from "@decky/ui";
import { VFC } from "react";

// The bottom of the tab panel lies under the SteamOS footer (button hints).
// A plain paddingBottom is not enough: with the D-pad, Steam scrolls only up to
// the focused element, so the padding below the last control never comes into
// view and that control stays covered. This empty spacer is the last focus stop:
// focusing it scrolls the panel to the very end. Touch scrolling worked anyway.
//
// Pitfall: when focus lands here no focus ring is shown (noFocusRing), so the
// highlight seems to disappear; pressing up returns to the last control.
// onActivate is what makes a Focusable without interactive children take focus;
// pressing A on it does nothing on purpose.
export const BottomSpacer: VFC = () => (
    <Focusable
        noFocusRing
        onActivate={() => { /* intentionally ignored */ }}
        style={{ height: "48px" }}
    >
        <div />
    </Focusable>
);
