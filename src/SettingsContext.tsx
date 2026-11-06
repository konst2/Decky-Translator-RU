// src/SettingsContext.tsx
import React, { createContext, useContext, useEffect, useReducer } from 'react';
import { call } from '@decky/api';
import { GameTranslatorLogic } from './Translator';
import { InputMode } from './Input';
import { logger } from './Logger';
import { SETTINGS_DEFAULTS } from './settingsDefaults';

// Define the settings interface
export interface Settings {
    inputLanguage: string;
    targetLanguage: string;
    inputMode: InputMode;
    enabled: boolean;
    initialized: boolean;
    holdTimeTranslate: number;
    holdTimeDismiss: number;
    confidenceThreshold: number; // New setting for confidence threshold
    rapidocrConfidence: number; // RapidOCR-specific confidence threshold (0.0-1.0)
    rapidocrBoxThresh: number; // RapidOCR box detection threshold (0.0-1.0)
    rapidocrUnclipRatio: number; // RapidOCR box expansion ratio (1.0-3.0)
    rapidocrPersistentMode: boolean; // Keep RapidOCR worker alive between requests
    chromeScreenAiPersistentMode: boolean; // Keep Chrome Screen AI worker alive between requests
    ct2PersistentMode: boolean; // Keep CT2/NLLB worker alive between requests
    pauseGameOnOverlay: boolean; // Setting to control pausing game when overlay is shown
    quickToggleEnabled: boolean; // Quick toggle overlay with right button in combo modes
    useFreeProviders: boolean; // Use free providers (OCR.space + free Google Translate) - deprecated, use ocrProvider
    ocrProvider: 'rapidocr' | 'ocrspace' | 'googlecloud' | 'gemini_vision' | 'chromescreenai'; // OCR provider
    translationProvider: 'mymemory' | 'freegoogle' | 'googlecloud' | 'ct2' | 'opusmt' | 'yandex' | 'gigachat' | 'gigachat_plus'; // Translation provider
    googleApiKey: string; // Google Cloud Vision API key for text recognition
    geminiApiKey: string; // Gemini API key for Gemini Vision (free tier available)
    yandexApiKey: string; // Yandex Cloud API key for Yandex Translate
    yandexFolderId: string; // Yandex Cloud folder ID for Yandex Translate
    gigachatApiKey: string; // GigaChat Authorization Key (Base64 of Client ID and Secret)
    myMemoryEmail: string; // Optional email for MyMemory: raises its daily quota from 5 000 to 50 000 chars
    geminiModel: string; // Gemini model to use
    debugMode: boolean; // Debug mode for verbose console logging
    fontScale: number; // Overlay font scale multiplier for external monitors
    groupingPower: number; // Text grouping aggressiveness (0.25 normal - 1.0 huge)
    translatedTextAlignment: 'left' | 'right' | 'center' | 'justify';
    translatedTextFontFamily: string;
    translatedTextFontStyle: 'normal' | 'bold' | 'italic' | 'bolditalic';
    hideIdenticalTranslations: boolean;
    allowLabelGrowth: boolean;
    customRecognitionSettings: boolean;
    translationCacheEnabled: boolean;
}

// Define action types
// Settings masked in debug logs: users send these logs to GitHub issues.
// Mirrors SENSITIVE_SETTING_KEYS in main.py; a new secret must be added to both.
// The email is not a secret, but personal data, so the backend masks it too.
// Yandex folder ID stays readable: it is not a secret (same as on the backend).
const SENSITIVE_SETTINGS: (keyof Settings)[] = [
    'googleApiKey',
    'geminiApiKey',
    'yandexApiKey',
    'gigachatApiKey',
    'myMemoryEmail',
];

// Same shape as _mask_secret in main.py, so both logs look alike: the last 4 characters
// are enough to tell which key is set without revealing it
const maskSecret = (value: unknown): unknown => {
    if (typeof value !== 'string' || value === '') {
        return value;
    }
    if (value.length <= 4) {
        return '****';
    }
    return '*'.repeat(value.length - 4) + value.slice(-4);
};

