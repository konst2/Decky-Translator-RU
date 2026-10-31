// Overlay.tsx - Handles overlay components and UI

import { findModuleChild } from "@decky/ui";


import { VFC, useEffect, useState, useRef, useCallback, useMemo } from "react";
import { TranslatedRegion } from "./TextTranslator";
import { NO_SPACE_SCRIPT, lineWrapSeparator } from "./TextRecognizer";
import { logger } from "./Logger";
import { buildTranslatedFontFamily, ensureFontLoaded, resolveFontStyleCSS } from "./fonts";
import type { FontStyleOption } from "./fonts";
import { DataFlowIconGlyph } from "./tabs/DataFlowBadge";
import type { DataFlow } from "./tabs/DataFlowBadge";

export type HorizontalTextAlignment = 'left' | 'right' | 'center' | 'justify';

// UI Composition for overlay
enum UIComposition {
    Hidden = 0,
    Notification = 1,
    Overlay = 2,
    Opaque = 3,
    OverlayKeyboard = 4,
}

const useUIComposition: (composition: UIComposition) => void = findModuleChild(
    (m) => {
        if (typeof m !== "object") return undefined;
        for (let prop in m) {
            if (
                typeof m[prop] === "function" &&
                m[prop].toString().includes("AddMinimumCompositionStateRequest") &&
                m[prop].toString().includes("ChangeMinimumCompositionStateRequest") &&
                m[prop].toString().includes("RemoveMinimumCompositionStateRequest") &&
                !m[prop].toString().includes("m_mapCompositionStateRequests")
            ) {
                return m[prop];
            }
        }
    }
);

// Mountable component that holds a composition state request.
// When unmounted, the hook cleanup calls RemoveMinimumCompositionStateRequest,
// fully releasing the request so Steam's own UI sections can get input focus.
const CompositionRequest: VFC<{ level: UIComposition }> = ({ level }) => {
    useUIComposition(level);
    return null;
};

// Corners of the captured frame: each arm paints only two sides
const frameCornerArms = (inset: number, size: number, thickness: number, color: string) => [
    { top: inset, left: inset, borderTopWidth: thickness, borderLeftWidth: thickness },
    { top: inset, right: inset, borderTopWidth: thickness, borderRightWidth: thickness },
    { bottom: inset, left: inset, borderBottomWidth: thickness, borderLeftWidth: thickness },
    { bottom: inset, right: inset, borderBottomWidth: thickness, borderRightWidth: thickness },
].map((arm, i) => (
    <div key={i} style={{
        position: "absolute",
        width: size,
        height: size,
        // Without this the two untouched sides fall back to the initial border width
        // and the corner grows into a box
        borderWidth: 0,
        borderStyle: "solid",
        borderColor: color,
        ...arm,
    }} />
));

// Enhanced ImageState to handle translated text regions
export class ImageState {
    private visible = false;
    private imageData = "";
    private translatedRegions: TranslatedRegion[] = [];
    private loading = false;
    private processingStep = ""; // Added to track current processing step
    private processingDetail = "";
    private processingIsError = false;
    // Where data goes (cloud icon on the status pill): the current stage while
    // working, the whole run on final statuses ("Текст не найден", errors).
    // null = no icon, e.g. the "Обработка" stage before any provider is involved.
    private processingDataFlow: DataFlow | null = null;
    private loadingIndicatorTimer: ReturnType<typeof setTimeout> | null = null; // Timer for delayed indicator
    private translationsVisible = true; // New property to track translation visibility
    private fontScale = 1.0;
    private allowLabelGrowth = true;
    private translatedTextAlignment: HorizontalTextAlignment = 'center';
    private translatedTextFontFamily = "";
    private translatedTextFontStyle: FontStyleOption = 'normal';
    private onStateChangedListeners: Array<(visible: boolean, imageData: string, regions: TranslatedRegion[], loading: boolean, processingStep: string, processingDetail: string, processingIsError: boolean, translationsVisible: boolean, fontScale: number, allowLabelGrowth: boolean, translatedTextAlignment: HorizontalTextAlignment, translatedTextFontFamily: string, translatedTextFontStyle: FontStyleOption, processingDataFlow: DataFlow | null) => void> = [];

    onStateChanged(callback: (visible: boolean, imageData: string, regions: TranslatedRegion[], loading: boolean, processingStep: string, processingDetail: string, processingIsError: boolean, translationsVisible: boolean, fontScale: number, allowLabelGrowth: boolean, translatedTextAlignment: HorizontalTextAlignment, translatedTextFontFamily: string, translatedTextFontStyle: FontStyleOption, processingDataFlow: DataFlow | null) => void): void {
        this.onStateChangedListeners.push(callback);
    }

    offStateChanged(callback: (visible: boolean, imageData: string, regions: TranslatedRegion[], loading: boolean, processingStep: string, processingDetail: string, processingIsError: boolean, translationsVisible: boolean, fontScale: number, allowLabelGrowth: boolean, translatedTextAlignment: HorizontalTextAlignment, translatedTextFontFamily: string, translatedTextFontStyle: FontStyleOption, processingDataFlow: DataFlow | null) => void): void {
        const index = this.onStateChangedListeners.indexOf(callback);
        if (index !== -1) {
            this.onStateChangedListeners.splice(index, 1);
        }
    }

    // Show the overlay with loading indicator immediately
    startLoading(step: string = "Захват экрана"): void {
        // Set internal state immediately
        this.visible = true;
        this.loading = true;
        this.processingStep = step;
        this.processingDetail = "";
        this.processingIsError = false;
        this.processingDataFlow = null;
        this.translationsVisible = true; // Reset to visible when starting new translation

        // Clear any existing timer
        if (this.loadingIndicatorTimer) {
            clearTimeout(this.loadingIndicatorTimer);
            this.loadingIndicatorTimer = null;
        }

        // Show loading indicator immediately - no stealth mode
        // This ensures the overlay has visible content which properly maintains UI composition
        this.notifyListeners();
    }

