// Translator.tsx - Handles translator logic and API interactions

import { call, toaster } from "@decky/api";
import { Router } from "@decky/ui";
import { TextRecognizer, NetworkError, ApiKeyError, RateLimitError, ModelNotAvailableError, TranslationError } from "./TextRecognizer";
import { TextTranslator } from "./TextTranslator";
import { Input, InputMode, ActionType, ProgressInfo } from "./Input";
import { ImageState } from "./Overlay";
import { OCR_DATA_FLOW, TRANSLATION_DATA_FLOW, mostExposed } from "./tabs/DataFlowBadge";
import type { DataFlow } from "./tabs/DataFlowBadge";
import { logger } from "./Logger";
import { SETTINGS_DEFAULTS } from "./settingsDefaults";
import { fetchReadiness, readinessToastTitle, readinessToastBody } from "./Readiness";

// Screenshot response interface
export interface ScreenshotResponse {
    path: string;
    base64: string;
}

// Extra wait after the frames below, for gamescope to drop Steam's layer and present a
// clean game frame. Picked by hand: if the indicator still shows up on screenshots, raise it
const INDICATOR_HIDE_DELAY_MS = 100;

// The hold indicator lives in Steam's UI layer above the game. Hiding it is not instant:
// React re-renders it, its composition request is removed, then gamescope recomposes.
// Two animation frames let React commit and paint; the delay covers the gamescope side,
// which this JS context cannot observe
const waitForNextFrame = (): Promise<void> =>
    new Promise(resolve => requestAnimationFrame(() => resolve()));

const waitForIndicatorToLeaveScreen = async (): Promise<void> => {
    await waitForNextFrame();
    await waitForNextFrame();
    await new Promise<void>(resolve => setTimeout(resolve, INDICATOR_HIDE_DELAY_MS));
};

// Main app logic
export class GameTranslatorLogic {
    private isProcessing = false;
    // Идёт предпроверка хоткея (check_readiness) — см. translateFromHotkey.
    private isCheckingReadiness = false;
    public imageState: ImageState;
    private textRecognizer: TextRecognizer;
    private textTranslator: TextTranslator;
    private shortcutInput: Input; // Added shortcut input handler
    private progressListeners: Array<(progressInfo: ProgressInfo) => void> = [];
    private enabled: boolean = SETTINGS_DEFAULTS.enabled;
    private confidenceThreshold: number = SETTINGS_DEFAULTS.confidenceThreshold;
    private pauseGameOnOverlay: boolean = SETTINGS_DEFAULTS.pauseGameOnOverlay;
    private hideIdenticalTranslations: boolean = SETTINGS_DEFAULTS.hideIdenticalTranslations;
    private currentRunId: number = 0;

    // Provider settings for upfront validation
    private ocrProvider: string = SETTINGS_DEFAULTS.ocrProvider;
    private translationProvider: string = SETTINGS_DEFAULTS.translationProvider;
    private hasGoogleApiKey: boolean = false;
    private hasGeminiApiKey: boolean = false;
    private hasYandexApiKey: boolean = false;
    private hasYandexFolderId: boolean = false;
    private hasGigachatApiKey: boolean = false;

    isOverlayVisible(): boolean {
        return this.imageState.isVisible();
    }

    // Add public access to shortcutInput for diagnostics
    public get shortcutInputHandler(): Input {
        return this.shortcutInput;
    }

