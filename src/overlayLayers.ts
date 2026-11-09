// overlayLayers.ts - Stacking levels (z-index) of the plugin's on-screen elements.
//
// Kept in a separate small module so that every component placing itself relative to
// Steam's layers imports the numbers from one place, without pulling in Overlay.tsx.
//
// The overlay and the indicators are global components (routerHook.addGlobalComponent),
// so they share one stacking context (.BasicUI) inside Steam's main UI window
// ("Big Picture") with Steam's own layers. Measured on the Deck via CEF remote
// debugging, November 2026 (Steam values, not ours):
//     auto   regular Steam UI (#Main)
//     10     Steam event overlay (PartnerEventOverlayContainer)
//     1500   dialogs opened with showModal, ours and other plugins' (.FullModalOverlay)
//     3900   dimming under the side menus, on-screen keyboard container
//     6000   on-screen keyboard (#virtual keyboard)
//     7000   button hints bar at the bottom (#Footer)
// Not affected by these values: the side menus (QAM, Steam menu) and toasts -- they are
// separate CEF windows (QuickAccess_*, MainMenu_*, notificationtoasts_*).
// If a Steam popup hides under the screenshot again, re-measure these layers first.

// The overlay must be above the regular UI and below every popup. The original plugin
// used 7002: the key entry dialog, the on-screen keyboard and the hints bar opened under
// the screenshot -- they took input but stayed invisible until the overlay was closed.
// 1000 leaves a margin below the dialogs in case Steam shifts them a little.
export const OVERLAY_Z_INDEX = 1000;

// Short-lived indicators stay above everything, Steam's popups included: they are
// visible for a few seconds at most, and the user must see them even when the hints
// bar or the keyboard is open. Two use it: the hotkey hold sign (ActivationIndicator)
// and the status pill (StatusPill, one below the hold sign -- same corner).
// Anything that can stay on screen for long must not use this level -- it would cover
// a corner of Steam's popups. That is why the "translations hidden" eye stays inside
// the overlay: it is shown for as long as the user keeps the translations hidden.
export const INDICATOR_Z_INDEX = 8003;
