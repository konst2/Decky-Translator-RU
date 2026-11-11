import { call } from "@decky/api";
import { logger } from "./Logger";
import { SETTINGS_DEFAULTS } from "./settingsDefaults";
import { TextGlue } from "./glue/TextGlue";

export interface ErrorResponse {
    error: string;
    message: string;
}

export class NetworkError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'NetworkError';
    }
}

export class ApiKeyError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'ApiKeyError';
    }
}

export class RateLimitError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'RateLimitError';
    }
}

export class ModelNotAvailableError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'ModelNotAvailableError';
    }
}

// Any other translation failure. Shown to the user instead of silently
// falling back to the original text, which looked like a successful
// translation.
export class TranslationError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'TranslationError';
    }
}

function isErrorResponse(value: unknown): value is ErrorResponse {
    return typeof value === 'object' && value !== null && 'error' in value && 'message' in value;
}

export interface TextRegion {
    text: string;
    rect: {
        left: number;
        top: number;
        right: number;
        bottom: number;
    };
    isDialog: boolean;
    confidence?: number;
    textColor?: string;
    bgColor?: number[];  // [R, G, B] average background color from screenshot
    fontSize?: number;
    textDensity?: number;
    textContext?: string;
    lineNumber?: number;
    indent?: number;
    typographyType?: string;
    alignment?: string;
    translatedText?: string;  // Pre-translated text from combined OCR+translation providers
    blockId?: number;         // Screen AI layout id of the visual block (Chrome Screen AI only)
}

export class TextRecognizer {
    private confidenceThreshold: number = SETTINGS_DEFAULTS.confidenceThreshold;
    private glue = new TextGlue(lines => this.sendBlockAttachLog(lines));

    constructor() {
        logger.info('TextRecognizer', 'TextRecognizer initialized');
    }

    setConfidenceThreshold(threshold: number): void {
        this.confidenceThreshold = threshold;
    }

    getConfidenceThreshold(): number {
        return this.confidenceThreshold;
    }

    setGroupingPower(power: number): void {
        this.glue.setGroupingPower(power);
    }

    getGroupingPower(): number {
        return this.glue.getGroupingPower();
    }

    // One call per screen, not awaited: logging must not slow down or break
    // recognition, so a failed call is only noted in the console.
    private sendBlockAttachLog(lines: string[]): void {
        call<[string[]], void>('log_block_attach', lines).catch((error) => {
            logger.warn('TextRecognizer', `Block attach: failed to write plugin log: ${error}`);
        });
    }