    // Toggle translation visibility
    toggleTranslationsVisibility(): void {
        this.translationsVisible = !this.translationsVisible;
        logger.debug('ImageState', `Translations visibility toggled to: ${this.translationsVisible}`);
        this.notifyListeners();
    }

    // Getter for translation visibility state
    areTranslationsVisible(): boolean {
        return this.translationsVisible;
    }

    setFontScale(scale: number): void {
        this.fontScale = scale;
        this.notifyListeners();
    }

    getFontScale(): number {
        return this.fontScale;
    }

    setAllowLabelGrowth(allow: boolean): void {
        this.allowLabelGrowth = allow;
        this.notifyListeners();
    }

    getAllowLabelGrowth(): boolean {
        return this.allowLabelGrowth;
    }

    setTranslatedTextAlignment(alignment: HorizontalTextAlignment): void {
        this.translatedTextAlignment = alignment;
        this.notifyListeners();
    }

    getTranslatedTextAlignment(): HorizontalTextAlignment {
        return this.translatedTextAlignment;
    }

    setTranslatedTextFontFamily(fontFamily: string): void {
        this.translatedTextFontFamily = fontFamily;
        this.notifyListeners();
    }

    getTranslatedTextFontFamily(): string {
        return this.translatedTextFontFamily;
    }

    setTranslatedTextFontStyle(style: FontStyleOption): void {
        this.translatedTextFontStyle = style;
        this.notifyListeners();
    }

    getTranslatedTextFontStyle(): FontStyleOption {
        return this.translatedTextFontStyle;
    }

    // Update the current processing step
    // dataFlow is passed explicitly on each call, not kept from the previous
    // stage: recognition and translation may use providers with different flows.
    updateProcessingStep(step: string, isError: boolean = false, detail: string = "", isLoading: boolean = true, dataFlow: DataFlow | null = null): void {
        this.processingStep = step;
        this.processingDetail = detail;
        this.processingIsError = isError;
        this.processingDataFlow = dataFlow;
        // Update the loading state and keep the current image displayed
        this.loading = isLoading;
        // Force immediate update
        this.notifyListeners();
    }

    showImage(imageData: string): void {
        // Clear any pending timer
        if (this.loadingIndicatorTimer) {
            clearTimeout(this.loadingIndicatorTimer);
            this.loadingIndicatorTimer = null;
        }

        // Always set a fresh image data - don't reuse old data
        this.imageData = imageData;

        // Clear any previous translations
        this.translatedRegions = [];

        // Ensure the overlay is visible
        this.visible = true;

        // Reset translations visibility to true for new image
        this.translationsVisible = true;

        // Set loading state based on whether we're in the middle of processing
        this.loading = this.processingStep !== "";

        logger.debug('ImageState', `Showing new image, length: ${imageData.length}, loading: ${this.loading}, step: ${this.processingStep}`);

        // Notify all listeners about the state change
        this.notifyListeners();
    }

    showTranslatedImage(imageData: string, regions: TranslatedRegion[]): void {
        // Clear any pending timer
        if (this.loadingIndicatorTimer) {
            clearTimeout(this.loadingIndicatorTimer);
            this.loadingIndicatorTimer = null;
        }

        // Always set fresh image data
        this.imageData = imageData;

        // Set the translated regions
        this.translatedRegions = regions;

        // Ensure the overlay is visible
        this.visible = true;

        // Make sure translations are visible when first showing them
        this.translationsVisible = true;

        // Turn off loading state and clear processing step
        this.loading = false;
        this.processingStep = "";
        this.processingDetail = "";
        this.processingIsError = false;
        this.processingDataFlow = null;

        logger.info('ImageState', `Showing translated image with ${regions.length} text regions`);

        this.notifyListeners();
    }

    hideImage(): void {
        // Clear any pending timer
        if (this.loadingIndicatorTimer) {
            clearTimeout(this.loadingIndicatorTimer);
            this.loadingIndicatorTimer = null;
        }

        // Reset all state properties
        this.visible = false;
        this.loading = false;
        this.processingStep = "";
        this.processingDetail = "";
        this.processingIsError = false;
        this.processingDataFlow = null;
        this.translationsVisible = true; // Reset to default when hiding

        // Important: Clear the image data and regions to prevent reuse
        this.imageData = "";
        this.translatedRegions = [];

        logger.debug('ImageState', 'Hiding image and clearing all state');

        this.notifyListeners();
    }

    private notifyListeners(): void {
        for (const callback of this.onStateChangedListeners) {
            callback(this.visible, this.imageData, this.translatedRegions, this.loading, this.processingStep, this.processingDetail, this.processingIsError, this.translationsVisible, this.fontScale, this.allowLabelGrowth, this.translatedTextAlignment, this.translatedTextFontFamily, this.translatedTextFontStyle, this.processingDataFlow);
        }
    }

    isVisible(): boolean {
        return this.visible;
    }

    isLoading(): boolean {
        return this.loading;
    }

    getCurrentStep(): string {
        return this.processingStep;
    }
}

