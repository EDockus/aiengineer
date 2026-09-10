/*
 * Local dev harness server.
 *
 * Builds dev/main.ts (which imports the real src/hub.ts) and serves:
 *   /            the harness wrapper page with the controls
 *   /hub.html    the real src/hub.html, with its script tag pointed at the dev bundle
 *   /styles.css  the real src/styles.css
 *
 * hub.html is generated from src/hub.html on every build rather than copied by
 * hand, so there is no second copy of the markup to keep in sync.
 */
import { context } from "esbuild";
import { mkdir, readFile, rm, writeFile, copyFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outdir = resolve(root, "dev/dist");
const port = Number(process.env.PORT ?? 5173);

async function stageStaticFiles() {
  const html = await readFile(resolve(root, "src/hub.html"), "utf8");
  const devHtml = html.replace('src="hub.js"', 'src="main.js"');
  if (devHtml === html) {
    throw new Error('Expected src/hub.html to load "hub.js"; the dev harness rewrite found nothing.');
  }
  await writeFile(resolve(outdir, "hub.html"), devHtml);
  await copyFile(resolve(root, "src/styles.css"), resolve(outdir, "styles.css"));
  await copyFile(resolve(root, "dev/index.html"), resolve(outdir, "index.html"));
}

const stagePlugin = {
  name: "stage-static",
  setup(build) {
    build.onEnd(async (result) => {
      if (result.errors.length === 0) await stageStaticFiles();
    });
  },
};

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

const ctx = await context({
  entryPoints: [resolve(root, "dev/main.ts")],
  outfile: resolve(outdir, "main.js"),
  bundle: true,
  format: "iife",
  target: ["es2022"],
  platform: "browser",
  sourcemap: "inline",
  logLevel: "info",
  plugins: [stagePlugin],
});

await ctx.watch();

let server;
try {
  server = await ctx.serve({ servedir: outdir, port, host: "127.0.0.1" });
} catch (error) {
  if (String(error?.message ?? "").includes("address already in use")) {
    console.error(`\n  Port ${port} is already in use.`);
    console.error(`  Stop whatever is on it, or pick another:  PORT=5174 npm run dev\n`);
    process.exit(1);
  }
  throw error;
}

console.log("");
console.log("  ticket-to-pr dev harness");
console.log(`  →  http://${server.hosts[0] ?? "127.0.0.1"}:${server.port}/`);
console.log("");
console.log("  Rebuilds on change. Ctrl-C to stop.");