    constructor(imageState: ImageState) {
        this.imageState = imageState;
        this.textRecognizer = new TextRecognizer();
        this.textTranslator = new TextTranslator();

        // Initialize for hidraw-based button detection
        this.shortcutInput = new Input();

        // Set up listener for translate, dismiss, and toggle actions
        this.shortcutInput.onShortcutPressed((actionType: ActionType) => {
            // Only process inputs if the plugin is enabled
            if (!this.enabled) return;

            if (actionType === ActionType.DISMISS) {
                this.dismiss();
            } else if (actionType === ActionType.TOGGLE_TRANSLATIONS) {
                // Toggle translations action
                if (this.imageState.isVisible() && !this.imageState.isLoading()) {
                    logger.debug('Translator', 'Toggling translation visibility');
                    this.imageState.toggleTranslationsVisibility();
                }
            } else {
                this.translateFromHotkey().catch(err => logger.error('Translator', 'Hotkey translation failed', err));
            }
        });

        imageState.onStateChanged((visible, _, __, ___, ____, _____, ______, _______, ________) => {
            this.shortcutInput.setOverlayVisible(visible);

            if (!this.enabled) return;

            if (this.pauseGameOnOverlay && !visible) {
                this.resumeCurrentGame();
            }
        });

        // Set up progress listener
        this.shortcutInput.onProgress((progressInfo: ProgressInfo) => {
            this.notifyProgressListeners(progressInfo);
        });

        // Load enabled state from server
        this.loadInitialState();
    }

    // Load initial state from server
    private async loadInitialState() {
        try {
            const result = await call<[], boolean>('get_enabled_state');
            this.enabled = !!result;
            logger.info('Translator', `Loaded initial enabled state: ${this.enabled}`);

            if (this.shortcutInput) {
                this.shortcutInput.setEnabled(this.enabled);
            }

            // If plugin starts disabled, stop the hidraw monitor that was auto-started
            if (!this.enabled) {
                logger.info('Translator', 'Plugin is disabled on startup, stopping hidraw monitor');
                call('stop_hidraw_monitor').catch(error => {
                    logger.error('Translator', 'Failed to stop hidraw monitor on startup', error);
                });
            }
        } catch (error) {
            logger.error('Translator', 'Failed to load initial state', error);
        }
    }

    // Add method to enable/disable the plugin
    setEnabled(enabled: boolean): void {
        this.enabled = enabled;

        if (this.shortcutInput) {
            this.shortcutInput.setEnabled(enabled);
        }

        // Save to server settings file
        call('set_setting', 'enabled', enabled).catch(error => {
            logger.error('Translator', 'Failed to save enabled state to server', error);
        });

        if (!enabled) {
            this.dismiss();
        }

        // Stop or start the backend hidraw monitor based on enabled state
        if (enabled) {
            // Re-start hidraw monitor when re-enabling
            call('start_hidraw_monitor').then(result => {
                logger.info('Translator', `Hidraw monitor start result: ${JSON.stringify(result)}`);
            }).catch(error => {
                logger.error('Translator', 'Failed to start hidraw monitor', error);
            });
        } else {
            // Stop hidraw monitor when disabling to save resources
            call('stop_hidraw_monitor').then(result => {
                logger.info('Translator', `Hidraw monitor stop result: ${JSON.stringify(result)}`);
            }).catch(error => {
                logger.error('Translator', 'Failed to stop hidraw monitor', error);
            });
        }
    }

    // Add method to get enabled state
    isEnabled(): boolean {
        return this.enabled;
    }

    // Method to get full diagnostic information
    getInputDiagnostics(): object | null {
        if (!this.shortcutInput) return null;
        return this.shortcutInput.getDiagnostics();
    }

    // New methods for confidence threshold
    setConfidenceThreshold(threshold: number): void {
        logger.debug('Translator', `Setting confidence threshold to: ${threshold}`);
        this.confidenceThreshold = threshold;

        // Update the textRecognizer with the new threshold
        this.textRecognizer.setConfidenceThreshold(threshold);
    }

    getConfidenceThreshold(): number {
        return this.confidenceThreshold;
    }

    setGroupingPower = (power: number): void => {
        this.textRecognizer.setGroupingPower(power);
    }

    setHideIdenticalTranslations = (enabled: boolean): void => {
        this.hideIdenticalTranslations = enabled;
    }

    setPauseGameOnOverlay = (enabled: boolean): void => {
        logger.debug('Translator', `Setting pauseGameOnOverlay to: ${enabled}`);
        this.pauseGameOnOverlay = enabled;

        // If overlay is currently visible and we're enabling this setting, pause the game
        if (enabled && this.imageState.isVisible()) {
            this.pauseCurrentGame();
        }
    }

