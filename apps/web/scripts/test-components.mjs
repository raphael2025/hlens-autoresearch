// Component test runner (`npm run test:components`; apps/web README "组件测试"), with no new
// dependency: Node cannot strip JSX, so esbuild (already installed as vite's own dependency) bundles
// every src/**/*.test.tsx into an ES module, and `node --test` runs the bundles. The tests render
// pages and components with react-dom/server's renderToStaticMarkup (no DOM needed).
//
// Output goes to node_modules/.component-tests/ (gitignored, wiped on every run). That directory is
// exactly two levels below apps/web, like src/lib/, so src/lib/fixtures.test-util.ts' `new
// URL("../../fixtures/", import.meta.url)` still resolves to apps/web/fixtures/ from the bundles;
// entries and shared chunks are therefore written flat (no sub-directories).
//
// Extra arguments are passed to `node --test` (e.g. `-- --test-name-pattern=Jobs`).
import { spawnSync } from "node:child_process";
import { readdirSync, rmSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = join(webRoot, "src");
const outDir = join(webRoot, "node_modules", ".component-tests");

const entryPoints = readdirSync(srcDir, { recursive: true })
  .map(String)
  .filter((name) => name.endsWith(".test.tsx"))
  .sort()
  .map((name) => join(srcDir, name));

if (entryPoints.length === 0) {
  console.error("test-components: no src/**/*.test.tsx files found");
  process.exit(1);
}

const names = entryPoints.map((file) => file.slice(file.lastIndexOf("/") + 1));
const duplicate = names.find((name, index) => names.indexOf(name) !== index);
if (duplicate !== undefined) {
  // entries are written flat (see above), so two test files may not share a basename
  console.error(`test-components: duplicate test file name ${duplicate}`);
  process.exit(1);
}

rmSync(outDir, { recursive: true, force: true });

await build({
  entryPoints,
  outdir: outDir,
  entryNames: "[name]",
  chunkNames: "chunk-[hash]",
  outExtension: { ".js": ".mjs" },
  bundle: true,
  splitting: true,
  platform: "node",
  format: "esm",
  target: "node20",
  jsx: "automatic",
  sourcemap: "inline",
  logLevel: "warning",
  // CommonJS packages bundled into ES modules (react-dom/server) require Node built-ins at run time.
  banner: {
    js: 'import { createRequire as __hlensCreateRequire } from "node:module"; const require = __hlensCreateRequire(import.meta.url);',
  },
});

const bundles = readdirSync(outDir)
  .filter((name) => name.endsWith(".test.mjs"))
  .sort()
  .map((name) => join(outDir, name));

console.log(
  `test-components: bundled ${bundles.length} test file(s) from ${entryPoints
    .map((file) => relative(webRoot, file))
    .join(", ")}`,
);

const result = spawnSync(process.execPath, ["--test", ...process.argv.slice(2), ...bundles], {
  stdio: "inherit",
  cwd: webRoot,
});
process.exit(result.status ?? 1);
