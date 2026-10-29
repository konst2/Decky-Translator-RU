// src/tabs/VersionBadge.tsx - Version and build date at the bottom of the main tab

import { VFC } from "react";

// Placeholders replaced at build time by rollup.config.js: the version comes
// from package.json (or the release version CI passes in), the date is the
// build date of the installed artifact.
declare const __PLUGIN_VERSION__: string;
declare const __PLUGIN_BUILD_DATE__: string;

export const VersionBadge: VFC = () => (
    <div style={{ display: "flex", justifyContent: "center", marginTop: "10px" }}>
        <span
            title="Версия и дата сборки"
            style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                padding: "2px 9px",
                fontSize: "10px",
                letterSpacing: "0.2px",
                color: "#7c848d",
                backgroundColor: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.07)",
                borderRadius: "10px",
                whiteSpace: "nowrap",
            }}
        >
            <span style={{ color: "#9aa2ab", fontWeight: "bold" }}>v{__PLUGIN_VERSION__}</span>
            <span style={{ opacity: 0.4 }}>·</span>
            <span>{__PLUGIN_BUILD_DATE__}</span>
        </span>
    </div>
);
