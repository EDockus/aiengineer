import { build } from "esbuild";
import { existsSync } from "node:fs";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const outdir = resolve(root, "dist");
const dev = process.argv.includes("--dev");

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

await build({
  entryPoints: [resolve(root, "src/main.ts")],
  outfile: resolve(outdir, "hub.js"),
  bundle: true,
  format: "iife",
  target: ["es2022", "chrome110", "firefox115", "safari16", "edge110"],
  platform: "browser",
  minify: !dev,
  sourcemap: dev ? "inline" : false,
  legalComments: "none",
  logLevel: "info",
});

await cp(resolve(root, "src/hub.html"), resolve(outdir, "hub.html"));
await cp(resolve(root, "src/styles.css"), resolve(outdir, "styles.css"));
/*
 * Administrator-pinned settings, read at runtime by ado-host.ts.
 *
 * `src/agent-config.json` is a tracked template and is deliberately empty — one
 * tenant's agent URL and app registration must not ship in a public repository.
 * The deployer's real values live in the untracked `deploy.local.json` written
 * by `npm run configure`, and are merged in here. With no deploy.local.json the
 * package ships unpinned and every user configures their own agent in the hub.
 */
const GENERATED_COMMENT =
  "Generated at package time from extension/deploy.local.json by `npm run configure`. Do not edit by hand and do not commit a copy with real values in it - the tracked template at extension/src/agent-config.json is deliberately empty. Changing these settings requires repackaging and reinstalling the extension, which needs the organization's Manage extensions permission; that permission is the access control on these values.";
const deployFile = resolve(root, "deploy.local.json");
if (existsSync(deployFile)) {
  const deploy = JSON.parse(await readFile(deployFile, "utf8"));
  const agentConfig = deploy.agentConfig ?? { default: {}, projects: {} };
  await writeFile(
    resolve(outdir, "agent-config.json"),
    `${JSON.stringify({ _comment: GENERATED_COMMENT, ...agentConfig }, null, 2)}\n`,
  );
  console.log(`pinned agent settings from deploy.local.json -> dist/agent-config.json`);
} else {
  await cp(resolve(root, "src/agent-config.json"), resolve(outdir, "agent-config.json"));
}

console.log(`built ${dev ? "dev" : "production"} bundle -> dist/`);