// Overlay text metrics. The vertical step is shared by the fitter and by the
// CSS below — change it here only, otherwise the two disagree.
const OVERLAY_LINE_HEIGHT = 1.15;
const OVERLAY_PAD_Y_EM = 0.06;   // padding: 0.06em 0.15em
const OVERLAY_PAD_X_EM = 0.15;
const MIN_OVERLAY_FONT_SIZE = 7;
const MAX_OVERLAY_FONT_SIZE = 48;
// Canvas widths and the browser's own line layout differ by fractions of a
// pixel (kerning, rounding). Wrapping a bit narrower than the real box keeps
// the browser from breaking a line we considered full a second time.
const OVERLAY_WRAP_SAFETY_PX = 1;
// allowLabelGrowth: the translation "does not fit" its block when its font
// comes out at least this much smaller than the original text's would. A 1px
// difference is invisible and not worth a plate spilling over the picture.
const MIN_GROWTH_FONT_SHORTFALL = 2;

// Width of `text` at `fontSize` in the overlay's font.
type OverlayMeasure = (text: string, fontSize: number) => number;

let overlayMeasureCtx: CanvasRenderingContext2D | null | undefined;
// Font last assigned to the shared context. Kept here instead of reading
// ctx.font back: the browser normalises that string, so comparing with it
// would reassign the font on every call.
let overlayMeasureFont = '';

function getOverlayMeasureContext(): CanvasRenderingContext2D | null {
    if (overlayMeasureCtx === undefined) {
        try {
            overlayMeasureCtx = document.createElement('canvas').getContext('2d');
        } catch {
            overlayMeasureCtx = null;
        }
    }
    return overlayMeasureCtx;
}

function overlayFontCss(fontSize: number, fontFamily: string, fontStyle: FontStyleOption): string {
    const { fontWeight, fontStyle: cssFontStyle } = resolveFontStyleCSS(fontStyle);
    return `${cssFontStyle} ${fontWeight} ${fontSize}px ${fontFamily}`;
}

// Measures text as the browser will lay it out. The size search re-wraps the
// same words at several sizes, and assigning ctx.font is the costly part, so
// widths are cached and the font is only reassigned when it changes. Create
// one per render: the cache does not notice a web font finishing loading.
// Average-glyph fallback keeps the overlay usable if canvas is unavailable.
function createOverlayMeasure(fontFamily: string, fontStyle: FontStyleOption): OverlayMeasure {
    const ctx = getOverlayMeasureContext();
    const widths = new Map<string, number>();

    return (text, fontSize) => {
        if (!ctx) return text.length * fontSize * 0.55;

        const key = `${fontSize}|${text}`;
        const cached = widths.get(key);
        if (cached !== undefined) return cached;

        const font = overlayFontCss(fontSize, fontFamily, fontStyle);
        if (font !== overlayMeasureFont) {
            ctx.font = font;
            overlayMeasureFont = font;
        }
        const width = ctx.measureText(text).width;
        widths.set(key, width);
        return width;
    };
}

interface WrappedText {
    lines: string[];
    // A word of a space-separated script had to be cut mid-word.
    brokeWord: boolean;
}

// Greedy wrap: fill each line up to `maxWidth`. Tokens wider than a line are
// broken per character so a long word cannot escape the block.
function wrapOverlayText(text: string, maxWidth: number, fontSize: number, measure: OverlayMeasure): WrappedText {
    const words = text.split(/\s+/).filter(w => w.length > 0);
    if (words.length === 0) return { lines: [], brokeWord: false };

    const spaceWidth = measure(' ', fontSize);
    const lines: string[] = [];
    let brokeWord = false;
    let line = '';
    let lineWidth = 0;

    for (const word of words) {
        const wordWidth = measure(word, fontSize);

        if (wordWidth > maxWidth) {
            if (line) {
                lines.push(line);
                line = '';
                lineWidth = 0;
            }
            let chunk = '';
            let chunkWidth = 0;
            let prevCh = '';
            for (const ch of word) {
                const chWidth = measure(ch, fontSize);
                if (chunk && chunkWidth + chWidth > maxWidth) {
                    // Chinese/Japanese have no spaces, so a whole sentence is one
                    // "word" here and a cut between two of their characters is
                    // normal. Any other cut splits a real word. The check is per
                    // cut, not per word: one 「 or ？ next to a Russian word must
                    // not make the whole word fair game.
                    if (!(NO_SPACE_SCRIPT.test(prevCh) && NO_SPACE_SCRIPT.test(ch))) brokeWord = true;
                    lines.push(chunk);
                    chunk = '';
                    chunkWidth = 0;
                }
                chunk += ch;
                chunkWidth += chWidth;
                prevCh = ch;
            }
            line = chunk;
            lineWidth = chunkWidth;
            continue;
        }

        if (lineWidth === 0) {
            line = word;
            lineWidth = wordWidth;
        } else if (lineWidth + spaceWidth + wordWidth <= maxWidth) {
            line += ' ' + word;
            lineWidth += spaceWidth + wordWidth;
        } else {
            lines.push(line);
            line = word;
            lineWidth = wordWidth;
        }
    }

    if (line) lines.push(line);
    return { lines, brokeWord };
}

// Join OCR lines of one block into a single paragraph, by the same rule
// TextRecognizer uses when it merges lines (no space for Chinese/Japanese or
// after a trailing hyphen).
function joinOcrLines(text: string): string {
    const lines = text.split('\n').map(line => line.trim()).filter(line => line.length > 0);
    let joined = '';
    for (const line of lines) {
        if (!joined) {
            joined = line;
            continue;
        }
        joined += lineWrapSeparator(joined, line) + line;
    }
    return joined;
}

// Width available to the text inside a plate of `width`. The fitter and the
// layout must use the same formula: when they disagreed, a ~9px plate passed
// the fitter's check while the layout gave up and returned one unwrapped line,
// so the plate got a 29px font.
function overlayContentWidth(width: number, fontSize: number): number {
    return width - 2 * OVERLAY_PAD_X_EM * fontSize - OVERLAY_WRAP_SAFETY_PX;
}