// Returns a copy for logging; the settings object itself is left untouched
const maskSettingsForLog = (settings: Partial<Settings>): Record<string, unknown> => {
    const masked: Record<string, unknown> = { ...settings };
    for (const key of SENSITIVE_SETTINGS) {
        if (key in masked) {
            masked[key] = maskSecret(masked[key]);
        }
    }
    return masked;
};

type SettingsAction =
    | { type: 'INITIALIZE_SETTINGS', settings: Partial<Settings> }
    | { type: 'UPDATE_SETTING', key: keyof Settings, value: any }
    | { type: 'SET_INITIALIZED', initialized: boolean };

// Define the initial state. Defaults come from py_modules/settings_defaults.json
// (see settingsDefaults.ts); only fields without a default are listed here.
const initialSettings: Settings = {
    ...SETTINGS_DEFAULTS,
    initialized: false,
    useFreeProviders: true, // Deprecated, derived from ocrProvider on the backend
    // API keys and the MyMemory email: empty means "not entered"
    googleApiKey: "",
    geminiApiKey: "",
    yandexApiKey: "",
    yandexFolderId: "",
    gigachatApiKey: "",
    myMemoryEmail: "",
};

// Create the reducer
function settingsReducer(state: Settings, action: SettingsAction): Settings {
    switch (action.type) {
        case 'INITIALIZE_SETTINGS':
            return { ...state, ...action.settings };
        case 'UPDATE_SETTING':
            return { ...state, [action.key]: action.value };
        case 'SET_INITIALIZED':
            return { ...state, initialized: action.initialized };
        default:
            return state;
    }
}

// Create the context
interface SettingsContextType {
    settings: Settings;
    updateSetting: (key: keyof Settings, value: any, label?: string) => Promise<boolean>;
    initialized: boolean;
}

const SettingsContext = createContext<SettingsContextType | undefined>(undefined);

// Create the provider component
interface SettingsProviderProps {
    children: React.ReactNode;
    logic: GameTranslatorLogic;
}

