import { logger } from "../Logger";
import type { TextRegion } from "../TextRecognizer";

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

// Screen AI block ids: grouping lines by id and the attach pass that glues
// back lines Screen AI split off a block.
export class BlockAttacher {
    // Receives the debug log (screen layout and attach decisions) when
    // BLOCK_ATTACH.debugLogToBackend is on. Passed in so that glue code does
    // not call the backend itself: the caller decides where the log goes.
    constructor(private readonly sendLog: (lines: string[]) => void) {}

    // Screen AI reports which visual block a line belongs to; group by that id
    // before any geometry so large line spacing cannot split a dialogue block.
    // Returns indices, not regions: the attach pass needs to know which lines
    // are still alone.
    groupByBlockId(regions: TextRegion[]): {
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
    attachToBlocks(
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

    private sendAttachLog(lines: string[]): void {
        if (!BLOCK_ATTACH.debugLogToBackend || lines.length === 0) return;
        this.sendLog(lines);
    }
}
