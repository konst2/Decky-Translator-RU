import { readFileSync } from "fs";
import deckyPlugin from "@decky/rollup";

const pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf-8"));

// CI passes the version being released: it rewrites package.json only after the
// frontend is built, so reading package.json here would bake the previous one.
const version = (process.env.PLUGIN_VERSION || pkg.version || "dev").trim() || "dev";

const MONTHS = [
  "января", "февраля", "марта", "апреля", "мая", "июня",
  "июля", "августа", "сентября", "октября", "ноября", "декабря",
];
const now = new Date();
const buildDate = `${now.getDate()} ${MONTHS[now.getMonth()]} ${now.getFullYear()}`;

// Replaces the __PLUGIN_*__ placeholders (declared in src/tabs/VersionBadge.tsx)
// with string literals; runs before the TypeScript plugin transpiles the file.
const buildInfo = {
  name: "plugin-build-info",
  transform(code) {
    if (!code.includes("__PLUGIN_VERSION__") && !code.includes("__PLUGIN_BUILD_DATE__")) {
      return null;
    }
    return {
      code: code
        .replace(/__PLUGIN_VERSION__/g, JSON.stringify(version))
        .replace(/__PLUGIN_BUILD_DATE__/g, JSON.stringify(buildDate)),
      map: null,
    };
  },
};

export default deckyPlugin({
  plugins: [buildInfo],
});