    // Method to get pause game on overlay state
    getPauseGameOnOverlay = (): boolean => {
        return this.pauseGameOnOverlay;
    }

    // Method to pause the current game
    async pauseCurrentGame(): Promise<void> {
        try {
            // Get the current running app ID
            const mainApp = Router.MainRunningApp;
            if (!mainApp || !mainApp.appid) {
                logger.debug('Translator', 'No game running to pause');
                return;
            }

            // Use the pid_from_appid function to get the process ID
            const pid = await call<[number], number>('pid_from_appid', Number(mainApp.appid));

            if (pid) {
                logger.info('Translator', `Pausing game with appid ${mainApp.appid}, pid ${pid}`);

                // Call the pause function in the backend
                const pauseResult = await call<[number], boolean>('pause', pid);
                if (pauseResult) {
                    logger.info('Translator', 'Game paused successfully');
                } else {
                    logger.error('Translator', 'Failed to pause game');
                }
            } else {
                logger.error('Translator', 'Failed to get PID for game');
            }
        } catch (error) {
            logger.error('Translator', 'Error pausing game', error);
        }
    }

    // Method to resume the current game
    async resumeCurrentGame(): Promise<void> {
        try {
            // Get the current running app ID
            const mainApp = Router.MainRunningApp;
            if (!mainApp || !mainApp.appid) {
                logger.debug('Translator', 'No game running to resume');
                return;
            }

            // Use the pid_from_appid function to get the process ID
            const pid = await call<[number], number>('pid_from_appid', Number(mainApp.appid));

            if (pid) {
                logger.info('Translator', `Resuming game with appid ${mainApp.appid}, pid ${pid}`);

                // Call the resume function in the backend
                const resumeResult = await call<[number], boolean>('resume', pid);
                if (resumeResult) {
                    logger.info('Translator', 'Game resumed successfully');
                } else {
                    logger.error('Translator', 'Failed to resume game');
                }
            } else {
                logger.error('Translator', 'Failed to get PID for game');
            }
        } catch (error) {
            logger.error('Translator', 'Error resuming game', error);
        }
    }

    // Methods for progress indicator
    onProgress(callback: (progressInfo: ProgressInfo) => void): void {
        this.progressListeners.push(callback);
    }

    offProgress(callback: (progressInfo: ProgressInfo) => void): void {
        const index = this.progressListeners.indexOf(callback);
        if (index !== -1) {
            this.progressListeners.splice(index, 1);
        }
    }

    private notifyProgressListeners(progressInfo: ProgressInfo): void {
        for (const callback of this.progressListeners) {
            callback(progressInfo);
        }
    }

    // Clean up resources when plugin is unmounted
    cleanup(): void {
        if (this.shortcutInput) {
            this.shortcutInput.unregister();
        }

        // Stop backend hidraw monitor
        call('stop_hidraw_monitor').catch(error => {
            logger.error('Translator', 'Failed to stop hidraw monitor', error);
        });
    }

    notify = async (message: string, duration: number = 1000, body?: string): Promise<void> => {
        toaster.toast({
            title: message,
            body: body || message,
            duration: duration,
            critical: true
        });
    }

    dismiss = (): void => {
        this.currentRunId++;
        this.isProcessing = false;
        if (this.imageState.isVisible()) {
            this.imageState.hideImage();
            this.shortcutInput.setOverlayVisible(false);
        }
    }

    // Хоткей «перевести». Предпроверка идёт до паузы и скриншота: если модель
    // не скачана, незачем замораживать игру и снимать экран.
    private translateFromHotkey = async (): Promise<void> => {
        if (this.imageState.isVisible()) return;
        if (this.isProcessing) return;
        // Предпроверка ходит в бэкенд, это доли секунды. Повторное нажатие
        // за это время не должно запустить вторую паузу и второй снимок:
        // isProcessing ставится только в captureAndTranslate, позже.
        if (this.isCheckingReadiness) return;

        this.isCheckingReadiness = true;
        const canStart = await this.canStartTranslation()
            .finally(() => { this.isCheckingReadiness = false; });
        if (!canStart) return;

        // Пока шла предпроверка, всё могло измениться: плагин выключили или
        // запустился перевод с кнопки на главной. Без этой перепроверки игра
        // встала бы на паузу, а снимать её с паузы было бы некому.
        if (!this.enabled || this.isProcessing || this.imageState.isVisible()) return;

        // Pause first so the game freezes before the screenshot
        if (this.pauseGameOnOverlay) {
            this.pauseCurrentGame().catch(err => logger.error('Translator', 'Pause failed', err));
        }
        await this.captureAndTranslate();
    }

