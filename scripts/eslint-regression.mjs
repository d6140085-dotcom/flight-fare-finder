// ESLint regression check (Phase 3B-D CI). The repository has a known baseline of existing
// findings (scripts/eslint-baseline.json). This check fails on any NEW finding, even when
// unrelated old findings disappear at the same time: findings are compared one by one, never
// by total count.
//
// A finding's fingerprint is file + rule + severity + the trimmed source line it points at
// (+ the message), so it survives unrelated edits that shift line numbers, but a new
// violation, or another copy of an existing one, is new. Duplicates are counted.
//
//   node scripts/eslint-regression.mjs            # check against the baseline (CI)
//   node scripts/eslint-regression.mjs --update   # rewrite the baseline (reviewed in a PR)
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { relative, resolve } from "node:path";
import { ESLint } from "eslint";

const ROOT = resolve(import.meta.dirname, "..");
const BASELINE = resolve(ROOT, "scripts/eslint-baseline.json");

function fingerprint(file, source, m) {
  const line = m.line ? (source.split("\n")[m.line - 1] ?? "").trim() : "";
  const id = [relative(ROOT, file), m.ruleId ?? "fatal", m.severity, line, m.message].join(
    "\u0000",
  );
  return createHash("sha256").update(id).digest("hex").slice(0, 24);
}

const eslint = new ESLint({ cwd: ROOT });
const results = await eslint.lintFiles(["."]);
const current = new Map();
const describe = new Map();
let errors = 0;
let warnings = 0;
for (const r of results) {
  if (!r.messages.length) continue;
  const source = r.source ?? readFileSync(r.filePath, "utf8");
  for (const m of r.messages) {
    if (m.severity === 2) errors++;
    else warnings++;
    const fp = fingerprint(r.filePath, source, m);
    current.set(fp, (current.get(fp) ?? 0) + 1);
    describe.set(
      fp,
      `${relative(ROOT, r.filePath)}:${m.line ?? 0}:${m.column ?? 0} ${m.severity === 2 ? "error" : "warning"} ${m.ruleId ?? "fatal"} — ${m.message}`,
    );
  }
}

if (process.argv.includes("--update")) {
  const fingerprints = Object.fromEntries([...current.entries()].sort());
  writeFileSync(BASELINE, JSON.stringify({ errors, warnings, fingerprints }, null, 2) + "\n");
  console.log(
    `baseline written: ${errors} errors, ${warnings} warnings, ${current.size} fingerprints`,
  );
  process.exit(0);
}

const baseline = JSON.parse(readFileSync(BASELINE, "utf8"));
const added = [];
for (const [fp, n] of current) {
  const allowed = baseline.fingerprints[fp] ?? 0;
  for (let i = allowed; i < n; i++) added.push(describe.get(fp));
}
let removed = 0;
for (const [fp, n] of Object.entries(baseline.fingerprints))
  removed += Math.max(0, n - (current.get(fp) ?? 0));

console.log(
  `ESLint: ${errors} errors, ${warnings} warnings (baseline ${baseline.errors} errors, ${baseline.warnings} warnings)`,
);
console.log(`New findings: ${added.length}; baseline findings no longer reported: ${removed}`);
if (added.length) {
  console.error("New ESLint findings (not in scripts/eslint-baseline.json):");
  for (const d of added) console.error("  " + d);
  process.exit(1);
}