    filterUntranslatableText(regions: TextRegion[]): TextRegion[] {
        if (!regions || regions.length === 0) return regions;

        logger.debug('TextRecognizer', `Filtering untranslatable text from ${regions.length} regions`);

        return regions.filter(region => {
            const text = region.text.trim();

            if (!text) return false;

            if (region.confidence !== undefined && region.confidence < this.confidenceThreshold) {
                return false;
            }

            if (text.length === 1) return false;

            // Numeric patterns
            if (
                /^\d+$/.test(text) ||
                /^[\d\/]+$/.test(text) ||
                /^[\d\s]+$/.test(text) ||
                /^[\d\s\+\-\*\/\=\(\)]+$/.test(text) ||
                /^[\d\s,\.:\/\-]+$/.test(text) ||
                /^[+-]?\s*\d+\s*%$/.test(text) ||
                /^\d{1,2}:\d{1,2}(:\d{1,2})?\s*(AM|PM|am|pm|a\.m\.|p\.m\.)?$/.test(text) ||
                /^([WDL]\d+\s*)+$|^\d+[\-:]\d+$/.test(text)
            ) {
                return false;
            }

            // Punctuation-only
            if (/^[^\p{L}\p{N}\s]+$/u.test(text) || /^[_\-\+\*\/\=\.,;:!?@#$%^&*()[\]{}|<>~`"']+$/.test(text)) {
                return false;
            }

            // Decorative separators
            if (/^[-=_*]{3,}$/.test(text) || /^[~\u2022]{3,}$/.test(text)) {
                return false;
            }

            // Very short non-words (Latin only -- CJK carries more meaning per character)
            const hasNonLatinLetter = /[^\x00-\x7F]/.test(text) && /\p{L}/u.test(text);
            if (text.length <= 3 && !hasNonLatinLetter && !/^(OK|GO|NO|YES|ON|OFF|NEW|ADD|ALL|BUY|THE|AND|FOR|TO|IN|IS|IT|BE|BY)$/i.test(text)) {
                return false;
            }

            // File extensions
            if (/^\.[a-zA-Z0-9]{2,4}$/.test(text)) return false;

            // Social media tags
            if (/^[@#][a-zA-Z0-9_]+$/.test(text)) return false;

            // URLs and emails
            if (/^(https?:\/\/|www\.|[\w.-]+@)/.test(text)) return false;

            // Game UI patterns
            if (
                /^[xX\u00D7]\d+$|^\d+[xX\u00D7]$/.test(text) ||
                /^\d+\s*\/\s*\d+$|^\d+\s+of\s+\d+$/i.test(text) ||
                /^(LVL|LEVEL)\s*\d+$/i.test(text) ||
                /^(HP|MP|SP|AP)\s*\d+$/i.test(text) ||
                /^(STR|DEX|INT|WIS|CHA|CON|AGI)\s*\d+$/i.test(text)
            ) {
                return false;
            }

            return true;
        });
    }

    async recognizeText(imageData: string): Promise<TextRegion[]> {
        try {
            const response = await call<[string], TextRegion[]>('recognize_text', imageData);

            if (response) {
                const regions = response;
                logger.info('TextRecognizer', `Got ${regions.length} raw text regions from OCR`);

                const mergedRegions = this.glue.applyAutoGlue(regions);
                const filteredRegions = this.filterUntranslatableText(mergedRegions);
                logger.info('TextRecognizer', `After filtering, ${filteredRegions.length} regions remain`);

                return filteredRegions;
            }

            logger.error('TextRecognizer', 'Failed to recognize text');
            return [];
        } catch (error) {
            logger.error('TextRecognizer', 'Text recognition error', error);
            return [];
        }
    }

    async recognizeTextFile(imagePath: string): Promise<TextRegion[]> {
        try {
            const response = await call<[string], TextRegion[] | ErrorResponse>('recognize_text_file', imagePath);
            if (response) {
                if (isErrorResponse(response)) {
                    const errorResponse = response as ErrorResponse;
                    if (errorResponse.error === 'network_error') {
                        throw new NetworkError(errorResponse.message);
                    }
                    if (errorResponse.error === 'api_key_error') {
                        throw new ApiKeyError(errorResponse.message);
                    }
                    if (errorResponse.error === 'rate_limit_error') {
                        throw new RateLimitError(errorResponse.message);
                    }
                    if (errorResponse.error === 'model_not_available') {
                        throw new ModelNotAvailableError(errorResponse.message);
                    }
                    logger.error('TextRecognizer', `Error from backend: ${errorResponse.error} - ${errorResponse.message}`);
                    return [];
                }

                const regions = response as TextRegion[];
                logger.info('TextRecognizer', `Got ${regions.length} regions from file-based OCR`);

                const mergedRegions = this.glue.applyAutoGlue(regions);
                const filteredRegions = this.filterUntranslatableText(mergedRegions);
                logger.info('TextRecognizer', `After filtering, ${filteredRegions.length} regions remain`);

                return filteredRegions;
            }
            logger.error('TextRecognizer', 'Failed to recognize text (file-based)');
            return [];
        } catch (error) {
            if (error instanceof NetworkError || error instanceof ApiKeyError || error instanceof RateLimitError || error instanceof ModelNotAvailableError) {
                throw error;
            }
            logger.error('TextRecognizer', 'Text recognition error (file-based)', error);
            return [];
        }
    }
}