function clampOverlayFontSize(fontSize: number): number {
    return Math.max(MIN_OVERLAY_FONT_SIZE, Math.min(MAX_OVERLAY_FONT_SIZE, fontSize));
}

// Lay the text out at a fixed font size; explicit line breaks are kept.
function layoutOverlayText(text: string, width: number, fontSize: number, measure: OverlayMeasure): WrappedText {
    const contentWidth = overlayContentWidth(width, fontSize);
    if (contentWidth <= 0) return { lines: [text.replace(/\s+/g, ' ').trim()], brokeWord: false };

    const paragraphs = text.split(/\n+/).map(p => p.trim()).filter(p => p.length > 0);
    const lines: string[] = [];
    let brokeWord = false;
    for (const paragraph of paragraphs) {
        const wrapped = wrapOverlayText(paragraph, contentWidth, fontSize, measure);
        lines.push(...wrapped.lines);
        if (wrapped.brokeWord) brokeWord = true;
    }
    return { lines, brokeWord };
}

interface OverlayFit {
    fontSize: number;
    lines: string[];
    // False when the text overflows the plate even at the minimum size.
    fits: boolean;
}

// Largest integer font size at which the text wraps into `width` x `height`.
// The old area heuristic guessed a size and then corrected it downwards; here
// the size comes from the layout itself, so wide blocks stop wasting the
// horizontal room on short lines.
function fitOverlayText(
    text: string,
    width: number,
    height: number,
    measure: OverlayMeasure,
): OverlayFit {
    const raw = text.trim();
    if (!raw || width <= 0 || height <= 0) {
        return { fontSize: MIN_OVERLAY_FONT_SIZE, lines: raw ? [raw] : [], fits: false };
    }

    const fits = (fontSize: number): boolean => {
        const contentWidth = overlayContentWidth(width, fontSize);
        const contentHeight = height - 2 * OVERLAY_PAD_Y_EM * fontSize;
        if (contentWidth <= 0 || contentHeight <= 0) return false;

        const layout = layoutOverlayText(raw, width, fontSize, measure);
        // A smaller font with whole words reads better than a bigger one with
        // "Сохра-/нение". Cutting words is left to the minimum size, where
        // there is no smaller option.
        if (layout.brokeWord) return false;
        return layout.lines.length * fontSize * OVERLAY_LINE_HEIGHT <= contentHeight;
    };

    let lo = MIN_OVERLAY_FONT_SIZE;
    let hi = MAX_OVERLAY_FONT_SIZE;
    let best = MIN_OVERLAY_FONT_SIZE - 1;

    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (fits(mid)) {
            best = mid;
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }

    const textFits = best >= MIN_OVERLAY_FONT_SIZE;
    const fontSize = textFits ? best : MIN_OVERLAY_FONT_SIZE;
    const { lines } = layoutOverlayText(raw, width, fontSize, measure);
    return { fontSize, lines: lines.length > 0 ? lines : [raw], fits: textFits };
}

// Narrowest plate width in (blockWidth, maxWidth] at which the text reaches
// `targetSize`, laid out at exactly that size. Label growth uses it so a plate
// grows only as much as the translation needs to match the original text,
// instead of taking all the free room and a font larger than the original.
// Font size grows with width, so a binary search over widths finds it.
function growOverlayText(
    text: string,
    blockWidth: number,
    maxWidth: number,
    height: number,
    targetSize: number,
    measure: OverlayMeasure,
): { width: number; fit: OverlayFit } {
    const reachesTarget = (fit: OverlayFit): boolean => fit.fits && fit.fontSize >= targetSize;

    // Even all the free room is not enough: take all of it, it is the best we can do.
    const widest = fitOverlayText(text, maxWidth, height, measure);
    if (!reachesTarget(widest)) return { width: maxWidth, fit: widest };

    let lo = blockWidth + 1;
    let hi = maxWidth;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (reachesTarget(fitOverlayText(text, mid, height, measure))) {
            hi = mid;
        } else {
            lo = mid + 1;
        }
    }

    // The narrowest width may allow a bit more than the target; stay at the
    // original's size, so a grown plate does not shout over its neighbours.
    const { lines } = layoutOverlayText(text, lo, targetSize, measure);
    return { width: lo, fit: { fontSize: targetSize, lines: lines.length > 0 ? lines : [text], fits: true } };
}

// Apply the user's fontScale to a fitted size. Scaling up may make a word wider
// than the plate; step back down until words are whole again, but not below
// the fitted size, where they were already whole (or cut at the minimum size,
// which is allowed). The plate may still grow taller — that is what a larger
// scale asks for.
function scaleOverlayText(
    text: string,
    width: number,
    fit: OverlayFit,
    fontScale: number,
    measure: OverlayMeasure,
): { fontSize: number; lines: string[] } {
    let fontSize = clampOverlayFontSize(Math.round(fit.fontSize * fontScale));
    if (fontSize === fit.fontSize) return { fontSize, lines: fit.lines };

    let layout = layoutOverlayText(text, width, fontSize, measure);
    while (layout.brokeWord && fontSize > fit.fontSize) {
        fontSize--;
        layout = layoutOverlayText(text, width, fontSize, measure);
    }
    return { fontSize, lines: layout.lines.length > 0 ? layout.lines : [text] };
}

