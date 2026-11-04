import { InputMode } from "./Input";

// Short name of the translate/dismiss hotkey, as drawn on a button glyph.
//
// Not inputModeOptions (tabs/TabControls.tsx): those labels are for the settings
// list and carry explanations like "(Нажатие левого стика)", too long for a glyph.
// The input diagnostics in TabControls.tsx keep a similar table of their own
// (getInputModeButtons): they get the mode as an enum name string, not as InputMode.
export function hotkeyShortLabel(mode: InputMode): string {
    switch (mode) {
        case InputMode.L3_BUTTON: return "L3";
        case InputMode.L4_BUTTON: return "L4";
        case InputMode.L5_BUTTON: return "L5";
        case InputMode.R3_BUTTON: return "R3";
        case InputMode.R4_BUTTON: return "R4";
        case InputMode.R5_BUTTON: return "R5";
        case InputMode.L3_R3_COMBO: return "L3 + R3";
        case InputMode.L4_R4_COMBO: return "L4 + R4";
        case InputMode.L5_R5_COMBO: return "L5 + R5";
        case InputMode.TOUCHPAD_COMBO: return "Тачпады";
        default: return "";
    }
}
