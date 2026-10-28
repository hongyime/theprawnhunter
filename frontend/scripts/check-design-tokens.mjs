import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

// Design contract: 3 color families (ink/slate, paper/white, teal) with red
// reserved for errors, 2 fonts (Space Grotesk + mono), 4 sizes (12/14/18/24).
const ROOTS = ["app", "components"];
const ALLOWED_SIZES = new Set(["text-xs", "text-sm", "text-lg", "text-2xl"]);
const ALLOWED_FAMILIES = new Set(["slate", "red"]);
const ALLOWED_HEX = new Set([
  "#172033",
  "#197e74",
  "#12645c",
  "#f4f7fa",
  "#475569",
  "#64748b",
  "#cbd5e1",
  "#dc2626",
  "#ffffff",
]);

const violations = [];

function check(file) {
  const lines = readFileSync(file, "utf8").split(/\r?\n/);
  lines.forEach((line, index) => {
    const loc = `${relative(process.cwd(), file)}:${index + 1}`;
    for (const match of line.matchAll(
      /\btext-(xs|sm|base|lg|xl|2xl|3xl|4xl|5xl|6xl|7xl|\[\d+px\])\b/g,
    )) {
      if (!ALLOWED_SIZES.has(match[0])) {
        violations.push(`${loc} font size ${match[0]}`);
      }
    }
    for (const match of line.matchAll(/\bfont-(black|\[[^\]]+\]|serif|sans)\b/g)) {
      violations.push(`${loc} font family/weight ${match[0]}`);
    }
    for (const match of line.matchAll(
      /\b(?:bg|text|border|ring|outline|from|to|via)-([a-z]+)-[0-9]{2,3}\b/g,
    )) {
      if (!ALLOWED_FAMILIES.has(match[1])) {
        violations.push(`${loc} color ${match[0]}`);
      }
    }
    for (const match of line.matchAll(/#[0-9a-fA-F]{6}\b/g)) {
      if (!ALLOWED_HEX.has(match[0].toLowerCase())) {
        violations.push(`${loc} hex ${match[0]}`);
      }
    }
  });
}

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      walk(path);
    } else if (path.endsWith(".tsx") && !path.includes(".test.")) {
      check(path);
    }
  }
}

ROOTS.forEach(walk);

if (violations.length > 0) {
  console.error("Design token violations:\n" + violations.join("\n"));
  process.exit(1);
}

console.log(
  "Design tokens OK: 3 color families (+error red), 2 fonts, 4 sizes.",
);
