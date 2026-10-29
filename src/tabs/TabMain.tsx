// src/tabs/TabMain.tsx - Main tab with enable toggle and translate button

import {
    ButtonItem,
    PanelSection,
    PanelSectionRow,
    ToggleField,
    Router,
    Navigation,
    DialogButton,
    Focusable
} from "@decky/ui";

import { VFC } from "react";
import { BsTranslate, BsXLg, BsEye, BsStars } from "react-icons/bs";
import { SiKofi } from "react-icons/si";
import { HiQrCode, HiInboxArrowDown } from "react-icons/hi2";
import showQrModal from "../showQrModal";
import { useSettings } from "../SettingsContext";
import { GameTranslatorLogic } from "../Translator";
import { logger } from "../Logger";
import { inputModeOptions } from "./TabControls";
import { BottomSpacer } from "./BottomSpacer";
import { VersionBadge } from "./VersionBadge";
import { DataFlowIcon, OCR_DATA_FLOW, TRANSLATION_DATA_FLOW } from "./DataFlowBadge";

const StatusDot: VFC<{ ok: boolean }> = ({ ok }) => (
    <span style={{
        display: 'inline-block',
        width: '5px',
        height: '5px',
        borderRadius: '50%',
        backgroundColor: ok ? '#4caf50' : '#ff6b6b',
        marginRight: '6px',
        flexShrink: 0
    }} />
);

const PendingDot: VFC = () => (
    <span style={{
        display: 'inline-block',
        width: '5px',
        height: '5px',
        borderRadius: '50%',
        backgroundColor: '#888',
        marginRight: '6px',
        flexShrink: 0
    }} />
);

const InstallingDot: VFC = () => (
    <span style={{
        display: 'inline-block',
        width: '5px',
        height: '5px',
        borderRadius: '50%',
        backgroundColor: '#ffa726',
        marginRight: '6px',
        flexShrink: 0
    }} />
);

type ReachResult = { ok: boolean; reason: string; provider: string } | null | undefined;

const ReachabilityRow: VFC<{ result: ReachResult; expectedProvider: string }> = ({ result, expectedProvider }) => {
    if (!result || result.provider !== expectedProvider) {
        return (
            <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
                <PendingDot />
                <span>Проверка...</span>
            </div>
        );
    }
    return (
        <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
            <StatusDot ok={result.ok} />
            <span>{result.ok ? 'Готово' : `Не готово (${result.reason || 'недоступно'})`}</span>
        </div>
    );
};

interface TabMainProps {
    logic: GameTranslatorLogic;
    overlayVisible: boolean;
    providerStatus: any;
    webReachability: {
        ocr?: ReachResult;
        translation?: ReachResult;
    } | null;
    onNavigateToTab: (tabId: string, scrollTargetId?: string) => void;
}