    // Кнопка «Перевести» на главной: та же предпроверка, без паузы
    // (как было и раньше).
    takeScreenshotAndTranslate = async (): Promise<void> => {
        if (this.isProcessing || !this.enabled) {
            logger.debug('Translator', 'Already processing a screenshot or plugin disabled, skipping');
            return;
        }
        if (!(await this.canStartTranslation())) return;
        await this.captureAndTranslate();
    }

    // Скриншот, распознавание и перевод. Предпроверки — у вызывающих
    // (translateFromHotkey / takeScreenshotAndTranslate): у хоткея между ними
    // и снимком стоит пауза игры.
    private captureAndTranslate = async (): Promise<void> => {
        // If already processing or disabled, return
        if (this.isProcessing || !this.enabled) {
            logger.debug('Translator', 'Already processing a screenshot or plugin disabled, skipping');
            return;
        }

        const runId = ++this.currentRunId;
        const isCancelled = (): boolean => runId !== this.currentRunId;
        // The most sensitive data flow among the stages this run has started.
        // Final statuses ("Текст не найден", errors) show it as one icon: the user's
        // question there is "did anything of mine leave the device?", and the last
        // stage alone can answer wrong (cloud OCR sent a screenshot, then local
        // translation failed). `let`: it accumulates per stage and is read in catch.
        let runDataFlow: DataFlow | null = null;

        try {
            this.isProcessing = true;

            // Take screenshot FIRST while screen is clean (no overlay visible)
            const appName = Router.MainRunningApp?.display_name || "";
            // Steam appid: the key of the game's saved GigaChat+ glossary. Non-Steam
            // shortcuts (GOG, Epic...) get their own appid from Steam too, stable while
            // the shortcut exists; the display name is not a good key, the user can
            // rename a shortcut and some titles depend on the Steam client language.
            // String(): @decky/ui declares appid a string, but Steam's other app
            // types use numbers; the backend expects text either way.
            const appId = String(Router.MainRunningApp?.appid ?? "");
            logger.info('Translator', `Taking new screenshot for: ${appName} (appid ${appId})`);
            await waitForIndicatorToLeaveScreen();
            const result = await call<[string, string], ScreenshotResponse>('take_screenshot', appName, appId);

            if (isCancelled()) {
                logger.debug('Translator', 'Translation cancelled before screenshot processed');
                return;
            }

            if (!result || !result.path || !result.base64) {
                logger.warn('Translator', 'Screenshot capture failed, not opening overlay');
                this.notify('Не удалось захватить экран', 2500, 'Попробуйте нажать комбинацию ещё раз');
                return;
            }

            logger.debug('Translator', `Screenshot captured, path: ${result.path}, base64 length: ${result.base64.length}`);

            this.imageState.startLoading("Обработка");
            this.imageState.showImage(result.base64);
            const recognizingStep = this.ocrProvider === 'gemini_vision'
                ? "Распознавание и перевод"
                : "Распознавание";
            const ocrFlow = this.ocrDataFlow();
            runDataFlow = mostExposed(runDataFlow, ocrFlow);
            this.imageState.updateProcessingStep(recognizingStep, false, this.ocrMethodHint(), true, ocrFlow);

            const textRegions = await this.textRecognizer.recognizeTextFile(result.path);
            if (isCancelled()) {
                logger.debug('Translator', 'Translation cancelled after OCR');
                return;
            }
            logger.info('Translator', `Found ${textRegions.length} text regions`);

            if (textRegions.length > 0) {
                const alreadyTranslated = textRegions.every(r => r.translatedText);
                if (!alreadyTranslated) {
                    const translationFlow = this.translationDataFlow();
                    runDataFlow = mostExposed(runDataFlow, translationFlow);
                    this.imageState.updateProcessingStep("Перевод текста", false, this.translationMethodHint(), true, translationFlow);
                }

                // Translate text (skips backend call if already translated by OCR provider)
                let translatedRegions = await this.textTranslator.translateText(textRegions);
                if (isCancelled()) {
                    logger.debug('Translator', 'Translation cancelled after translation step');
                    return;
                }
                logger.info('Translator', `Translation complete: ${translatedRegions.length} regions`);

                if (this.hideIdenticalTranslations) {
                    const before = translatedRegions.length;
                    translatedRegions = translatedRegions.filter(r =>
                        r.translatedText.trim().toLowerCase() !== r.text.trim().toLowerCase()
                    );
                    if (translatedRegions.length < before) {
                        logger.info('Translator', `Filtered ${before - translatedRegions.length} identical translations`);
                    }
                }

                this.imageState.showTranslatedImage(result.base64, translatedRegions);
            } else {
                // No text found, show message
                this.imageState.updateProcessingStep("Текст не найден", false, "", false, runDataFlow);

                // Hide overlay after a short delay
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 2000); // 2 seconds delay
            }
        } catch (error) {
            if (isCancelled()) {
                logger.debug('Translator', 'Translation cancelled, suppressing error UI', error);
                return;
            }

            logger.error('Translator', 'Screenshot and translation error', error);

            // Check if this is a network error
            if (error instanceof NetworkError) {
                const msg = error.message || "Нет подключения к интернету";
                this.imageState.updateProcessingStep(msg, true, "", true, runDataFlow);
                // Hide overlay after showing the error message
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 2500); // 2.5 seconds delay for network error
            } else if (error instanceof ApiKeyError) {
                this.imageState.updateProcessingStep(error.message || "Неверный API-ключ", true, "", true, runDataFlow);
                // Hide overlay after showing the error message
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 2500); // 2.5 seconds delay for API key error
            } else if (error instanceof ModelNotAvailableError) {
                this.imageState.updateProcessingStep(error.message, true, "", true, runDataFlow);
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 3000);
            } else if (error instanceof RateLimitError) {
                this.imageState.updateProcessingStep(error.message, true, "", true, runDataFlow);
                // Hide overlay after showing the error message
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 3000); // 3 seconds delay for rate limit error
            } else if (error instanceof TranslationError) {
                this.imageState.updateProcessingStep(error.message, true, "", true, runDataFlow);
                setTimeout(() => {
                    this.imageState.hideImage();
                }, 3000); // longer: the message may name the exception to report
            } else {
                this.imageState.hideImage();
            }
        }
        finally {
            if (!isCancelled()) {
                this.isProcessing = false;
            }
        }
    }

    setInputLanguage = (language: string): void => {
        this.textTranslator.setInputLanguage(language);
    }

    getInputLanguage = (): string => {
        return this.textTranslator.getInputLanguage();
    }

    setTargetLanguage = (language: string): void => {
        this.textTranslator.setTargetLanguage(language);
    }

    getTargetLanguage = (): string => {
        return this.textTranslator.getTargetLanguage();
    }

    // Method to set input mode
    setInputMode = (mode: InputMode): void => {
        this.shortcutInput.setInputMode(mode);
    }

    // Method to get current input mode
    getInputMode = (): InputMode => {
        return this.shortcutInput.getInputMode();
    }

    // Method to set translation hold time
    setHoldTimeTranslate = (ms: number): void => {
        if (this.shortcutInput) {
            this.shortcutInput.setTranslateHoldTime(ms);
        }
    }

    // Method to get translation hold time
    getHoldTimeTranslate = (): number => {
        return this.shortcutInput ? this.shortcutInput.getTranslateHoldTime() : SETTINGS_DEFAULTS.holdTimeTranslate;
    }

    // Method to set dismiss hold time
    setHoldTimeDismiss = (ms: number): void => {
        if (this.shortcutInput) {
            this.shortcutInput.setDismissHoldTime(ms);
        }
    }

    // Method to get dismiss hold time
    getHoldTimeDismiss = (): number => {
        return this.shortcutInput ? this.shortcutInput.getDismissHoldTime() : SETTINGS_DEFAULTS.holdTimeDismiss;
    }

    // Method to set quick toggle enabled
    setQuickToggleEnabled = (enabled: boolean): void => {
        if (this.shortcutInput) {
            this.shortcutInput.setQuickToggleEnabled(enabled);
        }
    }

    // Method to get quick toggle enabled state
    getQuickToggleEnabled = (): boolean => {
        return this.shortcutInput ? this.shortcutInput.getQuickToggleEnabled() : false;
    }

    setFontScale = (scale: number): void => {
        this.imageState.setFontScale(scale);
    }

    setAllowLabelGrowth = (allow: boolean): void => {
        this.imageState.setAllowLabelGrowth(allow);
    }

    setTranslatedTextAlignment = (alignment: 'left' | 'right' | 'center' | 'justify'): void => {
        this.imageState.setTranslatedTextAlignment(alignment);
    }

    setTranslatedTextFontFamily = (fontFamily: string): void => {
        this.imageState.setTranslatedTextFontFamily(fontFamily);
    }

    setTranslatedTextFontStyle = (style: 'normal' | 'bold' | 'italic' | 'bolditalic'): void => {
        this.imageState.setTranslatedTextFontStyle(style);
    }

    // Methods for provider settings (used for upfront API key validation)
    setOcrProvider = (provider: string): void => {
        this.ocrProvider = provider;
        logger.debug('Translator', `OCR provider set to: ${provider}`);
    }

    setTranslationProvider = (provider: string): void => {
        this.translationProvider = provider;
        logger.debug('Translator', `Translation provider set to: ${provider}`);
    }

    private translationMethodHint(): string {
        const labels: Record<string, string> = {
            ct2: "Локально (NLLB)",
            opusmt: "Локально (лёгкая, en→ru)",
            freegoogle: "Google Translate",
            mymemory: "MyMemory",
            googlecloud: "Google Cloud",
            yandex: "Яндекс",
            gigachat: "GigaChat",
            gigachat_plus: "GigaChat+",
        };
        return labels[this.translationProvider] || this.translationProvider;
    }

    private ocrMethodHint(): string {
        const labels: Record<string, string> = {
            rapidocr: "Локально (RapidOCR)",
            chromescreenai: "Локально (Chrome)",
            ocrspace: "OCR.space",
            googlecloud: "Google Cloud",
            gemini_vision: "Gemini Vision",
        };
        return labels[this.ocrProvider] || this.ocrProvider;
    }

    // ocrProvider / translationProvider are plain strings here, not the Settings
    // unions, so the lookup can miss (e.g. a stale value from an old settings
    // file). A miss means "no icon" rather than a wrong one.
    private ocrDataFlow(): DataFlow | null {
        const flows: Record<string, DataFlow> = OCR_DATA_FLOW;
        return flows[this.ocrProvider] ?? null;
    }

    private translationDataFlow(): DataFlow | null {
        const flows: Record<string, DataFlow> = TRANSLATION_DATA_FLOW;
        return flows[this.translationProvider] ?? null;
    }

    setHasGoogleApiKey = (hasKey: boolean): void => {
        this.hasGoogleApiKey = hasKey;
        logger.debug('Translator', `Google API key available: ${hasKey}`);
    }

    setHasGeminiApiKey = (hasKey: boolean): void => {
        this.hasGeminiApiKey = hasKey;
        logger.debug('Translator', `Gemini API key available: ${hasKey}`);
    }

    setHasYandexApiKey = (hasKey: boolean): void => {
        this.hasYandexApiKey = hasKey;
        logger.debug('Translator', `Yandex API key available: ${hasKey}`);
    }

    setHasYandexFolderId = (hasFolderId: boolean): void => {
        this.hasYandexFolderId = hasFolderId;
        logger.debug('Translator', `Yandex folder ID available: ${hasFolderId}`);
    }

    setHasGigachatApiKey = (hasKey: boolean): void => {
        this.hasGigachatApiKey = hasKey;
        logger.debug('Translator', `GigaChat Authorization Key available: ${hasKey}`);
    }

    // Runs all pre-checks that would prevent a translation from starting.
    // Returns false and shows a toast if any check fails.
    // Сначала дешёвые проверки без бэкенда (языки, ключи), потом готовность
    // моделей — её знает только бэкенд.
    private async canStartTranslation(): Promise<boolean> {
        const inputLang = this.getInputLanguage();
        const targetLang = this.getTargetLanguage();
        if (!inputLang && targetLang) {
            logger.warn('Translator', 'Cannot start translation: languages not configured');
            this.notify("Исходный язык не выбран", 3000, "Выберите его в настройках плагина");
            return false;
        }
        if (!targetLang && inputLang) {
            logger.warn('Translator', 'Cannot start translation: languages not configured');
            this.notify("Язык перевода не выбран", 3000, "Выберите его в настройках плагина");
            return false;
        }
        if (!inputLang && !targetLang) {
            logger.warn('Translator', 'Cannot start translation: languages not configured');
            this.notify("Язык перевода и исходный язык не выбраны", 3000, "Выберите их в настройках плагина");
            return false;
        }

        if (inputLang !== 'auto' && inputLang === targetLang) {
            logger.warn('Translator', `Cannot start translation: input and output language are both ${inputLang}`);
            this.notify("Исходный и целевой языки не могут совпадать", 3000, "Измените их в настройках плагина");
            return false;
        }

        const apiKeyCheck = this.requiresApiKeyButMissing();
        if (apiKeyCheck.missing) {
            logger.warn('Translator', `Cannot start translation: ${apiKeyCheck.message}`);
            this.notify(apiKeyCheck.message, 3000, "Настройте API-ключ на вкладке настроек перевода.");
            return false;
        }

        const readiness = await fetchReadiness(inputLang, targetLang);
        if (!readiness.ready) {
            logger.warn('Translator', `Cannot start translation: ${readiness.reason} - ${readiness.message}`);
            // 5 с, а не 3, как у остальных: в тексте размер модели и что делать,
            // а в игре человек читает тост краем глаза.
            this.notify(readinessToastTitle(readiness), 5000, readinessToastBody(readiness));
            return false;
        }

        return true;
    }

    // Check if the current provider configuration requires an API key that's missing
    private requiresApiKeyButMissing(): { missing: boolean; message: string } {
        if (this.ocrProvider === 'gemini_vision' && !this.hasGeminiApiKey) {
            return { missing: true, message: "Для Gemini Vision требуется API-ключ Gemini" };
        }

        const ocrNeedsKey = this.ocrProvider === 'googlecloud';
        const translationNeedsKey = this.translationProvider === 'googlecloud';

        if ((ocrNeedsKey || translationNeedsKey) && !this.hasGoogleApiKey) {
            if (ocrNeedsKey && translationNeedsKey) {
                return { missing: true, message: "Требуется API-ключ для распознавания и перевода" };
            } else if (ocrNeedsKey) {
                return { missing: true, message: "Требуется API-ключ для распознавания" };
            } else {
                return { missing: true, message: "Требуется API-ключ для перевода" };
            }
        }

        if (this.translationProvider === 'yandex' && !(this.hasYandexApiKey && this.hasYandexFolderId)) {
            return { missing: true, message: "Для Яндекс Перевода требуются API-ключ и folder_id" };
        }

        // Оба метода GigaChat работают с одним и тем же ключом.
        const usesGigachatKey = this.translationProvider === 'gigachat'
            || this.translationProvider === 'gigachat_plus';
        if (usesGigachatKey && !this.hasGigachatApiKey) {
            return { missing: true, message: "Для GigaChat требуется Authorization Key" };
        }

        return { missing: false, message: "" };
    }
}
