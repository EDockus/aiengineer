#!/usr/bin/env node
/*
 * Writes the deployer-specific values that must never be committed.
 *
 * This repository is a template. Three things in it are tenant-specific — the
 * Marketplace publisher id, the agent's Foundry endpoint, and the Entra app
 * registration — and every one of them used to be hand-edited into a tracked
 * file. That is how one tenant's identifiers end up in a public repo. So the
 * tracked files carry placeholders only, and this script writes the real values
 * into two untracked, gitignored artifacts:
 *
 *   extension/deploy.local.json      the resolved values (source of truth)
 *   extension/vss-extension.local.json  the manifest tfx actually packages
 *
 * `npm run build` reads deploy.local.json and emits dist/agent-config.json from
 * it; `dist/` is gitignored too. After a full `npm run package`, `git status`
 * is still clean.
 *
 * Inputs are resolved per value, in this order, first hit wins:
 *
 *   1. a command-line flag
 *   2. an environment variable
 *   3. `azd env get-values` in the repository root (skip with --no-azd)
 *
 * Nothing is guessed. A value that cannot be resolved, or that the extension's
 * own validators would reject at runtime, is a hard error — this script never
 * writes a half-configured package that fails silently three steps later in an
 * Azure DevOps organization.
 *
 * Usage
 *   node scripts/configure.mjs --publisher <id> [--client-id <guid>] [--tenant-id <guid>]
 *                              [--agent-url <url>] [--repo-url <url>] [--version <x.y.z>]
 *                              [--project <ado-project>=<agent-url>]...
 *                              [--azd-env <name>] [--no-azd] [--print]
 *   node scripts/configure.mjs --verify     re-validate what is already there (used by `npm run package`)
 *
 * Environment variables
 *   T2PR_EXT_PUBLISHER      Marketplace publisher id
 *   T2PR_ENTRA_CLIENT_ID    Entra app registration (client) id of the SPA registration
 *   T2PR_ENTRA_TENANT_ID    Entra directory (tenant) id
 *   T2PR_AGENT_URL          Agent Application URL (the /invocations suffix is stripped for you)
 *   T2PR_EXT_REPO_URL       repository URL for the Marketplace Repository/Support links
 */
import { build } from "esbuild";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(root, "..");

const DEPLOY_FILE = resolve(root, "deploy.local.json");
const MANIFEST_SRC = resolve(root, "vss-extension.json");
const MANIFEST_OUT = resolve(root, "vss-extension.local.json");
const GENERATED_COMMENT =
  "Generated at package time from extension/deploy.local.json by `npm run configure`. Do not edit by hand and do not commit a copy with real values in it - the tracked template at extension/src/agent-config.json is deliberately empty. Changing these settings requires repackaging and reinstalling the extension, which needs the organization's Manage extensions permission; that permission is the access control on these values.";

/** Publisher ids on the Visual Studio Marketplace: lowercase alphanumerics and hyphens. */
const PUBLISHER = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/;

class ConfigError extends Error {}

// ---------------------------------------------------------------- arguments

function parseArgs(argv) {
  const flags = { projects: {} };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const take = () => {
      const value = argv[i + 1];
      if (value === undefined || value.startsWith("--")) {
        throw new ConfigError(`${arg} needs a value.`);
      }
      i += 1;
      return value;
    };
    switch (arg) {
      case "--publisher": flags.publisher = take(); break;
      case "--client-id": flags.entraClientId = take(); break;
      case "--tenant-id": flags.entraTenantId = take(); break;
      case "--agent-url": flags.agentUrl = take(); break;
      case "--repo-url": flags.repositoryUrl = take(); break;
      case "--version": flags.version = take(); break;
      case "--azd-env": flags.azdEnv = take(); break;
      case "--project": {
        const pair = take();
        const eq = pair.indexOf("=");
        if (eq < 1) throw new ConfigError(`--project expects <ado-project>=<agent-url>, got "${pair}".`);
        flags.projects[pair.slice(0, eq)] = pair.slice(eq + 1);
        break;
      }
      case "--no-azd": flags.noAzd = true; break;
      case "--verify": flags.verify = true; break;
      case "--print": flags.print = true; break;
      case "-h":
      case "--help": flags.help = true; break;
      default:
        throw new ConfigError(`Unknown argument "${arg}". Run with --help.`);
    }
  }
  return flags;
}

