// TextTranslator.tsx

import { call } from "@decky/api";
import { TextRegion, NetworkError, ApiKeyError, RateLimitError, ModelNotAvailableError, TranslationError, ErrorResponse } from "./TextRecognizer";
import { logger } from "./Logger";

// Type guard to check if response is an error
function isErrorResponse(value: unknown): value is ErrorResponse {
    return typeof value === 'object' && value !== null && 'error' in value && 'message' in value;
}

// Include translated text with the original region info
export interface TranslatedRegion extends TextRegion {
    translatedText: string;
}

export class TextTranslator {
    private targetLanguage: string;
    private inputLanguage: string = "en"; // Default to English

    constructor(initialLanguage: string = "ru") {
        this.targetLanguage = initialLanguage;
    }

    setTargetLanguage(language: string): void {
        this.targetLanguage = language;
    }

    getTargetLanguage(): string {
        return this.targetLanguage;
    }

    // New methods for input language
    setInputLanguage(language: string): void {
        this.inputLanguage = language;
    }

    getInputLanguage(): string {
        return this.inputLanguage;
    }

    async translateText(textRegions: TextRegion[]): Promise<TranslatedRegion[]> {
        try {
            // Skip translation if there's nothing to translate
            if (!textRegions.length) {
                return [];
            }

            // Call the Python backend method for translation, now including input language
            const response = await call<[TextRegion[], string, string], TranslatedRegion[] | ErrorResponse>(
                'translate_text',
                textRegions,
                this.targetLanguage,
                this.inputLanguage
            );

            if (response) {
                // Check for error response (network error, API key error)
                if (isErrorResponse(response)) {
                    const errorResponse = response as ErrorResponse;
                    if (errorResponse.error === 'network_error') {
                        logger.error('TextTranslator', `Network error: ${errorResponse.message}`);
                        throw new NetworkError(errorResponse.message);
                    }
                    if (errorResponse.error === 'api_key_error') {
                        logger.error('TextTranslator', `API key error: ${errorResponse.message}`);
                        throw new ApiKeyError(errorResponse.message);
                    }
                    if (errorResponse.error === 'model_not_available') {
                        logger.error('TextTranslator', `Model not available: ${errorResponse.message}`);
                        throw new ModelNotAvailableError(errorResponse.message);
                    }
                    if (errorResponse.error === 'rate_limit_error') {
                        logger.error('TextTranslator', `Rate limit error: ${errorResponse.message}`);
                        throw new RateLimitError(errorResponse.message);
                    }
                    // "translation_error" and any code this version does not know
                    logger.error('TextTranslator', `Error from backend: ${errorResponse.error} - ${errorResponse.message}`);
                    throw new TranslationError(errorResponse.message || "Ошибка перевода");
                }

                return response as TranslatedRegion[];
            }

            logger.error('TextTranslator', 'Failed to translate text: empty response from backend');
            throw new TranslationError("Ошибка перевода: плагин не вернул результат");
        } catch (error) {
            // Re-throw known errors to be handled by caller
            if (error instanceof NetworkError || error instanceof ApiKeyError || error instanceof RateLimitError
                || error instanceof ModelNotAvailableError || error instanceof TranslationError) {
                throw error;
            }
            // E.g. the backend call itself failed. Before B13 the original text
            // was returned here, which looked like a successful translation.
            logger.error('TextTranslator', 'Text translation error', error);
            throw new TranslationError("Ошибка перевода: нет ответа от плагина");
        }
    }
}