import { call } from "@decky/api";
import { logger } from "./Logger";
import { SETTINGS_DEFAULTS } from "./settingsDefaults";

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

// CJK scripts (Chinese, Japanese, Korean) and fullwidth forms.
const CJK_CHAR = /[\u3000-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]/;

// Scripts written without spaces between words: Chinese and Japanese.
// Same as CJK_CHAR minus Hangul syllables: Korean separates words with
// spaces, so its wrapped lines must be joined with a space.
export const NO_SPACE_SCRIPT = /[\u3000-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF]/;

// Spacing between wrapped lines of one text block. The block is one phrase,
// so lines join into a single line: with "\n" translators split it back and
// translate each line on its own. Also used by the overlay to re-join OCR
// lines, so both join them the same way.
// getHorizontalSpacing is not reused: it treats quotes as punctuation and
// would glue `"Come here."` + `She left` into `"Come here."She left`.
export function lineWrapSeparator(prevLine: string, nextLine: string): string {
    // A trailing hyphen is kept and glued without a space: "self-" + "aware"
    // becomes "self-aware". A word break ("possi-" + "ble") can't be told
    // apart without a dictionary; games rarely hyphenate, so keeping the
    // hyphen is the safer guess.
    if (prevLine.endsWith("-")) {
        return "";
    }
    if (NO_SPACE_SCRIPT.test(prevLine.slice(-1)) || NO_SPACE_SCRIPT.test(nextLine.charAt(0))) {
        return "";
    }
    return " ";
}

// Replaced with true/false at build time by rollup.config.js.
declare const __DEBUG_BLOCK_ATTACH_LOG__: boolean;

// EXPERIMENT: attach lines that Screen AI split off a block.
// Screen AI often gives the short last line of a paragraph (sometimes several
// lines) its own block id. Block groups never meet the geometry pass, so such
// a line stayed alone and was translated as a separate phrase. The attach pass
// grows each block downward with lines that sit exactly where the block's
// next line would be. All thresholds are relative to the block itself, not
// to the screen median: games with wide line spacing were the reason block
// ids were introduced (geometry split those dialogues).
const BLOCK_ATTACH = {
    enabled: true,
    // Appends " [+N]" to every attached line (N = block id it was attached
    // to) so attach decisions are visible right in the overlay.
    // The tag goes into the translated text and can distort the translation,
    // so it is off; attach decisions are in the debug log.
    debugLabels: false,
    // Sends the screen layout and attach decisions to the plugin log
    // (main.py, log_block_attach): the frontend console needs CEF remote
    // debugging, the plugin log is one file to grab after playing.
    // Debug only, off in releases: every Screen AI screen lands in the log
    // with its text (what the player reads, chats included), which users then
    // attach to bug reports, and it adds 10-15 log lines per screen.
    // Set at build time (rollup.config.js): on only when the build runs with
    // DEBUG_BLOCK_ATTACH_LOG=1, which build-plugin-zip.sh does for local
    // builds. CI does not set it, so release builds never write this log.
    debugLogToBackend: __DEBUG_BLOCK_ATTACH_LOG__,
    // Candidate top may differ from "last line top + block pitch" by this
    // share of the pitch.
    pitchTolerance: 0.25,
    // Line height compared to the block's median line height (font size must
    // match). The limits are not symmetric: a line box shrinks to the letters
    // it holds, and a short tail without ascenders or descenders ("now.",
    // "I think.") is much lower than a full line of the same font, down to
    // about the lowercase letter height. A taller line is a different font.
    maxHeightRatio: 1.25,
    minHeightRatio: 0.55,
    // Left edges (left-aligned text) or centers (centered text) may differ
    // by this many block line heights.
    alignTolerance: 1.0,
    // Max ratio between a continuation width and the widest block line.
    // Right edges of prose are ragged: in recorded screens a line in the
    // same block was up to 1.3 times wider than every line above it, so a
    // tight limit would drop a full next line split off by Screen AI.
    maxWidthRatio: 1.35,
    // A line narrower than this share of the block width is "short".
    // A short line ends a paragraph: nothing is attached after it, and a short
    // line followed by more text is a header (character name), not a tail.
    shortLineRatio: 0.6,
    // Starting a block from two lone lines, for screens where Screen AI gave
    // every line its own block id. There is no block yet to take the line
    // step from, so it is guessed from the upper line height; these limits
    // are stricter than the ones for growing a block.
    // The upper line must be at least this many of its own heights wide:
    // a wrapped line of prose is (~20 in recorded dialogue), a character
    // name (~3) or a menu item is not. Without it a menu, an item list or
    // a stats table would be glued into one phrase.
    seedMinWidthInHeights: 10,
    // Distance between the tops of the two lines, in upper line heights.
    // Recorded dialogue lines: 1.46-1.83. Below 1.2 the lower line nearly
    // touches the upper one: in the Steam library a game title and the
    // play time under it stood at 1.14.
    seedMinPitchRatio: 1.2,
    seedMaxPitchRatio: 2.0,
    // Left edges of the two lines may differ by this many upper line heights
    // (stricter than alignTolerance). Guards against a long character name
    // above a short one-line reply: such a name passes the width check, and
    // in a game that puts names closer to the text it passes the step check
    // too. Names stand 11-32 px left of the text in recorded screens (about
    // 0.5-1.4 name heights), lines of one paragraph 0-4 px. Centered text is
    // still accepted by the center check with the usual tolerance.
    seedLeftAlignTolerance: 0.4,
};

function median(values: number[]): number {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
}

function regionWidth(region: TextRegion): number {
    return region.rect.right - region.rect.left;
}

function regionHeight(region: TextRegion): number {
    return region.rect.bottom - region.rect.top;
}