function usage() {
  return [
    "Usage:",
    "  node scripts/configure.mjs --publisher <id> [options]",
    "  node scripts/configure.mjs --verify",
    "",
    "Options (each also readable from an environment variable, and from `azd env get-values`):",
    "  --publisher <id>              Marketplace publisher id          T2PR_EXT_PUBLISHER",
    "  --client-id <guid>            Entra SPA app registration id     T2PR_ENTRA_CLIENT_ID",
    "  --tenant-id <guid>            Entra directory (tenant) id       T2PR_ENTRA_TENANT_ID",
    "  --agent-url <url>             Agent Application URL             T2PR_AGENT_URL",
    "                                (defaults to AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT from azd,",
    "                                 with the /invocations suffix and any query string removed)",
    "  --repo-url <url>              Marketplace Repository/Support    T2PR_EXT_REPO_URL",
    "  --project <name>=<agent-url>  per-Azure-DevOps-project override, repeatable",
    "  --version <x.y.z>             override the packaged extension version",
    "  --azd-env <name>              read a named azd environment instead of the current one",
    "  --no-azd                      do not shell out to azd at all",
    "  --print                       print what is currently configured and exit",
    "  --verify                      re-validate deploy.local.json and regenerate the manifest",
    "",
    "Leave --client-id, --tenant-id and --agent-url unset to package an extension with no",
    "pinned settings: every user then configures their own agent in the hub's settings form.",
    "--publisher is always required — tfx names the .vsix after it and the Marketplace",
    "refuses a publisher id you do not own.",
  ].join("\n");
}

// ------------------------------------------------------------------ sources

