// src/tabs/TabControls.tsx - Input controls, behavior settings, and debug

import {
    PanelSection,
    PanelSectionRow,
    DropdownItem,
    ToggleField,
    SliderField,
    Field
} from "@decky/ui";

import { VFC, useState } from "react";
import { useSettings } from "../SettingsContext";
import { InputMode } from "../Input";
import { useFontOptions, isRemoteFont, loadRemoteFont } from "../fonts";
import { BottomSpacer } from "./BottomSpacer";

// Input mode options for dropdown. Also used by the hotkey hint on the main tab,
// so the hint names the buttons exactly as this list does.
export const inputModeOptions = [
    { label: "L3 (Нажатие левого стика)", data: InputMode.L3_BUTTON },
    { label: "L4", data: InputMode.L4_BUTTON },
    { label: "L5", data: InputMode.L5_BUTTON },
    { label: "R3 (Нажатие правого стика)", data: InputMode.R3_BUTTON },
    { label: "R4", data: InputMode.R4_BUTTON },
    { label: "R5", data: InputMode.R5_BUTTON },
    { label: "L3 + R3 (Нажатие обоих стиков)", data: InputMode.L3_R3_COMBO },
    { label: "L4 + R4", data: InputMode.L4_R4_COMBO },
    { label: "L5 + R5", data: InputMode.L5_R5_COMBO },
    { label: "Касание обоих тачпадов", data: InputMode.TOUCHPAD_COMBO }
];

const translatedTextAlignmentOptions = [
    { label: "Слева", data: 'left' },
    { label: "Справа", data: 'right' },
    { label: "По центру", data: 'center' },
    { label: "Растянуть", data: 'justify' }
];

const fontStyleLabels: Record<string, string> = {
    normal: 'Обычный',
    bold: 'Жирный',
    italic: 'Курсив',
    bolditalic: 'Жирный курсив'
};

// Helper to get button labels for current input mode
const getInputModeButtons = (mode: string): string => {
    switch (mode) {
        case 'L3_BUTTON': return 'L3';
        case 'L4_BUTTON': return 'L4';
        case 'L5_BUTTON': return 'L5';
        case 'R3_BUTTON': return 'R3';
        case 'R4_BUTTON': return 'R4';
        case 'R5_BUTTON': return 'R5';
        case 'L3_R3_COMBO': return 'L3 + R3';
        case 'L4_R4_COMBO': return 'L4 + R4';
        case 'L5_R5_COMBO': return 'L5 + R5';
        case 'TOUCHPAD_COMBO': return 'Левый + правый тачпад';
        default: return mode;
    }
};

interface TabControlsProps {
    inputDiagnostics: any;
}