function regionCenterX(region: TextRegion): number {
    return (region.rect.left + region.rect.right) / 2;
}

// Geometry of a Screen AI block, measured once on its own lines. Kept fixed
// while lines are attached so a wrong attach can't drift the thresholds.
interface BlockShape {
    left: number;
    right: number;
    width: number;       // widest line, not the union rect
    lineHeight: number;  // median line height
    pitch: number;       // median distance between tops of consecutive lines
}

// Working state of the attach pass. A unit is a list of line indices
// (a block or a lone line); a unit merged into another one is "consumed".
interface AttachState {
    regions: TextRegion[];
    labeled: TextRegion[];   // regions with debug labels, if enabled
    units: number[][];
    consumed: Set<number>;
    note: (message: string) => void;
}

// Union-Find with path compression and union by rank.
// Enables transitive merging: if A merges with B and B with C,
// all three end up in the same group regardless of check order.
class UnionFind {
    private parent: number[];
    private rank: number[];

    constructor(n: number) {
        this.parent = Array.from({ length: n }, (_, i) => i);
        this.rank = new Array(n).fill(0);
    }

    find(x: number): number {
        while (this.parent[x] !== x) {
            this.parent[x] = this.parent[this.parent[x]];
            x = this.parent[x];
        }
        return x;
    }

    union(x: number, y: number): void {
        const px = this.find(x), py = this.find(y);
        if (px === py) return;
        if (this.rank[px] < this.rank[py]) {
            this.parent[px] = py;
        } else if (this.rank[px] > this.rank[py]) {
            this.parent[py] = px;
        } else {
            this.parent[py] = px;
            this.rank[px]++;
        }
    }

    getGroups(): Map<number, number[]> {
        const groups = new Map<number, number[]>();
        for (let i = 0; i < this.parent.length; i++) {
            const root = this.find(i);
            if (!groups.has(root)) groups.set(root, []);
            groups.get(root)!.push(i);
        }
        return groups;
    }
}

function computeMedianHeight(regions: TextRegion[]): number {
    const heights = regions
        .map(r => r.rect.bottom - r.rect.top)
        .filter(h => h > 0)
        .sort((a, b) => a - b);

    if (heights.length === 0) return 20;
    return heights[Math.floor(heights.length / 2)];
}

