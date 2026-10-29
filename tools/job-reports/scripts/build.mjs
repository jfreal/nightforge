// Copies the Routines page from tools/pr-dashboard/static into public/, so the hosted page and the
// local one are the same files. Wrangler runs this before every `dev` and `deploy` ([build] in
// wrangler.toml). public/ is gitignored: the files live in one place only.
import { copyFileSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(dirname(fileURLToPath(import.meta.url)));
const statics = join(here, "..", "pr-dashboard", "static");
const out = join(here, "public");
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
const files = [
  [join(statics, "routines.html"), "index.html"],
  [join(statics, "routines.js"), "routines.js"],
  [join(statics, "routines.css"), "routines.css"],
  [join(statics, "style.css"), "style.css"],
  [join(here, "..", "..", "docs", "logo.png"), "logo.png"],
];
for (const [from, to] of files) copyFileSync(from, join(out, to));
console.log(`copied ${files.length} files into public/`);