// Overlay component to display translated text
export const TranslatedTextOverlay: VFC<{
    visible: boolean,
    imageData: string,
    regions: TranslatedRegion[],
    loading: boolean,
    processingStep: string,
    processingDetail: string,
    processingIsError: boolean,
    translationsVisible: boolean,
    fontScale: number,
    allowLabelGrowth: boolean,
    translatedTextAlignment: HorizontalTextAlignment,
    translatedTextFontFamily: string,
    translatedTextFontStyle: FontStyleOption,
    processingDataFlow: DataFlow | null,
    onDismiss: () => void
}> = ({ visible, imageData, regions, loading, processingStep, processingDetail, processingIsError, translationsVisible, fontScale, allowLabelGrowth, translatedTextAlignment, translatedTextFontFamily, translatedTextFontStyle, processingDataFlow, onDismiss }) => {
    // Composition layer is handled by CompositionRequest below -- only mounted when visible

    // Ref to the screenshot image element
    const imgRef = useRef<HTMLImageElement>(null);

    // State to track actual rendered image dimensions
    const [imageDimensions, setImageDimensions] = useState({ width: 0, height: 0 });

    // State to track the natural (original) image dimensions from the screenshot
    const [naturalDimensions, setNaturalDimensions] = useState({ width: 1280, height: 800 });

    // Load font as a side-effect (network request / DOM injection)
    useEffect(() => {
        ensureFontLoaded(translatedTextFontFamily);
    }, [translatedTextFontFamily]);

    // Plate font sizes and line breaks are measured on a canvas. A font that is
    // still loading (web fonts, and local @font-face ones, which load lazily on
    // first use) is measured with a fallback font, so the breaks would not match
    // what the browser draws once the real font arrives. Re-render when any font
    // finishes loading so the plates are measured again. Listening to the event
    // rather than to ensureFontLoaded's promise covers every font source.
    const [, setFontLoadCount] = useState(0);
    useEffect(() => {
        const fontSet = document.fonts;
        if (!fontSet) return;

        const handleFontsLoaded = () => setFontLoadCount(count => count + 1);
        fontSet.addEventListener('loadingdone', handleFontsLoaded);
        return () => fontSet.removeEventListener('loadingdone', handleFontsLoaded);
    }, []);

    // Pure computation — no side-effects
    const translatedOverlayFontFamily = useMemo(
        () => {
            const resolved = buildTranslatedFontFamily(translatedTextFontFamily);
            logger.debug('Overlay', `Font resolved: "${translatedTextFontFamily}" → "${resolved}"`);
            return resolved;
        },
        [translatedTextFontFamily]
    );

    const formattedImageData = imageData && imageData.startsWith('data:')
        ? imageData
        : imageData ? `data:image/png;base64,${imageData}` : "";

    // Update image dimensions when the image loads or window resizes
    const updateImageDimensions = useCallback(() => {
        if (imgRef.current) {
            const rect = imgRef.current.getBoundingClientRect();
            setImageDimensions(prev => {
                if (prev.width === rect.width && prev.height === rect.height) return prev;
                logger.debug('Overlay', `Rendered image dimensions: ${rect.width}x${rect.height}`);
                return { width: rect.width, height: rect.height };
            });

            const natWidth = imgRef.current.naturalWidth;
            const natHeight = imgRef.current.naturalHeight;
            if (natWidth > 0 && natHeight > 0) {
                setNaturalDimensions(prev => {
                    if (prev.width === natWidth && prev.height === natHeight) return prev;
                    logger.debug('Overlay', `Natural image dimensions: ${natWidth}x${natHeight}`);
                    return { width: natWidth, height: natHeight };
                });
            }
        }
    }, []);

    // Listen for window resize to update image dimensions
    useEffect(() => {
        window.addEventListener('resize', updateImageDimensions);
        return () => {
            window.removeEventListener('resize', updateImageDimensions);
        };
    }, [updateImageDimensions]);

    // Function to calculate the scaling factor based on actual rendered image size
    function getScalingFactor() {
        // Use natural image dimensions as base (the actual screenshot resolution)
        // OCR coordinates are based on these dimensions
        const baseWidth = naturalDimensions.width;
        const baseHeight = naturalDimensions.height;

        // Use actual rendered image dimensions if available
        let renderedWidth = imageDimensions.width;
        let renderedHeight = imageDimensions.height;

        // Fallback: try to get dimensions from the img element directly
        if ((renderedWidth === 0 || renderedHeight === 0) && imgRef.current) {
            const rect = imgRef.current.getBoundingClientRect();
            renderedWidth = rect.width;
            renderedHeight = rect.height;
        }

        // Final fallback: use viewport dimensions if image not yet loaded
        if (renderedWidth === 0 || renderedHeight === 0) {
            // Calculate based on viewport while maintaining aspect ratio
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            const aspectRatio = baseWidth / baseHeight;

            if (viewportWidth / viewportHeight > aspectRatio) {
                // Viewport is wider - height is the constraint
                renderedHeight = viewportHeight;
                renderedWidth = viewportHeight * aspectRatio;
            } else {
                // Viewport is taller - width is the constraint
                renderedWidth = viewportWidth;
                renderedHeight = viewportWidth / aspectRatio;
            }
        }

        return {
            widthFactor: renderedWidth / baseWidth,
            heightFactor: renderedHeight / baseHeight,
            generalFactor: ((renderedWidth / baseWidth) + (renderedHeight / baseHeight)) / 2
        };
    }

    return (
        <>
        <style>{`
            @keyframes statusSweep {
                0% { transform: translateX(-100%); }
                100% { transform: translateX(333%); }
            }
            @keyframes fadeInTranslation {
                0% { opacity: 0; }
                100% { opacity: 1; }
            }
        `}</style>
        {visible && <CompositionRequest level={UIComposition.Notification} />}
        <div id='translation-overlay'
             onClick={onDismiss}
             style={{
                 height: "100vh",
                 width: "100vw",
                 display: "flex",
                 justifyContent: "center",
                 alignItems: "center",
                 zIndex: 7002,
                 position: "fixed",
                 top: 0,
                 left: 0,
                 backgroundColor: "transparent",
                 opacity: visible ? 1 : 0,
                 pointerEvents: visible ? "auto" : "none",
             }}>

            {/* Screenshot with Translations */}
            {imageData && (
                <div style={{
                    position: "relative",
                    maxHeight: "100vh",
                    maxWidth: "100vw",
                }}>
                    {/* Base screenshot image */}
                    <img
                        ref={imgRef}
                        src={formattedImageData}
                        onLoad={updateImageDimensions}
                        style={{
                            maxHeight: "100vh",
                            maxWidth: "100vw",
                            objectFit: "contain",
                            imageRendering: "pixelated"
                        }}
                        alt="Скриншот"
                    />

                    {/* Dim the captured frame while processing, so it reads as a frozen snapshot.
                        Vignette keeps the centre (where labels are read) at 15 % and deepens the edges */}
                    <div style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        right: 0,
                        bottom: 0,
                        pointerEvents: "none",
                        background: "radial-gradient(ellipse at center, rgba(0, 0, 0, 0) 42%, rgba(0, 0, 0, 0.32) 100%), rgba(0, 0, 0, 0.15)",
                        opacity: loading ? 1 : 0,
                        transition: "opacity 0.15s ease-out",
                    }} />

                    {/* Frame corners mark the captured shot without turning it into a window border */}
                    <div style={{
                        position: "absolute",
                        top: 0,
                        left: 0,
                        right: 0,
                        bottom: 0,
                        pointerEvents: "none",
                    }}>
                        {frameCornerArms(11, 28, 1, "rgba(0, 0, 0, 0.55)")}
                        {frameCornerArms(
                            12,
                            26,
                            2,
                            translationsVisible ? "rgba(255, 255, 255, 0.55)" : "rgba(26, 159, 255, 0.9)"
                        )}
                    </div>

                    {/* Overlay translated text boxes with adaptive font sizing */}
                    {translationsVisible && (() => {
                        const { widthFactor, heightFactor } = getScalingFactor();
                        const pad = 4;
                        const gap = 2;
                        const imgWidth = imageDimensions.width || window.innerWidth;
                        // One per render: all plates share the font, so they share the width cache.
                        const measure = createOverlayMeasure(translatedOverlayFontFamily, translatedTextFontStyle);

                        // Pre-compute scaled rects for collision detection
                        const scaled = regions.map(region => ({
                            left: Math.round(region.rect.left * widthFactor - pad),
                            top: Math.round(region.rect.top * heightFactor - pad),
                            width: Math.round((region.rect.right - region.rect.left) * widthFactor + pad * 2),
                            height: Math.round((region.rect.bottom - region.rect.top) * heightFactor + pad * 2),
                        }));

                        // For each label, find how far it can grow in both directions
                        const expansionLimits = scaled.map((rect, i) => {
                            let maxRight = imgWidth;
                            let minLeft = 0;
                            const rectBottom = rect.top + rect.height;

                            for (let j = 0; j < scaled.length; j++) {
                                if (i === j) continue;
                                const other = scaled[j];

                                // Check vertical overlap
                                if (rect.top < other.top + other.height && rectBottom > other.top) {
                                    // Neighbor to the right
                                    if (other.left > rect.left) {
                                        maxRight = Math.min(maxRight, other.left - gap);
                                    }
                                    // Neighbor to the left
                                    if (other.left < rect.left) {
                                        minLeft = Math.max(minLeft, other.left + other.width + gap);
                                    }
                                }
                            }

                            const maxExpandRight = Math.max(0, maxRight - (rect.left + rect.width));
                            const maxExpandLeft = Math.max(0, rect.left - minLeft);

                            return { maxExpandRight, maxExpandLeft };
                        });

                        return regions.map((region, index) => {
                            const alignmentStyles =
                                translatedTextAlignment === 'right'
                                    ? { textAlign: 'right' as const, justifyContent: 'flex-end' as const }
                                    : translatedTextAlignment === 'center'
                                        ? { textAlign: 'center' as const, justifyContent: 'center' as const }
                                        : translatedTextAlignment === 'justify'
                                            ? { textAlign: 'justify' as const, justifyContent: 'flex-start' as const }
                                            : { textAlign: 'left' as const, justifyContent: 'flex-start' as const };

                            // Compute label position and size based on alignment and expansion
                            let labelMaxWidth = scaled[index].width;
                            // Use max-content width for right/center/justify so single-line
                            // blocks don't over-stretch while multi-line blocks still expand
                            let useMaxContentWidth = false;
                            // Position styles differ per alignment direction
                            let positionStyles: Record<string, string> = {
                                left: `${scaled[index].left}px`,
                            };

                            if (allowLabelGrowth) {
                                const { maxExpandRight, maxExpandLeft } = expansionLimits[index];

                                if (translatedTextAlignment === 'left') {
                                    // Expand to the right — anchor left edge, auto-size width
                                    labelMaxWidth = scaled[index].width + maxExpandRight;
                                    positionStyles = { left: `${scaled[index].left}px` };
                                } else if (translatedTextAlignment === 'right') {
                                    // Expand to the left — anchor right edge
                                    labelMaxWidth = scaled[index].width + maxExpandLeft;
                                    useMaxContentWidth = true;
                                    positionStyles = { right: `${imgWidth - (scaled[index].left + scaled[index].width)}px` };
                                } else {
                                    // Center or Justify — expand equally from center
                                    const expandEach = Math.min(maxExpandLeft, maxExpandRight);
                                    labelMaxWidth = scaled[index].width + expandEach * 2;
                                    useMaxContentWidth = true;
                                    const centerX = scaled[index].left + scaled[index].width / 2;
                                    positionStyles = { left: `${centerX}px`, transform: 'translateX(-50%)' };
                                }
                            }

                            // Fit the translation into the plate: the largest size at which
                            // the wrapped text still fits, instead of an area guess that had to
                            // be corrected downwards afterwards.
                            // OCR line breaks are kept unless the plate grows (see below): a block
                            // may be a menu or a list, and its lines must stay separate.
                            let blockText = region.translatedText || region.text;
                            let layoutWidth = scaled[index].width;
                            let fit = fitOverlayText(blockText, layoutWidth, scaled[index].height, measure);

                            // The setting promises growth only when the translation does not fit
                            // the block. "Fits" is judged against the original text: fitted into
                            // the same block, it shows the size the game used there. A translation
                            // that comes out noticeably smaller, or overflows even at the minimum
                            // size, grows — up to the original's size, not to all the free room.
                            if (allowLabelGrowth && labelMaxWidth > scaled[index].width) {
                                // Both are fitted with their OCR lines kept, so they are compared
                                // like for like.
                                const originalFit = fitOverlayText(region.text, scaled[index].width, scaled[index].height, measure);
                                const isNoticeablySmaller = fit.fontSize + MIN_GROWTH_FONT_SHORTFALL <= originalFit.fontSize;
                                if (isNoticeablySmaller || !fit.fits) {
                                    // A growing plate is re-wrapped freely, so the OCR line breaks
                                    // would only force extra short lines: join them into one paragraph.
                                    const joinedText = joinOcrLines(blockText);
                                    const grown = growOverlayText(
                                        joinedText,
                                        scaled[index].width,
                                        labelMaxWidth,
                                        scaled[index].height,
                                        originalFit.fontSize,
                                        measure,
                                    );
                                    // Keep the wider plate only if it actually helps: a larger font,
                                    // or, for text overflowing at the minimum size, a plate that fits
                                    // or at least overflows by fewer lines.
                                    const isLarger = grown.fit.fontSize > fit.fontSize;
                                    const overflowsLess = !fit.fits
                                        && (grown.fit.fits || grown.fit.lines.length < fit.lines.length);
                                    if (isLarger || overflowsLess) {
                                        blockText = joinedText;
                                        layoutWidth = grown.width;
                                        fit = grown.fit;
                                    }
                                }
                            }

                            // fontScale changes the rendered size, so the lines are laid out
                            // again to match what the browser will actually draw.
                            const scaledText = scaleOverlayText(blockText, layoutWidth, fit, fontScale, measure);
                            const fontSize = scaledText.fontSize;
                            const displayText = scaledText.lines.join('\n');

                            return (
                                <div
                                    key={index}
                                    style={{
                                        position: "absolute",
                                        display: 'flex',
                                        justifyContent: alignmentStyles.justifyContent,
                                        alignItems: 'center',
                                        ...positionStyles,
                                        top: `${scaled[index].top}px`,
                                        minWidth: `${scaled[index].width}px`,
                                        ...(useMaxContentWidth
                                            ? { width: 'max-content', maxWidth: `${labelMaxWidth}px` }
                                            : { maxWidth: `${labelMaxWidth}px` }),
                                        minHeight: `${scaled[index].height}px`,
                                        boxSizing: 'border-box',

                                        backgroundColor: "rgba(0, 0, 0, 0.8)",
                                        color: "#FFFFFF",

                                        padding: `${OVERLAY_PAD_Y_EM}em ${OVERLAY_PAD_X_EM}em`,
                                        borderRadius: '0.22em',
                                        boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.12), 0 2px 6px rgba(0,0,0,0.45)",

                                        fontSize: `${fontSize}px`,
                                        lineHeight: String(OVERLAY_LINE_HEIGHT),
                                        ...resolveFontStyleCSS(translatedTextFontStyle),
                                        fontFamily: translatedOverlayFontFamily,

                                        wordWrap: "break-word",
                                        whiteSpace: "pre-wrap",

                                        animation: "fadeInTranslation 0.35s ease-out both",
                                        animationDelay: `${Math.min(index * 55, 330)}ms`
                                    }}
                                >
                                    <div style={{
                                        width: '100%',
                                        textAlign: alignmentStyles.textAlign,
                                        textAlignLast: translatedTextAlignment === 'justify' ? 'justify' : alignmentStyles.textAlign,
                                    }}>
                                        {displayText}
                                    </div>
                                </div>
                            );
                        });
                    })()}

                    {/* Indicator when translations are hidden - eye closed icon */}
                    {!translationsVisible && !loading && (
                        <div style={{
                            position: "absolute",
                            bottom: "20px",
                            left: "20px",
                            background: "rgba(0, 0, 0, 0.7)",
                            padding: '10px',
                            borderRadius: '50%',
                            zIndex: 7003,
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                        }}>
                            <svg
                                width="24"
                                height="24"
                                viewBox="0 0 24 24"
                                fill="none"
                                stroke="#1a9fff"
                                strokeWidth="2"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                            >
                                {/* Eye closed icon */}
                                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                                <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                                <path d="M1 1l22 22" />
                                <path d="M8.71 8.71a4 4 0 1 0 5.66 5.66" />
                            </svg>
                        </div>
                    )}
                </div>
            )}

            {/* Status pill: sweeping line while working, plain text for short messages */}
            {processingStep && (
                <div style={{
                    display: "flex",
                    flexDirection: "row",
                    alignItems: "center",
                    position: "absolute",
                    bottom: "20px",
                    left: "20px",
                    overflow: "hidden",
                    color: "#ffffff",
                    background: "rgba(0, 0, 0, 0.7)",
                    padding: '8px 12px',
                    borderRadius: '20px',
                    maxWidth: "420px",
                    boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
                    zIndex: 7003, // Higher than the image
                }}>
                    {processingIsError && (
                        <svg
                            width="18"
                            height="18"
                            viewBox="0 0 24 24"
                            fill="#ff6b6b"
                            style={{ flexShrink: 0, marginRight: "10px" }}
                        >
                            <path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z" />
                        </svg>
                    )}
                    {/* Where data goes: the current stage while working, the whole run on
                        final statuses and errors (after the error icon: the error matters more).
                        Shape and color only, no label: a local stage can be over in a fraction
                        of a second. */}
                    {processingDataFlow && (
                        <span style={{ display: "inline-flex", flexShrink: 0, marginRight: "10px" }}>
                            <DataFlowIconGlyph flow={processingDataFlow} size={16} />
                        </span>
                    )}
                    <div style={{ fontSize: "14px", whiteSpace: "pre-line", lineHeight: "1.3" }}>
                        {processingIsError || !loading ? processingStep : `${processingStep}...`}
                        {!processingIsError && processingDetail && (
                            <div style={{ fontSize: "10px", opacity: 0.7 }}>{processingDetail}</div>
                        )}
                    </div>
                    {loading && !processingIsError && (
                        <>
                            <div style={{
                                position: "absolute",
                                left: 0,
                                right: 0,
                                bottom: 0,
                                height: "2px",
                                background: "rgba(255, 255, 255, 0.10)",
                            }} />
                            <div style={{
                                position: "absolute",
                                bottom: 0,
                                left: 0,
                                width: "30%",
                                height: "2px",
                                borderRadius: "2px",
                                background: "#1a9fff",
                                animation: "statusSweep 1.3s ease-in-out infinite",
                            }} />
                        </>
                    )}
                </div>
            )}
        </div>
        </>
    );
};