function azdValues(envName) {
  const args = ["env", "get-values"];
  if (envName) args.push("-e", envName);
  let out;
  try {
    out = execFileSync("azd", args, { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch {
    // No azd on PATH, or no environment selected. Not an error: flags and
    // environment variables are a complete input path on their own.
    return {};
  }
  const values = {};
  for (const line of out.split("\n")) {
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (value.startsWith('"') && value.endsWith('"') && value.length >= 2) {
      value = value.slice(1, -1);
    }
    values[key] = value;
  }
  return values;
}

/**
 * `AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT` is the full POST target, e.g.
 * `…/endpoint/protocols/invocations?api-version=v1`. What the page wants is the
 * base: it appends `/invocations`, `/invocations/{runId}` and `?api-version=v1`
 * itself, and refuses a URL carrying a query string precisely so that pasting
 * the raw azd value is a visible mistake rather than a `…/invocations/invocations`
 * 404 later. Strip both here so the deployer never has to.
 */
function agentBaseUrl(value) {
  if (!value) return "";
  let url;
  try {
    url = new URL(value.trim());
  } catch {
    return value.trim();
  }
  url.search = "";
  url.hash = "";
  url.pathname = url.pathname.replace(/\/+$/, "").replace(/\/invocations$/, "");
  return url.toString().replace(/\/+$/, "");
}

function resolveInputs(flags) {
  const env = process.env;
  const azd = flags.noAzd ? {} : azdValues(flags.azdEnv);

  /*
   * Flag, then this process's environment, then the azd environment. The same
   * `T2PR_*` name usually appears twice — once as the process env var a human
   * exports, once as an azd key, because `scripts/setup.sh` records these with
   * `azd env set` so a later `configure` needs no arguments at all.
   */
  const pick = (flagValue, envKey, ...azdKeys) => {
    if (flagValue) return String(flagValue).trim();
    if (env[envKey]) return String(env[envKey]).trim();
    for (const key of azdKeys) if (azd[key]) return String(azd[key]).trim();
    return "";
  };

  const agentUrl = agentBaseUrl(
    pick(
      flags.agentUrl,
      "T2PR_AGENT_URL",
      "T2PR_AGENT_URL",
      "AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT",
    ),
  );

  return {
    publisher: pick(flags.publisher, "T2PR_EXT_PUBLISHER", "T2PR_EXT_PUBLISHER"),
    repositoryUrl: pick(flags.repositoryUrl, "T2PR_EXT_REPO_URL", "T2PR_EXT_REPO_URL"),
    version: flags.version ? String(flags.version).trim() : "",
    agentConfig: {
      default: {
        agentUrl,
        entraClientId: pick(flags.entraClientId, "T2PR_ENTRA_CLIENT_ID", "T2PR_ENTRA_CLIENT_ID"),
        agentScope: "", // always overwritten with FOUNDRY_AGENT_SCOPE in validate()
        entraTenantId: pick(
          flags.entraTenantId,
          "T2PR_ENTRA_TENANT_ID",
          "T2PR_ENTRA_TENANT_ID",
          "AZURE_TENANT_ID",
        ),
      },
      projects: Object.fromEntries(
        Object.entries(flags.projects).map(([name, url]) => [name, { agentUrl: agentBaseUrl(url) }]),
      ),
    },
  };
}

// --------------------------------------------------------------- validation

/**
 * Validate with the extension's own code, not a copy of its rules.
 *
 * `src/host.ts` is what the page runs; bundling it here means this script and
 * the settings form can never disagree about what a valid agent URL, tenant id
 * or scope is. It imports only types, so it bundles for Node with no stubs.
 */
async function loadValidators() {
  if (!existsSync(resolve(root, "node_modules/esbuild"))) {
    throw new ConfigError(
      "extension/node_modules is missing. Run `npm ci` in extension/ before configuring.",
    );
  }
  const dir = await mkdtemp(join(tmpdir(), "t2pr-configure-"));
  const outfile = join(dir, "host.mjs");
  try {
    await build({
      entryPoints: [resolve(root, "src/host.ts")],
      outfile,
      bundle: true,
      format: "esm",
      platform: "node",
      target: ["node20"],
      logLevel: "silent",
    });
    return { module: await import(pathToFileURL(outfile).href), dir };
  } catch (error) {
    await rm(dir, { recursive: true, force: true });
    throw error;
  }
}

function validate(config, host) {
  const { validateSettings, FOUNDRY_AGENT_SCOPE } = host;
  const base = config.agentConfig.default;
  // Not a choice: one literal, checked by validateAgentScope. See src/host.ts.
  base.agentScope = FOUNDRY_AGENT_SCOPE;
  const pinned = Boolean(base.agentUrl || base.entraClientId || base.entraTenantId);

  if (!config.publisher) {
    throw new ConfigError(
      "No Marketplace publisher id.\n" +
        "  Pass --publisher <id>, or set T2PR_EXT_PUBLISHER.\n" +
        "  Create one at https://marketplace.visualstudio.com/manage — it is the short immutable\n" +
        "  id (for example contoso-devtools), not the display name. tfx names the .vsix after it\n" +
        "  and the Marketplace refuses an id you do not own.",
    );
  }
  if (config.publisher.startsWith("REPLACE-WITH") || !PUBLISHER.test(config.publisher)) {
    throw new ConfigError(
      `"${config.publisher}" is not a Marketplace publisher id. Expected lowercase letters, ` +
        "digits and hyphens, for example contoso-devtools.",
    );
  }
  if (config.version && !/^\d+\.\d+\.\d+$/.test(config.version)) {
    throw new ConfigError(`--version expects x.y.z, got "${config.version}".`);
  }
  if (config.repositoryUrl && !/^https:\/\/[^\s]+$/.test(config.repositoryUrl)) {
    throw new ConfigError(`--repo-url must be an https URL, got "${config.repositoryUrl}".`);
  }

  if (!pinned) {
    // A deliberate, complete answer: ship no pinned settings and let each user
    // fill in the hub's settings form. Not a silent half-configuration.
    return { pinned: false };
  }

  const missing = [
    !base.agentUrl && "--agent-url (or AGENT_TICKET_TO_PR_INVOCATIONS_ENDPOINT in the azd environment)",
    !base.entraClientId && "--client-id (or T2PR_ENTRA_CLIENT_ID)",
    !base.entraTenantId && "--tenant-id (or T2PR_ENTRA_TENANT_ID / AZURE_TENANT_ID)",
  ].filter(Boolean);
  if (missing.length > 0) {
    throw new ConfigError(
      "Partially pinned settings. The hub ignores a half-filled agent-config.json and silently\n" +
        "falls back to per-user settings, so this is refused here instead. Missing:\n" +
        missing.map((m) => `  - ${m}`).join("\n") +
        "\nEither supply all of them, or none (then every user configures their own agent).",
    );
  }

  const result = validateSettings(base);
  if (!result.valid) {
    throw new ConfigError(
      `The pinned settings would be rejected by the extension at runtime:\n  ${result.message}`,
    );
  }
  for (const [name, override] of Object.entries(config.agentConfig.projects)) {
    const merged = { ...base, ...override };
    const check = validateSettings(merged);
    if (!check.valid) {
      throw new ConfigError(
        `The override for Azure DevOps project "${name}" would be rejected at runtime:\n  ${check.message}`,
      );
    }
  }
  return { pinned: true };
}

// ------------------------------------------------------------------ outputs

async function writeManifest(config) {
  const manifest = JSON.parse(await readFile(MANIFEST_SRC, "utf8"));
  manifest.publisher = config.publisher;
  if (config.version) manifest.version = config.version;
  if (config.repositoryUrl) {
    const repo = config.repositoryUrl.replace(/\/+$/, "");
    manifest.links = {
      ...(manifest.links ?? {}),
      repository: { uri: repo },
      support: { uri: `${repo}/issues` },
    };
    manifest.repository = { type: "git", uri: repo };
  }
  const remaining = JSON.stringify(manifest).match(/REPLACE-WITH-[A-Z-]+/g);
  if (remaining) {
    throw new ConfigError(
      `The generated manifest still contains placeholders: ${[...new Set(remaining)].join(", ")}.\n` +
        "  Pass --repo-url <https://…> so the Marketplace Repository and Support links point at\n" +
        "  your own fork.",
    );
  }
  await writeFile(MANIFEST_OUT, `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

/**
 * `dist/` is rebuilt from scratch by `npm run build`, which reads
 * deploy.local.json itself — so this write is only for the case where somebody
 * configures after building and wants the change without a rebuild. Harmless
 * either way, and it makes `configure` self-contained for setup.sh.
 */
async function writeDistConfig(config) {
  const dist = resolve(root, "dist");
  await mkdir(dist, { recursive: true });
  await writeFile(
    resolve(dist, "agent-config.json"),
    `${JSON.stringify({ _comment: GENERATED_COMMENT, ...config.agentConfig }, null, 2)}\n`,
  );
}

async function readDeployFile() {
  if (!existsSync(DEPLOY_FILE)) {
    throw new ConfigError(
      "extension/deploy.local.json does not exist, so there is nothing to package but placeholders.\n" +
        "  Run `npm run configure -- --publisher <your-publisher-id>` first (add --client-id,\n" +
        "  --tenant-id and --agent-url to pin the agent settings for everyone in the organization).\n" +
        "  See docs/extension.md step 3.",
    );
  }
  return JSON.parse(await readFile(DEPLOY_FILE, "utf8"));
}

// --------------------------------------------------------------------- main

async function main() {
  const flags = parseArgs(process.argv.slice(2));
  if (flags.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  if (flags.print) {
    const existing = await readDeployFile();
    process.stdout.write(`${JSON.stringify(existing, null, 2)}\n`);
    return;
  }

  const config = flags.verify ? await readDeployFile() : resolveInputs(flags);
  if (flags.verify) {
    // Normalise anything an older or hand-edited file might be missing.
    config.agentConfig = config.agentConfig ?? { default: {}, projects: {} };
    config.agentConfig.default = config.agentConfig.default ?? {};
    config.agentConfig.projects = config.agentConfig.projects ?? {};
  }

  const { module: host, dir } = await loadValidators();
  let outcome;
  try {
    outcome = validate(config, host);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }

  const record = {
    _comment:
      "Generated by extension/scripts/configure.mjs. Untracked and gitignored on purpose: it " +
      "holds your tenant's values. Re-run `npm run configure` to change it; delete it to go back " +
      "to an unconfigured tree.",
    generatedAt: new Date().toISOString(),
    publisher: config.publisher,
    ...(config.repositoryUrl ? { repositoryUrl: config.repositoryUrl } : {}),
    ...(config.version ? { version: config.version } : {}),
    agentConfig: config.agentConfig,
  };
  await writeFile(DEPLOY_FILE, `${JSON.stringify(record, null, 2)}\n`);
  const manifest = await writeManifest(record);
  await writeDistConfig(record);

  const lines = [
    `configured  publisher=${record.publisher}  version=${manifest.version}`,
    outcome.pinned
      ? `            agent pinned for the whole organization: ${record.agentConfig.default.agentUrl}`
      : "            no pinned agent settings — each user configures their own in the hub",
  ];
  if (Object.keys(record.agentConfig.projects).length > 0) {
    lines.push(
      `            per-project overrides: ${Object.keys(record.agentConfig.projects).join(", ")}`,
    );
  }
  lines.push(
    "            wrote extension/deploy.local.json, extension/vss-extension.local.json,",
    "                  extension/dist/agent-config.json  (all gitignored)",
    "            next: npm run package",
  );
  process.stdout.write(`${lines.join("\n")}\n`);
}

try {
  await main();
} catch (error) {
  if (error instanceof ConfigError) {
    process.stderr.write(`\nconfigure: ${error.message}\n\n`);
    process.exit(1);
  }
  throw error;
}