export const TabControls: VFC<TabControlsProps> = ({ inputDiagnostics }) => {
    const { settings, updateSetting } = useSettings();
    const { fontOptions, fontDescription, preloadWebFonts, unavailableDyslexiaFonts } = useFontOptions(
        settings.translatedTextFontFamily,
        settings.targetLanguage,
        () => updateSetting('translatedTextFontFamily', '', 'Шрифт переведённого текста'),
    );
    const [fontDropdownKey, setFontDropdownKey] = useState(0);

    return (
        <div>
            <PanelSection title="Управление">
                <PanelSectionRow>
                    <DropdownItem
                        layout="below"
                        label="Горячая клавиша перевода"
                        description="Выберите, какие кнопки удерживать для запуска перевода"
                        rgOptions={inputModeOptions}
                        selectedOption={settings.inputMode}
                        onChange={(option) => updateSetting('inputMode', option.data, 'Горячая клавиша перевода')}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <SliderField
                        value={settings.holdTimeTranslate / 1000}
                        max={3}
                        min={0}
                        step={0.1}
                        label="Время удержания для перевода"
                        description="Сколько секунд удерживать кнопки для перевода"
                        showValue={true}
                        valueSuffix="с"
                        onChange={(value) => {
                            const milliseconds = Math.round(value * 1000);
                            updateSetting('holdTimeTranslate', milliseconds, 'Время удержания для перевода');
                        }}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <SliderField
                        value={settings.holdTimeDismiss / 1000}
                        max={3}
                        min={0}
                        step={0.1}
                        label="Время удержания для закрытия"
                        description="Сколько секунд удерживать кнопки, чтобы закрыть оверлей"
                        showValue={true}
                        valueSuffix="с"
                        onChange={(value) => {
                            const milliseconds = Math.round(value * 1000);
                            updateSetting('holdTimeDismiss', milliseconds, 'Время удержания для закрытия');
                        }}
                    />
                </PanelSectionRow>

                {/* Quick toggle option - only show for combo modes */}
                {(settings.inputMode === InputMode.L4_R4_COMBO ||
                    settings.inputMode === InputMode.L5_R5_COMBO ||
                    settings.inputMode === InputMode.L3_R3_COMBO ||
                    settings.inputMode === InputMode.TOUCHPAD_COMBO) && (
                    <PanelSectionRow>
                        <ToggleField
                            checked={settings.quickToggleEnabled}
                            label="Быстрое переключение правой кнопкой"
                            description="Если выбрана комбинация из двух кнопок, нажмите правую кнопку, чтобы показать или скрыть оверлей"
                            onChange={(value) => {
                                updateSetting('quickToggleEnabled', value, 'Быстрое переключение правой кнопкой');
                            }}
                        />
                    </PanelSectionRow>
                )}
            </PanelSection>

            <PanelSection title="Отображение">
                <PanelSectionRow>
                    <SliderField
                        value={settings.fontScale}
                        max={3}
                        min={1}
                        step={0.1}
                        label="Масштаб шрифта"
                        description="Увеличьте, если переведённый текст слишком мелкий. Полезно для больших внешних мониторов"
                        showValue={true}
                        valueSuffix="x"
                        onChange={(value) => {
                            const rounded = Math.round(value * 10) / 10;
                            updateSetting('fontScale', rounded, 'Масштаб шрифта');
                        }}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <SliderField
                        value={settings.groupingPower}
                        min={0.25}
                        max={1.0}
                        step={0.25}
                        notchCount={4}
                        notchTicksVisible={true}
                        label="Группировка текстовых блоков"
                        description={
                            settings.groupingPower <= 0.25 ? "Обычная — блоки текста не объединяются" :
                            settings.groupingPower <= 0.5 ? "Усиленная — объединяет блоки текста" :
                            settings.groupingPower <= 0.75 ? "Большая — объединяет отдалённые блоки" :
                            "Максимальная — объединяет очень отдалённые блоки"
                        }
                        onChange={(value) => {
                            updateSetting('groupingPower', value, 'Группировка текстовых блоков');
                        }}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <DropdownItem
                        layout="below"
                        label="Выравнивание переведённого текста"
                        description="Выберите выравнивание для подписей переведённого текста"
                        rgOptions={translatedTextAlignmentOptions}
                        selectedOption={settings.translatedTextAlignment}
                        onChange={(option) => updateSetting('translatedTextAlignment', option.data, 'Выравнивание переведённого текста')}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <DropdownItem
                        key={fontDropdownKey}
                        layout="below"
                        label="Шрифт переведённого текста"
                        description={fontDescription}
                        rgOptions={fontOptions}
                        selectedOption={settings.translatedTextFontFamily}
                        onMenuWillOpen={(showMenu) => {
                            preloadWebFonts();
                            showMenu();
                        }}
                        onChange={(option) => {
                            const fontName = option.data;
                            if (fontName && unavailableDyslexiaFonts.has(fontName)) {
                                setFontDropdownKey(k => k + 1);
                                return;
                            }
                            if (fontName && isRemoteFont(fontName)) {
                                const previousFont = settings.translatedTextFontFamily;
                                updateSetting('translatedTextFontFamily', fontName, 'Шрифт переведённого текста');
                                loadRemoteFont(fontName).then((ok) => {
                                    if (!ok) {
                                        updateSetting('translatedTextFontFamily', previousFont, 'Шрифт переведённого текста');
                                    }
                                });
                            } else {
                                updateSetting('translatedTextFontFamily', fontName, 'Шрифт переведённого текста');
                            }
                        }}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <DropdownItem
                        layout="below"
                        label="Стиль переведённого текста"
                        description="Насыщенность и стиль шрифта для переведённого текста"
                        rgOptions={[
                            { label: <span>Обычный</span>, data: "normal" },
                            { label: <span style={{ fontWeight: 'bold' }}>Жирный</span>, data: "bold" },
                            { label: <span style={{ fontStyle: 'italic' }}>Курсив</span>, data: "italic" },
                            { label: <span style={{ fontWeight: 'bold', fontStyle: 'italic' }}>Жирный курсив</span>, data: "bolditalic" }
                        ]}
                        selectedOption={settings.translatedTextFontStyle}
                        renderButtonValue={() => fontStyleLabels[settings.translatedTextFontStyle] || 'Обычный'}
                        onChange={(option) => updateSetting('translatedTextFontStyle', option.data, 'Стиль переведённого текста')}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <ToggleField
                        checked={settings.hideIdenticalTranslations}
                        label="Скрывать одинаковые переводы"
                        description="Не показывать, если перевод совпадает с исходным словом или предложением"
                        onChange={(value) => {
                            updateSetting('hideIdenticalTranslations', value, 'Скрывать одинаковые переводы');
                        }}
                    />
                </PanelSectionRow>

                <PanelSectionRow>
                    <ToggleField
                        checked={settings.allowLabelGrowth}
                        label="Разрешить расширение подписей"
                        description="Позволить подписям перевода расширяться, если текст не помещается в исходный блок"
                        onChange={(value) => {
                            updateSetting('allowLabelGrowth', value, 'Разрешить расширение подписей');
                        }}
                    />
                </PanelSectionRow>
            </PanelSection>

            <PanelSection title="Поведение">
                <PanelSectionRow>
                    <ToggleField
                        checked={settings.pauseGameOnOverlay}
                        label="Ставить игру на паузу при переводе"
                        description={<>Ставит активную игру на паузу, чтобы можно было спокойно прочитать текст. Игра возобновится после закрытия оверлея.<br /><br />Плохо работает при стриминге игр (Moonlight, GeForce NOW, Remote Play и т.п.)</>}
                        onChange={(value) => {
                            updateSetting('pauseGameOnOverlay', value, 'Ставить игру на паузу при переводе');
                        }}
                    />
                </PanelSectionRow>
            </PanelSection>

            <PanelSection title="Разное">
                <PanelSectionRow>
                    <ToggleField
                        label="Режим отладки"
                        description="Включить подробное логирование в консоли и панель диагностики"
                        checked={settings.debugMode}
                        onChange={(value) => updateSetting('debugMode', value, 'Режим отладки')}
                    />
                </PanelSectionRow>

                {/* Show diagnostics when debug mode is on */}
                {settings.debugMode && inputDiagnostics && (
                    <PanelSectionRow>
                        <Field
                            focusable={true}
                            childrenContainerWidth="max"
                        >
                            <div className="dt-debug-panel" style={{
                                backgroundColor: 'rgba(0,0,0,0.4)',
                                padding: '12px',
                                borderRadius: '6px',
                                fontSize: '11px',
                                fontFamily: 'monospace',
                                border: '1px solid rgba(255,255,255,0.1)'
                            }}>
                                <div style={{ display: 'grid', gap: '3px' }}>
                                    <div>
                                        <span style={{ color: '#888' }}>Статус:</span>{' '}
                                        {inputDiagnostics.enabled ?
                                            (inputDiagnostics.healthy ? 'Исправен' : 'Неисправен') :
                                            'Выключен'
                                        }
                                    </div>

                                    <div>
                                        <span style={{ color: '#888' }}>Режим ввода:</span>{' '}
                                        {getInputModeButtons(inputDiagnostics.inputMode)}
                                    </div>

                                    <div>
                                        <span style={{ color: '#888' }}>Ввод активен:</span>{' '}
                                        {inputDiagnostics.leftTouchpadTouched ? 'Да' : 'Нет'}
                                    </div>

                                    <div>
                                        <span style={{ color: '#888' }}>Нажатые кнопки:</span>{' '}
                                        {inputDiagnostics.currentButtons && inputDiagnostics.currentButtons.length > 0
                                            ? inputDiagnostics.currentButtons.join(', ')
                                            : 'Нет'}
                                    </div>

                                    <div>
                                        <span style={{ color: '#888' }}>Состояние плагина:</span>{' '}
                                        {!inputDiagnostics.inCooldown && !inputDiagnostics.waitingForRelease && !inputDiagnostics.overlayVisible ? 'Готов' : ''}
                                        {inputDiagnostics.inCooldown ? 'Перезарядка ' : ''}
                                        {inputDiagnostics.waitingForRelease ? 'Ожидание отпускания ' : ''}
                                        {inputDiagnostics.overlayVisible ? 'Оверлей ' : ''}
                                    </div>

                                    <div>
                                        <span style={{ color: '#888' }}>Тайминги:</span>{' '}
                                        Удержание:{inputDiagnostics.translateHoldTime}мс{' '}
                                        Закрытие:{inputDiagnostics.dismissHoldTime}мс
                                    </div>
                                </div>

                                {!inputDiagnostics.healthy && inputDiagnostics.enabled && (
                                    <div style={{
                                        color: '#ff6b6b',
                                        fontWeight: 'bold',
                                        marginTop: '8px',
                                        padding: '6px',
                                        backgroundColor: 'rgba(255, 107, 107, 0.1)',
                                        borderRadius: '4px',
                                        fontSize: '11px'
                                    }}>
                                        Система ввода неисправна — попробуйте выключить и включить плагин
                                    </div>
                                )}
                            </div>
                        </Field>
                    </PanelSectionRow>
                )}
            </PanelSection>
            <BottomSpacer />
        </div>
    );
};