export class TextRecognizer {
    private confidenceThreshold: number = SETTINGS_DEFAULTS.confidenceThreshold;
    private groupingPower: number = SETTINGS_DEFAULTS.groupingPower;

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
        this.groupingPower = Math.max(0.25, Math.min(1.0, power));
        logger.debug('TextRecognizer', `Grouping power set to ${this.groupingPower}`);
    }

    getGroupingPower(): number {
        return this.groupingPower;
    }

    // Phase 1: group OCR boxes that sit on the same horizontal line.
    // Checks: Y-center alignment, height compatibility, horizontal gap.
    // All thresholds relative to median line height -- never absolute pixels.
    private assembleLines(regions: TextRegion[], medianH: number): TextRegion[] {
        const n = regions.length;
        if (n <= 1) return [...regions];

        const uf = new UnionFind(n);

        // Sort by top edge so we can break early on large vertical gaps
        const indexed = regions.map((r, i) => ({ r, i }));
        indexed.sort((a, b) => a.r.rect.top - b.r.rect.top);

        for (let a = 0; a < n; a++) {
            for (let b = a + 1; b < n; b++) {
                const ra = indexed[a].r, rb = indexed[b].r;

                // If the top of b is way past the bottom of a, everything
                // further is even lower -- stop checking
                if (rb.rect.top - ra.rect.bottom > medianH) break;

                const yCenterA = (ra.rect.top + ra.rect.bottom) / 2;
                const yCenterB = (rb.rect.top + rb.rect.bottom) / 2;
                if (Math.abs(yCenterA - yCenterB) > 0.5 * medianH) continue;

                const hA = ra.rect.bottom - ra.rect.top;
                const hB = rb.rect.bottom - rb.rect.top;
                if (hA > 0 && hB > 0 && Math.max(hA, hB) / Math.min(hA, hB) > 1.5) continue;

                // Horizontal gap: positive means space between boxes, negative means overlap
                const gap = Math.max(ra.rect.left, rb.rect.left) - Math.min(ra.rect.right, rb.rect.right);
                if (gap > 1.0 * medianH) continue;

                uf.union(indexed[a].i, indexed[b].i);
            }
        }

        const groups = Array.from(uf.getGroups().values());
        return this.extractMergedRegions(groups, regions, "horizontal");
    }

    // Phase 2: group text lines into paragraphs.
    // Checks: vertical gap, horizontal overlap ratio, height compatibility.
    // groupingPower scales the vertical gap threshold.
    private assembleParagraphs(lines: TextRegion[], medianH: number): TextRegion[] {
        const n = lines.length;
        if (n <= 1) return [...lines];

        const power = this.groupingPower;
        const uf = new UnionFind(n);

        const indexed = lines.map((l, i) => ({ l, i }));
        indexed.sort((a, b) => a.l.rect.top - b.l.rect.top);

        for (let a = 0; a < n; a++) {
            for (let b = a + 1; b < n; b++) {
                const la = indexed[a].l, lb = indexed[b].l;

                // Early exit on large vertical distance
                if (lb.rect.top - la.rect.bottom > 3.0 * medianH * power) break;

                // Vertical gap
                const vertGap = Math.max(0, lb.rect.top - la.rect.bottom);
                if (vertGap > 1.5 * medianH * power) continue;

                // Horizontal overlap ratio -- how much do the two lines share horizontally?
                const overlapLeft = Math.max(la.rect.left, lb.rect.left);
                const overlapRight = Math.min(la.rect.right, lb.rect.right);
                const overlapWidth = Math.max(0, overlapRight - overlapLeft);
                const widthA = la.rect.right - la.rect.left;
                const widthB = lb.rect.right - lb.rect.left;
                const minWidth = Math.min(widthA, widthB);

                if (minWidth > 0 && overlapWidth / minWidth < 0.3) continue;

                // Height ratio (font size similarity).
                // For multi-line regions (e.g. OCR returned a block spanning
                // multiple visual lines), normalize by estimated line count
                // so the comparison reflects per-line font size, not total height.
                const hA = la.rect.bottom - la.rect.top;
                const hB = lb.rect.bottom - lb.rect.top;
                const estLinesA = Math.max(1, Math.round(hA / medianH));
                const estLinesB = Math.max(1, Math.round(hB / medianH));
                const lineHA = hA / estLinesA;
                const lineHB = hB / estLinesB;
                if (lineHA > 0 && lineHB > 0 && Math.max(lineHA, lineHB) / Math.min(lineHA, lineHB) > 1.4) continue;

                // Alignment check: reject pairs whose left edges, right edges,
                // AND horizontal centers are all far apart.  This allows
                // left-aligned, right-aligned, and center-aligned text to merge
                // while still separating unrelated UI elements.
                const leftEdgeDist = Math.abs(la.rect.left - lb.rect.left);
                const rightEdgeDist = Math.abs(la.rect.right - lb.rect.right);
                const centerA = (la.rect.left + la.rect.right) / 2;
                const centerB = (lb.rect.left + lb.rect.right) / 2;
                const centerDist = Math.abs(centerA - centerB);
                const edgeThresh = 2.0 * medianH;
                const centerThresh = 1.0 * medianH;
                if (leftEdgeDist > edgeThresh && rightEdgeDist > edgeThresh && centerDist > centerThresh) continue;

                // Width ratio: a very narrow line next to a wide one is probably
                // a stray label, not a continuation. Skip this check for the
                // shorter of two lines when it could be a paragraph's last line
                // (i.e. when it's the lower one)
                const maxWidth = Math.max(widthA, widthB);
                if (maxWidth > 0) {
                    const widthRatio = minWidth / maxWidth;
                    // If the narrow line is the upper one, it's unlikely a paragraph tail
                    const narrowIsUpper = (widthA < widthB && la.rect.top < lb.rect.top) ||
                                          (widthB < widthA && lb.rect.top < la.rect.top);
                    if (widthRatio < 0.15 && narrowIsUpper) continue;
                }

                // Background color: different background = different UI element.
                // Uses simple RGB distance. Threshold of 60 catches obvious
                // differences (dark vs light, blue vs gray) while allowing
                // minor shading variation within the same panel.
                if (la.bgColor && lb.bgColor) {
                    const dr = la.bgColor[0] - lb.bgColor[0];
                    const dg = la.bgColor[1] - lb.bgColor[1];
                    const db = la.bgColor[2] - lb.bgColor[2];
                    const colorDist = Math.sqrt(dr * dr + dg * dg + db * db);
                    if (colorDist > 60) continue;
                }

                uf.union(indexed[a].i, indexed[b].i);
            }
        }

        const groups = Array.from(uf.getGroups().values());
        const refined = this.splitOvermerged(groups, lines, medianH);
        logger.debug('TextRecognizer', `Auto-glue: Split pass refined ${groups.length} groups into ${refined.length} groups`);
        return this.extractMergedRegions(refined, lines, "vertical");
    }

    // Post-merge split pass: break apart over-merged groups by checking
    // for internal gap inconsistencies and separator lines.
    private splitOvermerged(groups: number[][], lines: TextRegion[], medianH: number): number[][] {
        const result: number[][] = [];

        for (const group of groups) {
            // Groups of 1-2 lines can't have internal inconsistencies
            if (group.length <= 2) {
                result.push(group);
                continue;
            }

            // Sort constituent lines top-to-bottom
            const sorted = [...group].sort((a, b) => {
                const yA = (lines[a].rect.top + lines[a].rect.bottom) / 2;
                const yB = (lines[b].rect.top + lines[b].rect.bottom) / 2;
                return yA - yB;
            });

            // Compute vertical gaps between consecutive lines
            const gaps: number[] = [];
            for (let i = 0; i < sorted.length - 1; i++) {
                gaps.push(Math.max(0, lines[sorted[i + 1]].rect.top - lines[sorted[i]].rect.bottom));
            }

            const sortedGaps = [...gaps].sort((a, b) => a - b);
            const medianGap = sortedGaps[Math.floor(sortedGaps.length / 2)];

            // Find indices after which to split (between sorted[i] and sorted[i+1])
            const splitAfter = new Set<number>();

            for (let i = 0; i < gaps.length; i++) {
                // Gap inconsistency: this gap is much larger than the group's typical gap,
                // and large enough to not just be bounding box jitter
                if (medianGap > 0 && gaps[i] > 2.5 * medianGap && gaps[i] > 0.8 * medianH) {
                    splitAfter.add(i);
                }
                // When lines nearly overlap (medianGap ~ 0), any real gap stands out
                if (medianGap <= 1 && gaps[i] > 1.2 * medianH) {
                    splitAfter.add(i);
                }

                // Separator line: text is just dashes, equals, underscores, etc.
                const text = lines[sorted[i]].text.trim();
                if (/^[-=_*~.]{3,}$/.test(text)) {
                    if (i > 0) splitAfter.add(i - 1);
                    splitAfter.add(i);
                }
                if (i + 1 < sorted.length) {
                    const nextText = lines[sorted[i + 1]].text.trim();
                    if (/^[-=_*~.]{3,}$/.test(nextText)) {
                        splitAfter.add(i);
                    }
                }
            }

            if (splitAfter.size === 0) {
                result.push(sorted);
                continue;
            }

            // Build sub-groups at split points
            let subGroup: number[] = [];
            for (let i = 0; i < sorted.length; i++) {
                subGroup.push(sorted[i]);
                if (splitAfter.has(i)) {
                    if (subGroup.length > 0) result.push(subGroup);
                    subGroup = [];
                }
            }
            if (subGroup.length > 0) result.push(subGroup);
        }

        return result;
    }

    // Extract connected components from Union-Find, merge text and bounding boxes.
    // "paragraph" is "vertical" for lines of one Screen AI block: same
    // top-to-bottom order, but lines are joined into one phrase, not with "\n".
    private extractMergedRegions(
        groups: number[][],
        regions: TextRegion[],
        direction: "horizontal" | "vertical" | "paragraph"
    ): TextRegion[] {
        const merged: TextRegion[] = [];

        for (const indices of groups) {
            if (indices.length === 1) {
                merged.push({ ...regions[indices[0]] });
                continue;
            }

            // Sort: left-to-right for horizontal, top-to-bottom for vertical
            const sorted = indices
                .map(i => regions[i])
                .sort((a, b) => {
                    if (direction === "horizontal") return a.rect.left - b.rect.left;
                    const yCenterA = (a.rect.top + a.rect.bottom) / 2;
                    const yCenterB = (b.rect.top + b.rect.bottom) / 2;
                    return yCenterA - yCenterB;
                });

            let combinedText = sorted[0].text;
            let combinedTranslatedText = sorted[0].translatedText ?? null;
            let combinedRect = { ...sorted[0].rect };
            let isDialog = sorted[0].isDialog;
            let confidenceSum = sorted[0].confidence ?? 0;
            let confidenceCount = sorted[0].confidence !== undefined ? 1 : 0;
            // Accumulate bgColor for averaging
            let colorR = 0, colorG = 0, colorB = 0, colorCount = 0;
            if (sorted[0].bgColor) {
                colorR += sorted[0].bgColor[0];
                colorG += sorted[0].bgColor[1];
                colorB += sorted[0].bgColor[2];
                colorCount++;
            }

            for (let i = 1; i < sorted.length; i++) {
                const region = sorted[i];
                let separator = "\n";
                if (direction === "horizontal") {
                    separator = this.getHorizontalSpacing(sorted[i - 1], region);
                } else if (direction === "paragraph") {
                    separator = lineWrapSeparator(sorted[i - 1].text, region.text);
                }

                combinedText += separator + region.text;
                if (combinedTranslatedText !== null && region.translatedText) {
                    combinedTranslatedText += separator + region.translatedText;
                }

                combinedRect = {
                    left: Math.min(combinedRect.left, region.rect.left),
                    top: Math.min(combinedRect.top, region.rect.top),
                    right: Math.max(combinedRect.right, region.rect.right),
                    bottom: Math.max(combinedRect.bottom, region.rect.bottom),
                };

                isDialog = isDialog || region.isDialog;
                if (region.confidence !== undefined) {
                    confidenceSum += region.confidence;
                    confidenceCount++;
                }
                if (region.bgColor) {
                    colorR += region.bgColor[0];
                    colorG += region.bgColor[1];
                    colorB += region.bgColor[2];
                    colorCount++;
                }
            }

            const result: TextRegion = {
                text: combinedText,
                rect: combinedRect,
                isDialog,
            };
            if (combinedTranslatedText !== null) {
                result.translatedText = combinedTranslatedText;
            }
            if (confidenceCount > 0) {
                result.confidence = confidenceSum / confidenceCount;
            }
            if (colorCount > 0) {
                result.bgColor = [
                    Math.round(colorR / colorCount),
                    Math.round(colorG / colorCount),
                    Math.round(colorB / colorCount)
                ];
            }
            merged.push(result);
        }

        return merged;
    }

    // Spacing for horizontal (same-line) merging
    private getHorizontalSpacing(a: TextRegion, b: TextRegion): string {
        // No space before closing punctuation
        if (/^[.,!?:;)\]"'\u3002\u3001\uFF09\u300D\u300F\u3011\u3009\u300B)]/.test(b.text)) {
            return "";
        }
        // No space after opening punctuation
        if (/[(\["'\uFF08\u300C\u300E\u3010\u3008\u300A(]\s*$/.test(a.text)) {
            return "";
        }

        if (CJK_CHAR.test(a.text.slice(-1)) || CJK_CHAR.test(b.text.charAt(0))) {
            return "";
        }

        return " ";
    }

    // Post-merge dialog detection on complete text blocks
    private detectDialog(region: TextRegion): boolean {
        if (region.isDialog) return true;

        const text = region.text;
        let score = 0;

        if (/"[^"]+"/g.test(text) || /[\u00AB\u00BB]/g.test(text)) score += 2;

        const excl = (text.match(/!/g) || []).length;
        const quest = (text.match(/\?/g) || []).length;
        score += (excl + quest) * 0.5;

        if (/[A-Z][^.!?]+[.!?]\s*"/.test(text) || /"[^"]+"\s*[A-Z][^.!?]+[.!?]/.test(text)) {
            score += 2;
        }

        score += Math.min(2, text.length / 50);
        if (text.includes('\n')) score += 1.5;

        return score >= 3;
    }

    // Resolve overlapping rects in the final region list.
    // Merged paragraphs can have union rects that extend into neighbors.
    // For each overlapping pair: absorb the smaller if mostly contained,
    // otherwise trim the smaller region's rect at the overlapping edge.
    private resolveOverlaps(regions: TextRegion[]): TextRegion[] {
        if (regions.length <= 1) return regions;

        // Work on copies so we can mutate rects
        const result = regions.map(r => ({ ...r, rect: { ...r.rect } }));

        // Sort by area descending so larger (merged) regions take priority
        const byArea = result
            .map((r, i) => ({ i, area: (r.rect.right - r.rect.left) * (r.rect.bottom - r.rect.top) }))
            .sort((a, b) => b.area - a.area);

        const removed = new Set<number>();

        for (let ai = 0; ai < byArea.length; ai++) {
            const idxA = byArea[ai].i;
            if (removed.has(idxA)) continue;
            const a = result[idxA].rect;

            for (let bi = ai + 1; bi < byArea.length; bi++) {
                const idxB = byArea[bi].i;
                if (removed.has(idxB)) continue;
                const b = result[idxB].rect;

                // Check overlap
                const oLeft = Math.max(a.left, b.left);
                const oTop = Math.max(a.top, b.top);
                const oRight = Math.min(a.right, b.right);
                const oBottom = Math.min(a.bottom, b.bottom);

                if (oLeft >= oRight || oTop >= oBottom) continue;

                const overlapArea = (oRight - oLeft) * (oBottom - oTop);
                const bArea = (b.right - b.left) * (b.bottom - b.top);

                // If b is mostly inside a, absorb it
                if (bArea > 0 && overlapArea / bArea > 0.5) {
                    removed.add(idxB);
                    continue;
                }

                // Partial overlap: trim b at the edge that loses the least area
                const bW = b.right - b.left;
                const bH = b.bottom - b.top;
                const oW = oRight - oLeft;
                const oH = oBottom - oTop;

                // For each possible trim, how much of b's area is lost?
                // Only consider trims where the overlap is actually on that edge
                const candidates: { edge: string; loss: number }[] = [];

                // Trim b's left edge rightward (overlap is on b's left side)
                if (oLeft === b.left || oLeft - b.left < oW) {
                    candidates.push({ edge: 'left', loss: oW * bH });
                }
                // Trim b's right edge leftward (overlap is on b's right side)
                if (oRight === b.right || b.right - oRight < oW) {
                    candidates.push({ edge: 'right', loss: oW * bH });
                }
                // Trim b's top edge downward
                if (oTop === b.top || oTop - b.top < oH) {
                    candidates.push({ edge: 'top', loss: bW * oH });
                }
                // Trim b's bottom edge upward
                if (oBottom === b.bottom || b.bottom - oBottom < oH) {
                    candidates.push({ edge: 'bottom', loss: bW * oH });
                }

                if (candidates.length === 0) {
                    removed.add(idxB);
                    continue;
                }

                // Pick the trim with least area loss
                candidates.sort((x, y) => x.loss - y.loss);
                switch (candidates[0].edge) {
                    case 'left': b.left = oRight; break;
                    case 'right': b.right = oLeft; break;
                    case 'top': b.top = oBottom; break;
                    case 'bottom': b.bottom = oTop; break;
                }

                // If trimmed to nothing useful, remove it
                if (b.right - b.left < 5 || b.bottom - b.top < 5) {
                    removed.add(idxB);
                }
            }
        }

        return result.filter((_, i) => !removed.has(i));
    }

    // Screen AI reports which visual block a line belongs to; group by that id
    // before any geometry so large line spacing cannot split a dialogue block.
    // Returns indices, not regions: the attach pass needs to know which lines
    // are still alone.
    private groupByBlockId(regions: TextRegion[]): {
        groups: number[][];
        ungroupedIndices: number[];
    } {
        const byId = new Map<number, number[]>();
        const ungroupedIndices: number[] = [];

        regions.forEach((region, index) => {
            const blockId = region.blockId;
            if (blockId === undefined || blockId <= 0) {
                ungroupedIndices.push(index);
                return;
            }
            const group = byId.get(blockId);
            if (group) {
                group.push(index);
            } else {
                byId.set(blockId, [index]);
            }
        });

        // Only multi-line groups are useful. If Screen AI gives every line its
        // own block, leave those lines to the geometry pass so the previous
        // behavior is preserved.
        const groups: number[][] = [];
        for (const group of byId.values()) {
            if (group.length > 1) {
                groups.push(group);
            } else {
                ungroupedIndices.push(group[0]);
            }
        }

        return { groups, ungroupedIndices };
    }

    private sortTopToBottom(indices: number[], regions: TextRegion[]): number[] {
        return [...indices].sort((a, b) => regions[a].rect.top - regions[b].rect.top);
    }

    // `lines` are indices sorted top to bottom, at least two of them.
    private describeBlock(lines: number[], regions: TextRegion[]): BlockShape | null {
        const pitches: number[] = [];
        for (let i = 0; i + 1 < lines.length; i++) {
            pitches.push(regions[lines[i + 1]].rect.top - regions[lines[i]].rect.top);
        }
        const pitch = median(pitches);
        // Lines on one row (pitch ~0) give no step to extend the block with.
        if (pitch <= 0) return null;

        let left = Infinity;
        let right = -Infinity;
        let width = 0;
        const heights: number[] = [];
        for (const index of lines) {
            const line = regions[index];
            left = Math.min(left, line.rect.left);
            right = Math.max(right, line.rect.right);
            width = Math.max(width, regionWidth(line));
            heights.push(regionHeight(line));
        }
        return { left, right, width, lineHeight: median(heights), pitch };
    }

    private isShortLine(line: TextRegion, shape: BlockShape): boolean {
        return regionWidth(line) < BLOCK_ATTACH.shortLineRatio * shape.width;
    }

    // Is `top` where the line after a line starting at `prevTop` would be?
    private isOnPitch(prevTop: number, top: number, shape: BlockShape): boolean {
        const expectedTop = prevTop + shape.pitch;
        return Math.abs(top - expectedTop) <= BLOCK_ATTACH.pitchTolerance * shape.pitch;
    }

    private heightRejectReason(line: TextRegion, shape: BlockShape): string | null {
        const height = regionHeight(line);
        const tooTall = height > BLOCK_ATTACH.maxHeightRatio * shape.lineHeight;
        const tooLow = height < BLOCK_ATTACH.minHeightRatio * shape.lineHeight;
        if (tooTall || tooLow) {
            return `height ${height} vs block ${shape.lineHeight}`;
        }
        return null;
    }

    // Left-aligned text shares the left edge, centered text the center.
    private alignRejectReason(line: TextRegion, shape: BlockShape): string | null {
        const alignLimit = BLOCK_ATTACH.alignTolerance * shape.lineHeight;
        const leftDist = Math.abs(line.rect.left - shape.left);
        const centerDist = Math.abs(regionCenterX(line) - (shape.left + shape.right) / 2);
        if (leftDist > alignLimit && centerDist > alignLimit) {
            return `not aligned: left ${leftDist.toFixed(0)}, center ${centerDist.toFixed(0)} > ${alignLimit.toFixed(0)}`;
        }
        return null;
    }

    // Is there a text line of this block's kind right below `line`: on the
    // block's pitch, aligned with the block and of the same font size?
    // Merely overlapping is not enough: a row of on-screen buttons under the
    // dialogue box sits on the pitch too, and counting it made a paragraph's
    // last line look like a header ("likable." followed by "System").
    private hasLineBelow(line: TextRegion, shape: BlockShape, regions: TextRegion[]): boolean {
        for (const other of regions) {
            if (other === line) continue;
            if (!this.isOnPitch(line.rect.top, other.rect.top, shape)) continue;
            if (this.heightRejectReason(other, shape) !== null) continue;
            if (this.alignRejectReason(other, shape) !== null) continue;
            return true;
        }
        return false;
    }

    // `candidate` starts right on the block's pitch below its last line
    // (the caller checked). Returns why it is not a continuation, or null if
    // it is one.
    private attachRejectReason(
        candidate: number[],
        shape: BlockShape,
        regions: TextRegion[]
    ): string | null {
        const first = regions[candidate[0]];

        for (const index of candidate) {
            const heightReason = this.heightRejectReason(regions[index], shape);
            if (heightReason !== null) return heightReason;
            const width = regionWidth(regions[index]);
            if (width > BLOCK_ATTACH.maxWidthRatio * shape.width) {
                return `width ${width} > block ${shape.width}`;
            }
        }

        const alignReason = this.alignRejectReason(first, shape);
        if (alignReason !== null) return alignReason;

        // A group must keep the same line step as the block.
        if (candidate.length > 1) {
            const candidatePitch = regions[candidate[1]].rect.top - first.rect.top;
            if (Math.abs(candidatePitch - shape.pitch) > BLOCK_ATTACH.pitchTolerance * shape.pitch) {
                return `own pitch ${candidatePitch} vs block ${shape.pitch.toFixed(0)}`;
            }
        }

        // Short line followed by text: a header such as a character name
        // above a dialogue, not the tail of the block above.
        if (this.isShortLine(first, shape)) {
            const followedByText = candidate.length > 1 || this.hasLineBelow(first, shape, regions);
            if (followedByText) {
                return "short line followed by text (header?)";
            }
        }

        return null;
    }

    // Grows Screen AI blocks downward with lone lines and other blocks that
    // continue them, then starts blocks from pairs of lone lines that Screen
    // AI split one per block. Only downward: a character name sits above the
    // dialogue box, so it is never a candidate for that box.
    private attachToBlocks(
        regions: TextRegion[],
        blockGroups: number[][],
        ungroupedIndices: number[]
    ): { regions: TextRegion[]; groups: number[][]; ungroupedIndices: number[] } {
        if (!BLOCK_ATTACH.enabled) {
            return { regions, groups: blockGroups, ungroupedIndices };
        }
        // Other providers give no block ids: nothing to attach and nothing to
        // log, otherwise every screen of every provider would land in the log.
        const hasBlockIds = regions.some(region => region.blockId !== undefined && region.blockId > 0);
        if (!hasBlockIds) {
            return { regions, groups: blockGroups, ungroupedIndices };
        }

        // Copy: debug labels change texts, the caller's regions stay intact.
        const labeled = [...regions];

        const attachLog: string[] = [];
        const note = (message: string): void => {
            attachLog.push(message);
            logger.debug('TextRecognizer', `Block attach: ${message}`);
        };

        // Full layout first: a line that is off the block's pitch never shows
        // up in the decisions below, and only its rect explains why.
        note(`screen: ${regions.length} lines`);
        for (const region of this.sortRegionsTopToBottom(regions)) {
            const r = region.rect;
            note(`  id=${region.blockId} top=${r.top} left=${r.left} w=${r.right - r.left} h=${r.bottom - r.top} "${region.text.slice(0, 30)}"`);
        }

        // Every block and every lone Screen AI line is a unit. Lines without
        // a block id come from other providers and are left alone.
        const units: number[][] = [];
        for (const group of blockGroups) {
            units.push(this.sortTopToBottom(group, regions));
        }
        const blockCount = units.length;
        const otherLone: number[] = [];
        for (const index of ungroupedIndices) {
            const blockId = regions[index].blockId;
            if (blockId !== undefined && blockId > 0) {
                units.push([index]);
            } else {
                otherLone.push(index);
            }
        }
        const consumed = new Set<number>();
        const state: AttachState = { regions, labeled, units, consumed, note };

        // Pass 1: grow the blocks Screen AI gave. Top to bottom, so an upper
        // block takes a lower one before the lower one gets its own turn.
        const blockOrder = Array.from({ length: blockCount }, (_, i) => i);
        blockOrder.sort((a, b) => regions[units[a][0]].rect.top - regions[units[b][0]].rect.top);
        for (const blockUnit of blockOrder) {
            if (consumed.has(blockUnit)) continue;
            this.growBlock(blockUnit, state);
        }

        // Pass 2: lone lines left over may still be a paragraph that Screen AI
        // cut into one block per line. Runs after pass 1 so that a line
        // continuing a real block goes to that block, not to a new pair.
        // Each pair found becomes a block and grows like the ones above.
        const loneOrder: number[] = [];
        for (let unit = blockCount; unit < units.length; unit++) {
            loneOrder.push(unit);
        }
        loneOrder.sort((a, b) => regions[units[a][0]].rect.top - regions[units[b][0]].rect.top);
        for (const upperUnit of loneOrder) {
            if (consumed.has(upperUnit)) continue;
            const seeded = this.seedBlockFromLonePair(upperUnit, state);
            if (seeded) {
                this.growBlock(upperUnit, state);
            }
        }

        const groups: number[][] = [];
        const stillLone: number[] = [...otherLone];
        for (let unit = 0; unit < units.length; unit++) {
            if (consumed.has(unit)) continue;
            if (units[unit].length > 1) {
                groups.push(units[unit]);
            } else {
                stillLone.push(units[unit][0]);
            }
        }
        this.sendAttachLog(attachLog);
        return { regions: labeled, groups, ungroupedIndices: stillLone };
    }

    // Appends lines below the block `blockUnit` while they continue it.
    private growBlock(blockUnit: number, state: AttachState): void {
        const { regions, units, consumed, note } = state;
        const shape = this.describeBlock(units[blockUnit], regions);
        const blockId = regions[units[blockUnit][0]].blockId;
        if (shape === null) {
            note(`block ${blockId}: lines on one row, skipped`);
            return;
        }
        // `right` is logged separately: `w` is the widest line, so
        // left + w is not the block's right edge, and the center check
        // in attachRejectReason uses the real right edge.
        note(`block ${blockId}: ${units[blockUnit].length} lines, pitch=${shape.pitch} lineH=${shape.lineHeight} left=${shape.left} right=${shape.right} w=${shape.width}`);

        while (true) {
            const lines = units[blockUnit];
            const last = regions[lines[lines.length - 1]];
            if (this.isShortLine(last, shape)) {
                // Paragraph already ended with a short line.
                note(`block ${blockId}: ends with a short line, nothing attached after it`);
                return;
            }

            let found: number | null = null;
            for (let unit = 0; unit < units.length; unit++) {
                if (unit === blockUnit || consumed.has(unit)) continue;
                const first = regions[units[unit][0]];
                if (!this.isOnPitch(last.rect.top, first.rect.top, shape)) continue;

                const reason = this.attachRejectReason(units[unit], shape, regions);
                const preview = first.text.slice(0, 30);
                if (reason !== null) {
                    note(`"${preview}" not attached to block ${blockId}: ${reason}`);
                    continue;
                }
                note(`"${preview}" (${units[unit].length} line(s)) attached to block ${blockId}`);
                found = unit;
                break;
            }
            if (found === null) {
                // Rejected candidates are already logged above; this line
                // says where the next line was expected, so the layout
                // dump can be checked without recomputing the pitch.
                const expectedTop = last.rect.top + shape.pitch;
                const tolerance = BLOCK_ATTACH.pitchTolerance * shape.pitch;
                note(`block ${blockId}: nothing more attached, next line expected at top=${expectedTop.toFixed(0)}±${tolerance.toFixed(0)}`);
                return;
            }

            this.mergeUnits(blockUnit, found, state);
        }
    }

    // Appends unit `from` to unit `into` and marks `from` as consumed.
    private mergeUnits(into: number, from: number, state: AttachState): void {
        const { regions, labeled, units, consumed } = state;
        if (BLOCK_ATTACH.debugLabels) {
            const blockId = regions[units[into][0]].blockId;
            for (const index of units[from]) {
                labeled[index] = { ...labeled[index], text: `${labeled[index].text} [+${blockId}]` };
            }
        }
        units[into] = [...units[into], ...units[from]];
        consumed.add(from);
    }

    // Alignment for a pair of lone lines, stricter than alignRejectReason on
    // the left edge (see seedLeftAlignTolerance). Centers are checked with
    // the usual tolerance, so centered text still pairs.
    private pairAlignRejectReason(upper: TextRegion, lower: TextRegion): string | null {
        const upperHeight = regionHeight(upper);
        const leftLimit = BLOCK_ATTACH.seedLeftAlignTolerance * upperHeight;
        const centerLimit = BLOCK_ATTACH.alignTolerance * upperHeight;
        const leftDist = Math.abs(lower.rect.left - upper.rect.left);
        const centerDist = Math.abs(regionCenterX(lower) - regionCenterX(upper));
        if (leftDist > leftLimit && centerDist > centerLimit) {
            return `pair not aligned: left ${leftDist.toFixed(0)} > ${leftLimit.toFixed(0)}, center ${centerDist.toFixed(0)} > ${centerLimit.toFixed(0)}`;
        }
        return null;
    }

    // Tries to start a block from the lone line `upperUnit` and the lone line
    // right below it. Returns true if the pair was merged into `upperUnit`.
    private seedBlockFromLonePair(upperUnit: number, state: AttachState): boolean {
        const { regions, units, consumed, note } = state;
        const upper = regions[units[upperUnit][0]];
        const upperHeight = regionHeight(upper);
        if (upperHeight <= 0) return false;
        // Short lines (names, menu items, buttons) never start a block; not
        // logged, every button on the screen would show up otherwise.
        if (regionWidth(upper) < BLOCK_ATTACH.seedMinWidthInHeights * upperHeight) return false;

        // Candidates in the allowed step range, nearest first: the line right
        // below is the one that may continue the upper line.
        const candidates: number[] = [];
        for (let unit = 0; unit < units.length; unit++) {
            if (unit === upperUnit || consumed.has(unit) || units[unit].length !== 1) continue;
            const step = regions[units[unit][0]].rect.top - upper.rect.top;
            const stepRatio = step / upperHeight;
            if (stepRatio < BLOCK_ATTACH.seedMinPitchRatio) continue;
            if (stepRatio > BLOCK_ATTACH.seedMaxPitchRatio) continue;
            candidates.push(unit);
        }
        candidates.sort((a, b) => regions[units[a][0]].rect.top - regions[units[b][0]].rect.top);

        const upperPreview = upper.text.slice(0, 30);
        for (const unit of candidates) {
            const lower = regions[units[unit][0]];
            // A one-line "block": the step is the one being tried, so the
            // usual continuation checks (font size, width, alignment, header
            // rule) can be reused as is.
            const pairShape: BlockShape = {
                left: upper.rect.left,
                right: upper.rect.right,
                width: regionWidth(upper),
                lineHeight: upperHeight,
                pitch: lower.rect.top - upper.rect.top,
            };
            const reason =
                this.attachRejectReason(units[unit], pairShape, regions) ??
                this.pairAlignRejectReason(upper, lower);
            const lowerPreview = lower.text.slice(0, 30);
            if (reason !== null) {
                note(`"${lowerPreview}" not paired with "${upperPreview}": ${reason}`);
                continue;
            }
            note(`"${upperPreview}" + "${lowerPreview}" start a block (step ${pairShape.pitch}, upper h=${upperHeight})`);
            this.mergeUnits(upperUnit, unit, state);
            return true;
        }
        return false;
    }

    private sortRegionsTopToBottom(regions: TextRegion[]): TextRegion[] {
        return [...regions].sort((a, b) => a.rect.top - b.rect.top);
    }

    // One call per screen, not awaited: logging must not slow down or break
    // recognition, so a failed call is only noted in the console.
    private sendAttachLog(lines: string[]): void {
        if (!BLOCK_ATTACH.debugLogToBackend || lines.length === 0) return;
        call<[string[]], void>('log_block_attach', lines).catch((error) => {
            logger.warn('TextRecognizer', `Block attach: failed to write plugin log: ${error}`);
        });
    }

    applyAutoGlue(regions: TextRegion[]): TextRegion[] {
        if (!regions || regions.length <= 1) return regions;

        logger.debug('TextRecognizer', `Auto-glue: Processing ${regions.length} regions`);

        // Used for reading order of the final paragraphs.
        const medianH = computeMedianHeight(regions);

        // Lines with a shared Screen AI block id are merged as is. Everything
        // else (all regions for providers without block ids) goes through
        // geometry. Without block ids `ungrouped` is `regions` in the same
        // order, so other providers behave exactly as before.
        const byId = this.groupByBlockId(regions);
        const attached = this.attachToBlocks(regions, byId.groups, byId.ungroupedIndices);
        const mergedById = this.extractMergedRegions(attached.groups, attached.regions, "paragraph");
        const ungrouped = attached.ungroupedIndices.map(index => attached.regions[index]);

        let geoParagraphs = ungrouped;
        if (ungrouped.length > 1) {
            // Median of the lines geometry actually groups: dialogue lines
            // already merged by block id may use another font size.
            const geoMedianH = computeMedianHeight(ungrouped);
            logger.debug('TextRecognizer', `Auto-glue: Median line height = ${geoMedianH}px, grouping power = ${this.groupingPower}`);

            // Phase 1: assemble word-level boxes into lines
            const lines = this.assembleLines(ungrouped, geoMedianH);
            logger.debug('TextRecognizer', `Auto-glue: Phase 1 produced ${lines.length} lines from ${ungrouped.length} boxes`);

            // Phase 2: assemble lines into paragraphs
            geoParagraphs = this.assembleParagraphs(lines, geoMedianH);
            logger.debug('TextRecognizer', `Auto-glue: Phase 2 produced ${geoParagraphs.length} paragraphs from ${lines.length} lines`);
        }
        const paragraphs = [...mergedById, ...geoParagraphs];

        // Sort into reading order: top-to-bottom, left-to-right with tolerance
        paragraphs.sort((a, b) => {
            const yCenterA = (a.rect.top + a.rect.bottom) / 2;
            const yCenterB = (b.rect.top + b.rect.bottom) / 2;
            if (Math.abs(yCenterA - yCenterB) < 0.3 * medianH) {
                return a.rect.left - b.rect.left;
            }
            return yCenterA - yCenterB;
        });

        // Post-merge: dialog detection on complete blocks
        const annotated = paragraphs.map(p => ({
            ...p,
            isDialog: this.detectDialog(p),
        }));

        // Final pass: resolve any overlapping rects caused by merge union rects
        const resolved = this.resolveOverlaps(annotated);
        if (resolved.length !== annotated.length) {
            logger.debug('TextRecognizer', `Auto-glue: Overlap resolution reduced ${annotated.length} to ${resolved.length} regions`);
        }
        return resolved;
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

                const mergedRegions = this.applyAutoGlue(regions);
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

                const mergedRegions = this.applyAutoGlue(regions);
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