// Main image overlay component
export const ImageOverlay: VFC<{ state: ImageState, onDismiss: () => void }> = ({ state, onDismiss }) => {
    const [visible, setVisible] = useState<boolean>(false);
    const [imageData, setImageData] = useState<string>("");
    const [regions, setRegions] = useState<TranslatedRegion[]>([]);
    const [loading, setLoading] = useState<boolean>(false);
    const [processingStep, setProcessingStep] = useState<string>("");
    const [processingDetail, setProcessingDetail] = useState<string>("");
    const [processingIsError, setProcessingIsError] = useState<boolean>(false);
    const [translationsVisible, setTranslationsVisible] = useState<boolean>(true);
    const [fontScale, setFontScale] = useState<number>(1.0);
    const [allowLabelGrowth, setAllowLabelGrowth] = useState<boolean>(true);
    const [translatedTextAlignment, setTranslatedTextAlignment] = useState<HorizontalTextAlignment>('center');
    const [translatedTextFontFamily, setTranslatedTextFontFamily] = useState<string>("");
    const [translatedTextFontStyle, setTranslatedTextFontStyle] = useState<FontStyleOption>('normal');
    const [processingDataFlow, setProcessingDataFlow] = useState<DataFlow | null>(null);

    useEffect(() => {
        logger.debug('ImageOverlay', 'useEffect mounting, registering state listener');

        const handleStateChanged = (
            isVisible: boolean,
            imgData: string,
            textRegions: TranslatedRegion[],
            isLoading: boolean,
            currProcessingStep: string,
            currProcessingDetail: string,
            currProcessingIsError: boolean,
            areTranslationsVisible: boolean,
            currentFontScale: number,
            currentAllowLabelGrowth: boolean,
            currentTranslatedTextAlignment: HorizontalTextAlignment,
            currentTranslatedTextFontFamily: string,
            currentTranslatedTextFontStyle: FontStyleOption,
            currProcessingDataFlow: DataFlow | null
        ) => {
            logger.debug('ImageOverlay', `State changed - visible=${isVisible}, imgData.length=${imgData?.length || 0}, regions=${textRegions?.length || 0}`);
            setVisible(isVisible);
            setImageData(imgData);
            setRegions(textRegions);
            setLoading(isLoading);
            setProcessingStep(currProcessingStep);
            setProcessingDetail(currProcessingDetail);
            setProcessingIsError(currProcessingIsError);
            setTranslationsVisible(areTranslationsVisible);
            setFontScale(currentFontScale);
            setAllowLabelGrowth(currentAllowLabelGrowth);
            setTranslatedTextAlignment(currentTranslatedTextAlignment);
            setTranslatedTextFontFamily(currentTranslatedTextFontFamily);
            setTranslatedTextFontStyle(currentTranslatedTextFontStyle);
            setProcessingDataFlow(currProcessingDataFlow);
        };

        state.onStateChanged(handleStateChanged);

        const suspend_register = SteamClient.User.RegisterForPrepareForSystemSuspendProgress(() => {
            onDismiss();
        });

        return () => {
            state.offStateChanged(handleStateChanged);
            suspend_register.unregister();
        };
    }, [state, onDismiss]);

    return (
        <TranslatedTextOverlay
            visible={visible}
            imageData={imageData}
            regions={regions}
            loading={loading}
            processingStep={processingStep}
            processingDetail={processingDetail}
            processingIsError={processingIsError}
            translationsVisible={translationsVisible}
            fontScale={fontScale}
            allowLabelGrowth={allowLabelGrowth}
            translatedTextAlignment={translatedTextAlignment}
            translatedTextFontFamily={translatedTextFontFamily}
            translatedTextFontStyle={translatedTextFontStyle}
            processingDataFlow={processingDataFlow}
            onDismiss={onDismiss}
        />
    );
};