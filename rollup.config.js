import { readFileSync } from "fs";
import deckyPlugin from "@decky/rollup";

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8"));

// CI passes the version being released: it rewrites package.json only after the
// frontend is built, so reading package.json here would bake the previous one.
const version = (process.env.PLUGIN_VERSION || pkg.version || "dev").trim() || "dev";

// Debug log of block attach decisions (src/glue/blockAttach.ts). It writes screen
// text into the plugin log, so it is on only when asked for explicitly:
// build-plugin-zip.sh sets the variable for local builds, CI and a plain
// `pnpm build` do not, and releases come out without the log.
const debugBlockAttachLog = process.env.DEBUG_BLOCK_ATTACH_LOG === "1";

const MONTHS = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];
const now = new Date();
const buildDate = `${now.getDate()} ${MONTHS[now.getMonth()]} ${now.getFullYear()}`;

// Replaces the __PLUGIN_*__ placeholders (declared in src/tabs/VersionBadge.tsx)
// with string literals and __DEBUG_BLOCK_ATTACH_LOG__ (src/glue/blockAttach.ts)
// with a boolean; runs before the TypeScript plugin transpiles the file.
const buildInfo = {
  name: "plugin-build-info",
  transform(code) {
    const hasPlaceholder =
      code.includes("__PLUGIN_VERSION__") ||
      code.includes("__PLUGIN_BUILD_DATE__") ||
      code.includes("__DEBUG_BLOCK_ATTACH_LOG__");
    if (!hasPlaceholder) {
      return null;
    }
    return {
      code: code
        .replace(/__PLUGIN_VERSION__/g, JSON.stringify(version))
        .replace(/__PLUGIN_BUILD_DATE__/g, JSON.stringify(buildDate))
        .replace(/__DEBUG_BLOCK_ATTACH_LOG__/g, JSON.stringify(debugBlockAttachLog)),
      map: null,
    };
  },
};

export default deckyPlugin({
  plugins: [buildInfo],
});