export const TabMain: VFC<TabMainProps> = ({ logic, overlayVisible, providerStatus, webReachability, onNavigateToTab }) => {
    const { settings, updateSetting } = useSettings();

    const ocrNeedsDownload =
        !!providerStatus
        && ((settings.ocrProvider === 'chromescreenai' && !providerStatus.chromescreenai_downloaded)
            || (settings.ocrProvider === 'rapidocr' && !providerStatus.rapidocr_downloaded));

    const translationNeedsDownload =
        settings.ocrProvider !== 'gemini_vision'
        && ((settings.translationProvider === 'ct2'
                && !!providerStatus && !providerStatus.nllb_downloaded)
            || (settings.translationProvider === 'opusmt'
                && !!providerStatus && !providerStatus.opusmt_downloaded));

    const handleButtonClick = () => {
        if (overlayVisible) {
            logic.dismiss();
            Router.CloseSideMenus();
            return;
        }
        if (ocrNeedsDownload) {
            const target = settings.ocrProvider === 'rapidocr' ? 'rapidocr-action' : 'chromescreenai-action';
            onNavigateToTab('translation', target);
            return;
        }
        if (translationNeedsDownload) {
            onNavigateToTab('translation', 'ct2-action');
            return;
        }
        // Close menu first, then wait for UI to fully close before taking screenshot
        Router.CloseSideMenus();
        setTimeout(() => {
            logic.takeScreenshotAndTranslate().catch(err => logger.error('TabMain', 'Screenshot failed', err));
        }, 200);
    };

    // Newcomers often don't know that translation works in game without
    // opening this menu. Built from the settings, so it follows the user's
    // choice of buttons and hold time.
    const hotkeyHint = (): string => {
        const option = inputModeOptions.find(o => o.data === settings.inputMode);
        const buttons = option ? option.label : String(settings.inputMode);
        const seconds = settings.holdTimeTranslate / 1000;
        if (seconds <= 0) {
            return `Перевод в игре без меню: ${buttons}, нажатие`;
        }
        // 1 -> "1", 1.5 -> "1,5" (decimal comma in Russian)
        const secondsText = String(Number(seconds.toFixed(1))).replace('.', ',');
        return `Перевод в игре без меню: ${buttons}, удерживать ${secondsText} с`;
    };

    const renderButtonContent = () => {
        if (overlayVisible) {
            return <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}><BsXLg /> Закрыть оверлей</span>;
        }
        if (ocrNeedsDownload || translationNeedsDownload) {
            return <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}><HiInboxArrowDown size={20} /> Требуется загрузка</span>;
        }
        return <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}><BsTranslate /> Перевести</span>;
    };

    return (
        <div>
            <PanelSection>
                <PanelSectionRow>
                    <ToggleField
                        label={settings.enabled ? "Плагин включён" : "Плагин выключен"}
                        description="Включить или выключить плагин"
                        checked={settings.enabled}
                        onChange={(value) => updateSetting('enabled', value, 'Decky Translator RU')}
                    />
                </PanelSectionRow>

                {settings.enabled && (
                    <>
                        <PanelSectionRow>
                            <ButtonItem
                                bottomSeparator="standard"
                                layout="below"
                                onClick={handleButtonClick}>
                                {renderButtonContent()}
                            </ButtonItem>
                        </PanelSectionRow>

                        <PanelSectionRow>
                            <div style={{ fontSize: '11px', color: '#8b929a', marginTop: '4px' }}>
                                {hotkeyHint()}
                            </div>
                        </PanelSectionRow>

                        {/* Provider Status */}
                        <PanelSectionRow>
                            <div style={{ fontSize: '12px', marginTop: '8px' }}>
                                {settings.ocrProvider === 'gemini_vision' && (
                                    <div style={{ display: 'flex', alignItems: 'center', marginBottom: '4px' }}>
                                        <BsStars style={{ marginRight: '8px', color: '#aaa' }} />
                                        <span style={{ color: '#888' }}>Распознавание + перевод:</span>
                                        <span style={{ marginLeft: '6px', fontWeight: 'bold' }}>Gemini</span>
                                        <DataFlowIcon flow={OCR_DATA_FLOW.gemini_vision} />
                                    </div>
                                )}
                                {settings.ocrProvider !== 'gemini_vision' && (
                                    <div style={{ display: 'flex', alignItems: 'center', marginBottom: '4px' }}>
                                        <BsEye style={{ marginRight: '8px', color: '#aaa' }} />
                                        <span style={{ color: '#888' }}>Распознавание текста:</span>
                                        <span style={{ marginLeft: '6px', fontWeight: 'bold' }}>
                                            {settings.ocrProvider === 'chromescreenai' ? 'Локально (Chrome)' :
                                             settings.ocrProvider === 'rapidocr' ? 'Локально (RapidOCR)' :
                                             settings.ocrProvider === 'ocrspace' ? 'OCR.space' : 'Google Cloud'}
                                        </span>
                                        <DataFlowIcon flow={OCR_DATA_FLOW[settings.ocrProvider]} />
                                    </div>
                                )}
                                {settings.ocrProvider === 'rapidocr' && (
                                    <div style={{ marginLeft: '22px', marginBottom: '6px' }}>
                                        {providerStatus?.rapidocr_downloaded && (
                                            <div style={{ color: '#666', fontSize: '10px' }}>
                                                Установленная модель: RapidOCR{providerStatus?.rapidocr_info?.version ? ` v${providerStatus.rapidocr_info.version}` : ''}
                                            </div>
                                        )}
                                        <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
                                            {providerStatus?.rapidocr_downloading ? (
                                                <>
                                                    <InstallingDot />
                                                    <span>Установка...</span>
                                                </>
                                            ) : (
                                                <>
                                                    <StatusDot ok={!!providerStatus?.rapidocr_downloaded} />
                                                    <span>{providerStatus?.rapidocr_downloaded ? 'Готово' : 'Не готово (модель не установлена)'}</span>
                                                </>
                                            )}
                                        </div>
                                    </div>
                                )}
                                {settings.ocrProvider === 'chromescreenai' && (
                                    <div style={{ marginLeft: '22px', marginBottom: '6px' }}>
                                                                        {providerStatus?.chromescreenai_downloaded && (
                                            <div style={{ color: '#666', fontSize: '10px', marginBottom: '4px' }}>Движок: Chrome Screen AI</div>
                                        )}
                                        <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
                                            {providerStatus?.chromescreenai_downloading ? (
                                                <>
                                                    <InstallingDot />
                                                    <span>Установка...</span>
                                                </>
                                            ) : (
                                                <>
                                                    <StatusDot ok={!!providerStatus?.chromescreenai_downloaded} />
                                                    <span>{providerStatus?.chromescreenai_downloaded ? 'Готово' : 'Не готово (движок не установлен)'}</span>
                                                </>
                                            )}
                                        </div>
                                    </div>
                                )}
                                {settings.ocrProvider === 'googlecloud' && (
                                    <div style={{ marginLeft: '22px', marginBottom: '6px' }}>
                                        <ReachabilityRow result={webReachability?.ocr} expectedProvider="googlecloud" />
                                    </div>
                                )}
                                {settings.ocrProvider === 'ocrspace' && (
                                    <div style={{ marginLeft: '22px', marginBottom: '6px' }}>
                                        <div style={{ color: '#666', fontSize: '10px', marginBottom: '4px' }}>Бесплатно, API-ключ не нужен</div>
                                        {providerStatus?.ocr_usage && (
                                            <>
                                                <div style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: '3px'
                                                }}>
                                                    <span style={{ color: '#666', fontSize: '10px' }}>
                                                        Лимит 10 мин:
                                                    </span>
                                                    <span style={{
                                                        fontSize: '10px',
                                                        color: providerStatus.ocr_usage.rate_remaining <= 2 ? '#ff6b6b' : '#888'
                                                    }}>
                                                        {providerStatus.ocr_usage.rate_remaining}/{providerStatus.ocr_usage.rate_limit}
                                                    </span>
                                                </div>
                                                <div style={{
                                                    height: '3px',
                                                    backgroundColor: 'rgba(255,255,255,0.1)',
                                                    borderRadius: '2px',
                                                    overflow: 'hidden',
                                                    marginBottom: '4px'
                                                }}>
                                                    <div style={{
                                                        height: '100%',
                                                        width: `${(providerStatus.ocr_usage.rate_remaining / providerStatus.ocr_usage.rate_limit) * 100}%`,
                                                        backgroundColor: providerStatus.ocr_usage.rate_remaining <= 2
                                                            ? '#ff6b6b'
                                                            : providerStatus.ocr_usage.rate_remaining <= 5
                                                                ? '#ffa726'
                                                                : '#4caf50',
                                                        borderRadius: '2px',
                                                        transition: 'width 0.3s ease'
                                                    }} />
                                                </div>
                                                {providerStatus.ocr_usage.rate_remaining === 0 && providerStatus.ocr_usage.rate_reset_seconds > 0 && (
                                                    <div style={{ color: '#ff6b6b', fontSize: '10px', marginBottom: '4px' }}>
                                                        Лимит превышен — сброс через {Math.ceil(providerStatus.ocr_usage.rate_reset_seconds / 60)} мин
                                                    </div>
                                                )}

                                                <div style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: '3px'
                                                }}>
                                                    <span style={{ color: '#666', fontSize: '10px' }}>
                                                        Дневной лимит:
                                                    </span>
                                                    <span style={{
                                                        fontSize: '10px',
                                                        color: providerStatus.ocr_usage.remaining < 50 ? '#ff6b6b' : '#888'
                                                    }}>
                                                        {providerStatus.ocr_usage.remaining}/{providerStatus.ocr_usage.limit}
                                                    </span>
                                                </div>
                                                <div style={{
                                                    height: '3px',
                                                    backgroundColor: 'rgba(255,255,255,0.1)',
                                                    borderRadius: '2px',
                                                    overflow: 'hidden',
                                                    marginBottom: '4px'
                                                }}>
                                                    <div style={{
                                                        height: '100%',
                                                        width: `${(providerStatus.ocr_usage.remaining / providerStatus.ocr_usage.limit) * 100}%`,
                                                        backgroundColor: providerStatus.ocr_usage.remaining < 50
                                                            ? '#ff6b6b'
                                                            : providerStatus.ocr_usage.remaining < 100
                                                                ? '#ffa726'
                                                                : '#4caf50',
                                                        borderRadius: '2px',
                                                        transition: 'width 0.3s ease'
                                                    }} />
                                                </div>
                                                {providerStatus.ocr_usage.remaining < 50 && (
                                                    <div style={{ color: '#ff6b6b', fontSize: '10px', marginBottom: '4px' }}>
                                                        Мало запросов на сегодня
                                                    </div>
                                                )}
                                            </>
                                        )}
                                        <ReachabilityRow result={webReachability?.ocr} expectedProvider="ocrspace" />
                                    </div>
                                )}
                                {settings.ocrProvider !== 'gemini_vision' && (
                                    <div style={{ display: 'flex', alignItems: 'center', marginBottom: '2px' }}>
                                        <BsTranslate style={{ marginRight: '8px', color: '#aaa' }} />
                                        <span style={{ color: '#888' }}>Перевод:</span>
                                        <span style={{ marginLeft: '6px', fontWeight: 'bold' }}>
                                            {settings.translationProvider === 'googlecloud' ? 'Google Cloud' :
                                             settings.translationProvider === 'ct2' ? 'Локально (NLLB)' :
                                             settings.translationProvider === 'opusmt' ? 'Локально (лёгкая, en→ru)' :
                                             settings.translationProvider === 'yandex' ? 'Яндекс' :
                                             settings.translationProvider === 'mymemory' ? 'MyMemory' : 'Google Translate'}
                                        </span>
                                        <DataFlowIcon flow={TRANSLATION_DATA_FLOW[settings.translationProvider]} />
                                    </div>
                                )}
                                <div style={{ marginLeft: '22px', marginBottom: '6px' }}>
                                    {settings.ocrProvider === 'gemini_vision' && (
                                        <>
                                            <div style={{ color: '#666', fontSize: '10px' }}>
                                                Модель: {settings.geminiModel.replace(/^gemini-/, '').split('-').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ')}
                                            </div>
                                            <ReachabilityRow result={webReachability?.ocr} expectedProvider="gemini_vision" />
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'freegoogle' && (
                                        <>
                                            <div style={{ color: '#666', fontSize: '10px' }}>API-ключ не нужен</div>
                                            <ReachabilityRow result={webReachability?.translation} expectedProvider="freegoogle" />
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'mymemory' && (
                                        <>
                                            <div style={{ color: '#666', fontSize: '10px' }}>API-ключ не нужен, лимит 5 000 символов в день</div>
                                            <ReachabilityRow result={webReachability?.translation} expectedProvider="mymemory" />
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'googlecloud' && (
                                        <>
                                            <ReachabilityRow result={webReachability?.translation} expectedProvider="googlecloud" />
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'yandex' && (
                                        <>
                                            <div style={{ color: '#666', fontSize: '10px' }}>Требуется API-ключ и folder_id</div>
                                            <ReachabilityRow result={webReachability?.translation} expectedProvider="yandex" />
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'ct2' && (
                                        <>
                                            {providerStatus?.nllb_downloaded && (
                                                <div style={{ color: '#666', fontSize: '10px' }}>Модель: NLLB-200 1.3B</div>
                                            )}
                                            <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
                                                {providerStatus?.nllb_downloading ? (
                                                    <>
                                                        <InstallingDot />
                                                        <span>Установка...</span>
                                                    </>
                                                ) : (
                                                    <>
                                                        <StatusDot ok={!!providerStatus?.nllb_downloaded} />
                                                        <span>{providerStatus?.nllb_downloaded ? 'Готово' : 'Не готово (модель не установлена)'}</span>
                                                    </>
                                                )}
                                            </div>
                                        </>
                                    )}
                                    {settings.ocrProvider !== 'gemini_vision' && settings.translationProvider === 'opusmt' && (
                                        <>
                                            <div style={{ color: '#666', fontSize: '10px', display: 'flex', alignItems: 'center' }}>
                                                {providerStatus?.opusmt_downloading ? (
                                                    <>
                                                        <InstallingDot />
                                                        <span>Установка...</span>
                                                    </>
                                                ) : (
                                                    <>
                                                        <StatusDot ok={!!providerStatus?.opusmt_downloaded} />
                                                        <span>{providerStatus?.opusmt_downloaded ? 'Готово (модель 82 МБ)' : 'Не готово (модель не установлена)'}</span>
                                                    </>
                                                )}
                                            </div>
                                        </>
                                    )}
                                </div>
                            </div>
                        </PanelSectionRow>
                    </>
                )}

                <PanelSectionRow>
                    <VersionBadge />
                </PanelSectionRow>

                {/* Ko-fi Support Button */}
                <PanelSectionRow>
                    <div
                        style={{
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            gap: '6px',
                            marginTop: '12px',
                        }}
                    >
                        {/* The link goes to the author of the original plugin, not to
                            this fork. The note says so above the button, so the button
                            label stays short and fits on one line in the 300px panel
                            (a long label wrapped to two lines and made it tall). */}
                        <div style={{ fontSize: '11px', color: '#8b929a', textAlign: 'center' }}>
                            Этот плагин — форк Decky Translator от cat-in-a-box.
                            Нажав кнопку ниже, можете поддержать автора оригинального плагина.
                        </div>
                        <Focusable>
                            <DialogButton
                                onClick={() => {
                                    Navigation.CloseSideMenus();
                                    Navigation.NavigateToExternalWeb('https://ko-fi.com/alexanderdev');
                                }}
                                onSecondaryButton={() => showQrModal('https://ko-fi.com/alexanderdev')}
                                onSecondaryActionDescription="Показать QR-код"
                                style={{
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '8px',
                                    padding: '6px 12px',
                                    fontSize: '11px',
                                    minWidth: 'auto',
                                }}
                            >
                                <SiKofi style={{ fontSize: '13px' }} />
                                {/* GitHub name of the author; on Ko-fi the account is "alexanderdev" */}
                                <span>Ko-fi cat-in-a-box</span>
                                <HiQrCode style={{ fontSize: '13px', opacity: 0.6 }} />
                            </DialogButton>
                        </Focusable>
                    </div>
                </PanelSectionRow>
            </PanelSection>
            <BottomSpacer />
        </div>
    );
};