export const SettingsProvider: React.FC<SettingsProviderProps> = ({
                                                                      children,
                                                                      logic
                                                                  }) => {
    const [settings, dispatch] = useReducer(settingsReducer, initialSettings);

    // Load all settings at once
    const loadAllSettings = async () => {
        try {
            const serverSettings = await call<[], any>('get_all_settings');

            if (serverSettings) {

                // Map backend settings to frontend settings
                const mappedSettings: Partial<Settings> = {
                    inputLanguage: serverSettings.input_language ?? SETTINGS_DEFAULTS.inputLanguage,
                    targetLanguage: serverSettings.target_language ?? SETTINGS_DEFAULTS.targetLanguage,
                    inputMode: serverSettings.input_mode ?? SETTINGS_DEFAULTS.inputMode,
                    enabled: serverSettings.enabled ?? SETTINGS_DEFAULTS.enabled,
                    holdTimeTranslate: serverSettings.hold_time_translate ?? SETTINGS_DEFAULTS.holdTimeTranslate,
                    holdTimeDismiss: serverSettings.hold_time_dismiss ?? SETTINGS_DEFAULTS.holdTimeDismiss,
                    // `||` (not `??`) turns a saved 0 into the default; a known bug, kept as is here
                    confidenceThreshold: serverSettings.confidence_threshold || SETTINGS_DEFAULTS.confidenceThreshold,
                    rapidocrConfidence: serverSettings.rapidocr_confidence ?? SETTINGS_DEFAULTS.rapidocrConfidence,
                    rapidocrBoxThresh: serverSettings.rapidocr_box_thresh ?? SETTINGS_DEFAULTS.rapidocrBoxThresh,
                    rapidocrUnclipRatio: serverSettings.rapidocr_unclip_ratio ?? SETTINGS_DEFAULTS.rapidocrUnclipRatio,
                    rapidocrPersistentMode: serverSettings.rapidocr_persistent_mode ?? SETTINGS_DEFAULTS.rapidocrPersistentMode,
                    chromeScreenAiPersistentMode: serverSettings.chromescreenai_persistent_mode ?? SETTINGS_DEFAULTS.chromeScreenAiPersistentMode,
                    ct2PersistentMode: serverSettings.ct2_persistent_mode ?? SETTINGS_DEFAULTS.ct2PersistentMode,
                    pauseGameOnOverlay: serverSettings.pause_game_on_overlay ?? SETTINGS_DEFAULTS.pauseGameOnOverlay,
                    quickToggleEnabled: serverSettings.quick_toggle_enabled ?? SETTINGS_DEFAULTS.quickToggleEnabled,
                    useFreeProviders: serverSettings.use_free_providers !== false, // Default to true (deprecated)
                    ocrProvider: serverSettings.ocr_provider || SETTINGS_DEFAULTS.ocrProvider,
                    translationProvider: serverSettings.translation_provider || SETTINGS_DEFAULTS.translationProvider,
                    googleApiKey: serverSettings.google_api_key || "", // Google API key
                    geminiApiKey: serverSettings.gemini_api_key || "", // Gemini API key
                    yandexApiKey: serverSettings.yandex_api_key || "", // Yandex API key
                    yandexFolderId: serverSettings.yandex_folder_id || "", // Yandex folder ID
                    gigachatApiKey: serverSettings.gigachat_api_key || "", // GigaChat Authorization Key
                    myMemoryEmail: serverSettings.mymemory_email || "", // MyMemory email
                    geminiModel: serverSettings.gemini_model || SETTINGS_DEFAULTS.geminiModel,
                    debugMode: serverSettings.debug_mode ?? SETTINGS_DEFAULTS.debugMode,
                    fontScale: serverSettings.font_scale ?? SETTINGS_DEFAULTS.fontScale,
                    groupingPower: serverSettings.grouping_power ?? SETTINGS_DEFAULTS.groupingPower,
                    translatedTextAlignment: serverSettings.translated_text_alignment ?? SETTINGS_DEFAULTS.translatedTextAlignment,
                    translatedTextFontFamily: serverSettings.translated_text_font_family ?? SETTINGS_DEFAULTS.translatedTextFontFamily,
                    translatedTextFontStyle: serverSettings.translated_text_font_style ?? SETTINGS_DEFAULTS.translatedTextFontStyle,
                    hideIdenticalTranslations: serverSettings.hide_identical_translations ?? SETTINGS_DEFAULTS.hideIdenticalTranslations,
                    allowLabelGrowth: serverSettings.allow_label_growth ?? SETTINGS_DEFAULTS.allowLabelGrowth,
                    customRecognitionSettings: serverSettings.custom_recognition_settings ?? SETTINGS_DEFAULTS.customRecognitionSettings,
                    translationCacheEnabled: serverSettings.translation_cache_enabled ?? SETTINGS_DEFAULTS.translationCacheEnabled,
                };

                // Update settings in context
                dispatch({ type: 'INITIALIZE_SETTINGS', settings: mappedSettings });

                // Update logic instance with settings
                logic.setInputLanguage(serverSettings.input_language ?? SETTINGS_DEFAULTS.inputLanguage);
                logic.setTargetLanguage(serverSettings.target_language ?? SETTINGS_DEFAULTS.targetLanguage);
                logic.setInputMode(serverSettings.input_mode ?? SETTINGS_DEFAULTS.inputMode);
                logic.setEnabled(serverSettings.enabled ?? SETTINGS_DEFAULTS.enabled);
                logic.setHoldTimeTranslate(serverSettings.hold_time_translate ?? SETTINGS_DEFAULTS.holdTimeTranslate);
                logic.setHoldTimeDismiss(serverSettings.hold_time_dismiss ?? SETTINGS_DEFAULTS.holdTimeDismiss);
                logic.setConfidenceThreshold(serverSettings.confidence_threshold || SETTINGS_DEFAULTS.confidenceThreshold); // `||`: see above
                logic.setPauseGameOnOverlay(serverSettings.pause_game_on_overlay ?? SETTINGS_DEFAULTS.pauseGameOnOverlay); // Set pause on overlay setting
                logic.setQuickToggleEnabled(serverSettings.quick_toggle_enabled ?? SETTINGS_DEFAULTS.quickToggleEnabled); // Set quick toggle setting
                logger.setEnabled(serverSettings.debug_mode ?? SETTINGS_DEFAULTS.debugMode); // Set debug mode for logger

                // Set provider settings for upfront API key validation
                logic.setOcrProvider(serverSettings.ocr_provider || SETTINGS_DEFAULTS.ocrProvider);
                logic.setTranslationProvider(serverSettings.translation_provider || SETTINGS_DEFAULTS.translationProvider);
                logic.setHasGoogleApiKey(!!serverSettings.google_api_key);
                logic.setHasGeminiApiKey(!!serverSettings.gemini_api_key);
                logic.setHasYandexApiKey(!!serverSettings.yandex_api_key);
                logic.setHasYandexFolderId(!!serverSettings.yandex_folder_id);
                logic.setHasGigachatApiKey(!!serverSettings.gigachat_api_key);

                logic.setFontScale(serverSettings.font_scale ?? SETTINGS_DEFAULTS.fontScale);
                logic.setGroupingPower(serverSettings.grouping_power ?? SETTINGS_DEFAULTS.groupingPower);
                logic.setTranslatedTextAlignment(serverSettings.translated_text_alignment ?? SETTINGS_DEFAULTS.translatedTextAlignment);
                logic.setTranslatedTextFontFamily(serverSettings.translated_text_font_family ?? SETTINGS_DEFAULTS.translatedTextFontFamily);
                logic.setTranslatedTextFontStyle(serverSettings.translated_text_font_style ?? SETTINGS_DEFAULTS.translatedTextFontStyle);
                logic.setHideIdenticalTranslations(serverSettings.hide_identical_translations ?? SETTINGS_DEFAULTS.hideIdenticalTranslations);
                logic.setAllowLabelGrowth(serverSettings.allow_label_growth ?? SETTINGS_DEFAULTS.allowLabelGrowth);

                logger.info('SettingsContext', 'All settings loaded successfully');
                logger.logObject('SettingsContext', 'Settings', maskSettingsForLog(mappedSettings));
            } else {
                logger.error('SettingsContext', 'Failed to load settings');
            }
        } catch (error) {
            logger.error('SettingsContext', 'Error loading settings', error);
        } finally {
            dispatch({ type: 'SET_INITIALIZED', initialized: true });
        }
    };

    // Update a single setting
    const updateSetting = async (key: keyof Settings, value: any, label?: string): Promise<boolean> => {
        try {
            // Update local state
            dispatch({ type: 'UPDATE_SETTING', key, value });

            // Map frontend setting key to backend setting key
            const backendKeyMap: Record<keyof Settings, string> = {
                inputLanguage: 'input_language',
                targetLanguage: 'target_language',
                inputMode: 'input_mode',
                enabled: 'enabled',
                initialized: 'initialized',
                holdTimeTranslate: 'hold_time_translate',
                holdTimeDismiss: 'hold_time_dismiss',
                confidenceThreshold: 'confidence_threshold',
                rapidocrConfidence: 'rapidocr_confidence',
                rapidocrBoxThresh: 'rapidocr_box_thresh',
                rapidocrUnclipRatio: 'rapidocr_unclip_ratio',
                rapidocrPersistentMode: 'rapidocr_persistent_mode',
                chromeScreenAiPersistentMode: 'chromescreenai_persistent_mode',
                ct2PersistentMode: 'ct2_persistent_mode',
                pauseGameOnOverlay: 'pause_game_on_overlay',
                quickToggleEnabled: 'quick_toggle_enabled',
                useFreeProviders: 'use_free_providers',
                ocrProvider: 'ocr_provider',
                translationProvider: 'translation_provider',
                googleApiKey: 'google_api_key',
                geminiApiKey: 'gemini_api_key',
                yandexApiKey: 'yandex_api_key',
                yandexFolderId: 'yandex_folder_id',
                gigachatApiKey: 'gigachat_api_key',
                myMemoryEmail: 'mymemory_email',
                geminiModel: 'gemini_model',
                debugMode: 'debug_mode',
                fontScale: 'font_scale',
                groupingPower: 'grouping_power',
                translatedTextAlignment: 'translated_text_alignment',
                translatedTextFontFamily: 'translated_text_font_family',
                translatedTextFontStyle: 'translated_text_font_style',
                hideIdenticalTranslations: 'hide_identical_translations',
                allowLabelGrowth: 'allow_label_growth',
                customRecognitionSettings: 'custom_recognition_settings',
                translationCacheEnabled: 'translation_cache_enabled'
            };

            // Skip settings that don't need to be saved to backend
            if (key === 'initialized') return true;

            const backendKey = backendKeyMap[key];

            // Update logic based on setting type
            switch (key) {
                case 'inputLanguage':
                    logic.setInputLanguage(value);
                    break;
                case 'targetLanguage':
                    logic.setTargetLanguage(value);
                    break;
                case 'inputMode':
                    logic.setInputMode(value);
                    break;
                case 'enabled':
                    logic.setEnabled(value);
                    break;
                case 'holdTimeTranslate':
                    logic.setHoldTimeTranslate(value);
                    break;
                case 'holdTimeDismiss':
                    logic.setHoldTimeDismiss(value);
                    break;
                case 'confidenceThreshold':
                    logic.setConfidenceThreshold(value);
                    break;
                case 'pauseGameOnOverlay':
                    logic.setPauseGameOnOverlay(value);
                    break;
                case 'quickToggleEnabled':
                    logic.setQuickToggleEnabled(value);
                    break;
                case 'debugMode':
                    logger.setEnabled(value);
                    break;
                case 'fontScale':
                    logic.setFontScale(value);
                    break;
                case 'groupingPower':
                    logic.setGroupingPower(value);
                    break;
                case 'translatedTextAlignment':
                    logic.setTranslatedTextAlignment(value);
                    break;
                case 'translatedTextFontFamily':
                    logic.setTranslatedTextFontFamily(value);
                    break;
                case 'translatedTextFontStyle':
                    logic.setTranslatedTextFontStyle(value);
                    break;
                case 'hideIdenticalTranslations':
                    logic.setHideIdenticalTranslations(value);
                    break;
                case 'allowLabelGrowth':
                    logic.setAllowLabelGrowth(value);
                    break;
                case 'ocrProvider':
                    logic.setOcrProvider(value);
                    break;
                case 'translationProvider':
                    logic.setTranslationProvider(value);
                    break;
                case 'googleApiKey':
                    logic.setHasGoogleApiKey(!!value);
                    break;
                case 'geminiApiKey':
                    logic.setHasGeminiApiKey(!!value);
                    break;
                case 'yandexApiKey':
                    logic.setHasYandexApiKey(!!value);
                    break;
                case 'yandexFolderId':
                    logic.setHasYandexFolderId(!!value);
                    break;
                case 'gigachatApiKey':
                    logic.setHasGigachatApiKey(!!value);
                    break;
            }

            // Save to backend
            const result = await call<[string, any], boolean>('set_setting', backendKey, value);

            if (result) {
                // if (label) logic.notify(`${label} updated successfully`);
                return true;
            } else {
                logic.notify(`Не удалось обновить: ${label || key}`, 2000);
                return false;
            }
        } catch (error) {
            logger.error('SettingsContext', `Failed to update ${key}`, error);
            logic.notify(`Не удалось обновить: ${label || key}`, 2000);
            return false;
        }
    };

    // Initialize settings on mount
    useEffect(() => {
        loadAllSettings();
    }, []);

    return (
        <SettingsContext.Provider value={{
            settings,
            updateSetting,
            initialized: settings.initialized
        }}>
            {children}
        </SettingsContext.Provider>
    );
};

// Create a hook for using the settings
export const useSettings = () => {
    const context = useContext(SettingsContext);
    if (!context) {
        throw new Error('useSettings must be used within a SettingsProvider');
    }
    return context;
};