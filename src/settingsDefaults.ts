// Settings defaults for the frontend, taken from py_modules/settings_defaults.json:
// the same file the backend reads, so the two sides cannot drift apart again.
// What each key means, and why the file lives in py_modules/, is described in
// documentation/settings_defaults.md.
//
// rollup inlines the JSON into dist at build time; the file itself is not read
// at runtime. The file lives outside src/, which is why tsconfig.json sets
// "rootDir": "." (without it the TypeScript plugin stops emitting src/ files).
//
// Why there are runtime checks below: TypeScript types a JSON import loosely
// ("left" becomes string, 0 becomes number), so a typo in the JSON would pass the
// build. tests/test_settings_defaults.py validates the same file before a release;
// these checks are the second line: a broken value fails loudly at startup
// instead of silently drawing a wrong overlay.
import rawDefaults from "../py_modules/settings_defaults.json";
import type { Settings } from "./SettingsContext";
// Type-only imports: Input.tsx and SettingsContext.tsx import this module,
// a runtime import back would make a cycle where InputMode is not yet defined.
import type { InputMode } from "./Input";

// Settings that have a default. API keys and the MyMemory email are user data
// (empty means "not entered"), useFreeProviders is deprecated and derived from
// the OCR provider, initialized is UI state — none of them is in the JSON.
export type SettingsDefaults = Omit<Settings,
    | 'initialized'
    | 'useFreeProviders'
    | 'googleApiKey'
    | 'geminiApiKey'
    | 'yandexApiKey'
    | 'yandexFolderId'
    | 'gigachatApiKey'
    | 'myMemoryEmail'>;

const OCR_PROVIDERS: readonly Settings['ocrProvider'][] =
    ['chromescreenai', 'rapidocr', 'ocrspace', 'googlecloud', 'gemini_vision'];
const TRANSLATION_PROVIDERS: readonly Settings['translationProvider'][] =
    ['mymemory', 'freegoogle', 'googlecloud', 'yandex', 'gigachat', 'gigachat_plus', 'ct2', 'opusmt'];
const TEXT_ALIGNMENTS: readonly Settings['translatedTextAlignment'][] =
    ['left', 'right', 'center', 'justify'];
const FONT_STYLES: readonly Settings['translatedTextFontStyle'][] =
    ['normal', 'bold', 'italic', 'bolditalic'];

function invalid(key: string, value: unknown): Error {
    return new Error(`settings_defaults.json: invalid value for ${key}: ${JSON.stringify(value)}`);
}

function oneOf<T extends string>(key: string, value: unknown, allowed: readonly T[]): T {
    const match = allowed.find((option) => option === value);
    if (match === undefined) {
        throw invalid(key, value);
    }
    return match;
}

function inputMode(key: string, value: unknown): InputMode {
    // Only the shape is checked here (InputMode is a type-only import, see above);
    // the allowed numbers are checked by tests/test_settings_defaults.py.
    if (typeof value !== 'number' || !Number.isInteger(value)) {
        throw invalid(key, value);
    }
    return value as InputMode;
}

// Keys are spelled out one by one instead of converting snake_case in a loop:
// this way the type checker sees every field and fails the build if one is missing.
export const SETTINGS_DEFAULTS: SettingsDefaults = {
    enabled: rawDefaults.enabled,
    inputLanguage: rawDefaults.input_language,
    targetLanguage: rawDefaults.target_language,
    inputMode: inputMode('input_mode', rawDefaults.input_mode),
    holdTimeTranslate: rawDefaults.hold_time_translate,
    holdTimeDismiss: rawDefaults.hold_time_dismiss,
    ocrProvider: oneOf('ocr_provider', rawDefaults.ocr_provider, OCR_PROVIDERS),
    translationProvider: oneOf('translation_provider', rawDefaults.translation_provider, TRANSLATION_PROVIDERS),
    geminiModel: rawDefaults.gemini_model,
    confidenceThreshold: rawDefaults.confidence_threshold,
    customRecognitionSettings: rawDefaults.custom_recognition_settings,
    rapidocrConfidence: rawDefaults.rapidocr_confidence,
    rapidocrBoxThresh: rawDefaults.rapidocr_box_thresh,
    rapidocrUnclipRatio: rawDefaults.rapidocr_unclip_ratio,
    rapidocrPersistentMode: rawDefaults.rapidocr_persistent_mode,
    chromeScreenAiPersistentMode: rawDefaults.chromescreenai_persistent_mode,
    ct2PersistentMode: rawDefaults.ct2_persistent_mode,
    pauseGameOnOverlay: rawDefaults.pause_game_on_overlay,
    quickToggleEnabled: rawDefaults.quick_toggle_enabled,
    debugMode: rawDefaults.debug_mode,
    fontScale: rawDefaults.font_scale,
    groupingPower: rawDefaults.grouping_power,
    translatedTextAlignment: oneOf('translated_text_alignment', rawDefaults.translated_text_alignment, TEXT_ALIGNMENTS),
    translatedTextFontFamily: rawDefaults.translated_text_font_family,
    translatedTextFontStyle: oneOf('translated_text_font_style', rawDefaults.translated_text_font_style, FONT_STYLES),
    hideIdenticalTranslations: rawDefaults.hide_identical_translations,
    allowLabelGrowth: rawDefaults.allow_label_growth,
    translationCacheEnabled: rawDefaults.translation_cache_enabled,
};
