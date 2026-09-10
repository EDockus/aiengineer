/*
 * Runs the runtime checks in dev/checks against the real src/ modules.
 *
 * esbuild bundles the checks together with src/hub.ts and src/ado-host.ts for
 * Node, aliasing the two browser-only packages to local stubs. The code under
 * test is the shipped code: nothing in src/ knows this runner exists.
 */
import { build } from "esbuild";
import { mkdir, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
/*
 * VERIFY_SRC points the checks at a different copy of src/ — used once to run
 * the same checks against the pre-fix code from git and confirm they fail there.
 * Unset (the normal case) means the working tree.
 */
const srcRoot = process.env.VERIFY_SRC ? resolve(process.env.VERIFY_SRC) : root;
const outdir = resolve(root, "dev/dist-checks");
const outfile = resolve(outdir, "checks.mjs");

await rm(outdir, { recursive: true, force: true });
await mkdir(outdir, { recursive: true });

await build({
  entryPoints: [resolve(root, "dev/checks/main.mjs")],
  outfile,
  bundle: true,
  format: "esm",
  platform: "node",
  target: ["node20"],
  sourcemap: "inline",
  logLevel: "warning",
  plugins: [
    {
      name: "src-root",
      setup(build) {
        if (srcRoot === root) return;
        build.onResolve({ filter: /^\.\.\/\.\.\/src\// }, (args) => ({
          path: resolve(srcRoot, args.path.replace("../../", "")),
        }));
      },
    },
  ],
  alias: {
    "azure-devops-extension-sdk": resolve(root, "dev/checks/sdk-stub.mjs"),
    "@azure/msal-browser": resolve(root, "dev/checks/msal-stub.mjs"),
  },
});

process.env.HUB_HTML = resolve(srcRoot, "src/hub.html");
await import(pathToFileURL(outfile).href);
