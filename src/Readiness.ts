// Готовность выбранных методов к переводу.
//
// Сами правила живут на бэкенде (`py_modules/providers/readiness.py`, RPC
// `check_readiness`): там известно, скачана ли модель и что умеет провайдер.
// Здесь только вызов и то, как показать ответ. Свои условия «модель скачана?»
// во фронтенде не заводить — раньше они были в TabMain и разошлись с
// бэкендом: при Opus-MT кнопка «Перевести» не находила его кнопку загрузки.

import { call } from "@decky/api";
import { logger } from "./Logger";

// Причины — те же строки, что REASON_* в readiness.py.
export type ReadinessReason =
    | 'model_missing'
    | 'pair_unsupported'
    | 'auto_unsupported'
    | 'not_available'
    | 'not_started';

export interface Readiness {
    ready: boolean;
    stage?: 'ocr' | 'translation';
    reason?: ReadinessReason;
    // Полный текст для пользователя; может содержать перенос строки.
    message?: string;
    // id кнопки загрузки на вкладке перевода («chromescreenai-action» …);
    // null — причина не в модели, кнопки нет.
    scroll_target?: string | null;
}

const READY: Readiness = { ready: true };

// Языки передаются явно, как в translate_text: так проверяется та же пара,
// что потом уйдёт в перевод. Без них бэкенд берёт свои настройки.
export async function fetchReadiness(inputLanguage?: string, targetLanguage?: string): Promise<Readiness> {
    try {
        const result = await call<[string | undefined, string | undefined], Readiness>(
            'check_readiness', inputLanguage, targetLanguage,
        );
        // Пустой ответ бывает, если бэкенд старее фронтенда (метода нет).
        return result ?? READY;
    } catch (error) {
        // Сломанная проверка не должна запрещать перевод: если что-то и
        // правда не готово, это поймают страховочные проверки бэкенда
        // в recognize_text / translate_text — просто позже.
        logger.error('Readiness', 'check_readiness failed, assuming ready', error);
        return READY;
    }
}

// Короткий заголовок тоста. Полный текст из бэкенда идёт в тело: в заголовок
// тоста Steam длинная фраза не влезает и обрезается.
const TOAST_TITLES: Record<ReadinessReason, string> = {
    model_missing: 'Модель не скачана',
    pair_unsupported: 'Метод не переводит эту пару языков',
    auto_unsupported: 'Нужен исходный язык',
    not_available: 'Метод не готов',
    not_started: 'Плагин запускается',
};

export function readinessToastTitle(readiness: Readiness): string {
    if (readiness.reason) return TOAST_TITLES[readiness.reason];
    return 'Перевод сейчас недоступен';
}

// Перенос строки в сообщении — для оверлея; в теле тоста он не нужен.
export function readinessToastBody(readiness: Readiness): string {
    return (readiness.message ?? '').replace(/\n/g, ' ');
}
