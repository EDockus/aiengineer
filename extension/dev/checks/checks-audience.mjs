/*
 * Runtime checks for the token audience — MEDIUM-7 and the control it must not
 * re-open.
 *
 * Two things are being held down here at once, and they pull against each other:
 *
 *   1. The extension has to ask for the audience a Microsoft Foundry hosted
 *      agent actually accepts. That is `https://ai.azure.com/.default`
 *      ("Microsoft Entra ID uses OAuth 2.0 bearer tokens scoped to
 *      https://ai.azure.com/.default" — Authentication and authorization in
 *      Microsoft Foundry; and the Invocations sample in Deploy a hosted agent
 *      calls `credential.get_token("https://ai.azure.com/.default")`).
 *
 *   2. Admitting that literal must NOT be done by widening the validator to
 *      `https://…/.default`. That single character of slack would re-admit
 *      Microsoft Graph, ARM, Key Vault and the Azure DevOps resource — the
 *      exact hole the MEDIUM-7 fix closed. So the validator is an
 *      exact-literal allowlist, and the adversarial corpus below is the proof
 *      that it stayed one.
 *
 * A regression here is not a cosmetic test failure: it means the settings
 * document can once again name a first-party audience and this page will fetch
 * a token for it.
 */
import { AgentRequestError, createAdoHost } from "../../src/ado-host.ts";
import {
  ALLOWED_AGENT_SCOPES,
  FOUNDRY_AGENT_SCOPE,
  validateAgentScope,
  validateAgentUrl,
} from "../../src/host.ts";
import * as sdk from "./sdk-stub.mjs";
import * as msal from "./msal-stub.mjs";
import { assert, assertEqual, check, note, rejects } from "./harness.mjs";

const FOUNDRY_SCOPE = "https://ai.azure.com/.default";
const FOUNDRY_URL =
  "https://contoso.services.ai.azure.com/api/projects/platform/agents/ticket-to-pr/endpoint/protocols";
const SETTINGS_KEY = "settings.project-guid";

/**
 * Every scope string that must be refused. The first eleven are the ones the
 * MEDIUM-7 security review ran against the previous validator and named in the
 * send-back; the rest widen the same idea. Each entry is
 * `[value, why it matters]` so a failure names the hole it re-opened.
 */
const REJECTED_SCOPES = [
  ["https://graph.microsoft.com/.default", "Microsoft Graph — the whole directory as the user"],
  ["https://management.azure.com/.default", "ARM — every subscription the user can reach"],
  ["499b84ac-1321-427f-aa17-267ca6975798/.default", "the bare Azure DevOps resource GUID"],
  ["https://app.vssps.visualstudio.com/.default", "Azure DevOps profile/identity service"],
  [`${FOUNDRY_SCOPE} https://graph.microsoft.com/.default`, "multi-scope smuggling via a space"],
  [`${FOUNDRY_SCOPE}\nhttps://graph.microsoft.com/.default`, "multi-scope smuggling via a newline"],
  [`https://graph.microsoft.com/.default ${FOUNDRY_SCOPE}`, "the same smuggling, Graph first"],
  ["https://ai.azure.com/.DEFAULT", "a .default case variant"],
  ["https://ai.azure.com/.Default", "another .default case variant"],
  ["HTTPS://AI.AZURE.COM/.DEFAULT", "the whole literal upper-cased"],
  ["https://vault.azure.net/.default", "Key Vault — every secret the user can read"],
  ["https://storage.azure.com/.default", "Azure Storage data plane"],
  ["https://management.core.windows.net/.default", "the legacy ARM audience"],
  ["https://cognitiveservices.azure.com/.default", "a neighbouring AI audience that is still not this one"],
  [`${FOUNDRY_SCOPE}\thttps://graph.microsoft.com/.default`, "multi-scope smuggling via a tab"],
  [`openid profile ${FOUNDRY_SCOPE}`, "OIDC scopes prepended to the literal"],
  ["https://ai.azure.com.evil.test/.default", "a look-alike host that merely starts with the real one"],
  ["https://evil.test/ai.azure.com/.default", "the real audience demoted to a path segment"],
  ["http://ai.azure.com/.default", "the right resource over plain http"],
  ["https://ai.azure.com//.default", "a doubled separator"],
  ["https://ai.azure.com/.default/", "a trailing slash"],
  ["https://ai.azure.com/.default?resource=graph", "a query string glued on"],
  ["https://ai.azure.com/user_impersonation", "the right resource, but not the .default form"],
  ["api://66666666-7777-8888-9999-000000000000/.default", "the old api:// form, which no deployment produces"],
  ["", "an empty scope"],
];

/** URLs that must be refused, now that the audience is shared and first-party. */
const REJECTED_URLS = [
  ["https://evil.test", "a host with nothing to do with Foundry"],
  ["https://graph.microsoft.com", "a Microsoft host that is not a Foundry endpoint"],
  ["https://contoso.services.ai.azure.com@evil.test/", "userinfo that reads as the Foundry host"],
  ["https://services.ai.azure.com", "the bare suffix with no account label"],
  ["https://evilservices.ai.azure.com", "a host that ends in the suffix without the dot"],
  ["https://contoso.services.ai.azure.com.evil.test", "the suffix demoted to a middle label"],
  ["http://contoso.services.ai.azure.com", "the right host over plain http"],
  [`${FOUNDRY_URL}?api-version=v1`, "a query string in the base URL"],
  [`${FOUNDRY_URL}#frag`, "a fragment in the base URL"],
  ["https://xn--i-7iq.azure.com", "a punycoded look-alike host"],
  ["", "an empty URL"],
];

function bootFetchStub(packaged) {
  globalThis.fetch = async (url) => {
    if (String(url).endsWith("agent-config.json")) {
      return { ok: false, status: 404, async json() { return null; } };
    }
    throw new Error(`unexpected fetch of ${url}`);
  };
  return packaged;
}

export async function run() {
  await check("MEDIUM-7: the one audience a Foundry hosted agent accepts is accepted", async () => {
    assertEqual(ALLOWED_AGENT_SCOPES.length, 1, "the allowlist holds exactly one literal");
    assertEqual(ALLOWED_AGENT_SCOPES[0], FOUNDRY_SCOPE, "and it is the Foundry data-plane scope");
    assertEqual(FOUNDRY_AGENT_SCOPE, FOUNDRY_SCOPE, "the exported constant agrees");
    assert(validateAgentScope(FOUNDRY_SCOPE).valid, `${FOUNDRY_SCOPE} is accepted`);
    assert(
      validateAgentScope(`  ${FOUNDRY_SCOPE}  `).valid,
      "surrounding whitespace is trimmed, not treated as a second scope",
    );
    note("audience source: Authentication and authorization in Microsoft Foundry (learn.microsoft.com)");
  });

  await check("MEDIUM-7: the certified-closed audiences are still refused, all of them", async () => {
    note(`${REJECTED_SCOPES.length} adversarial scope strings`);
    for (const [value, why] of REJECTED_SCOPES) {
      const result = validateAgentScope(value);
      assert(!result.valid, `refused (${why}): ${JSON.stringify(value)}`);
    }
  });

  await check("MEDIUM-7: the validator is a literal allowlist, not a widened pattern", async () => {
    // The tempting fix for MEDIUM-7 was to loosen the pattern to admit
    // `https://…/.default`. This check fails loudly if anyone ever does: a
    // pattern-shaped validator accepts arbitrary hosts, a literal one cannot.
    const fuzz = [];
    for (const host of ["graph.microsoft.com", "management.azure.com", "vault.azure.net", "evil.test"]) {
      fuzz.push(`https://${host}/.default`);
    }
    for (const value of fuzz) {
      assert(!validateAgentScope(value).valid, `a widened https://…/.default would have let through ${value}`);
    }
    const message = validateAgentScope("https://graph.microsoft.com/.default").message ?? "";
    assert(message.includes(FOUNDRY_SCOPE), "the refusal names the one scope that works");
  });

  await check("MEDIUM-7: only a Foundry host may receive the shared first-party token", async () => {
    // `https://ai.azure.com` is not this agent's private audience: the token is
    // the user's Foundry data-plane token, good against every Foundry project
    // and agent they can reach. That makes the URL check load-bearing, so it is
    // pinned to the Foundry endpoint host family.
    assert(validateAgentUrl(FOUNDRY_URL).valid, "a real Foundry agent endpoint is accepted");
    assert(
      validateAgentUrl("https://contoso.services.ai.azure.com").valid,
      "so is the bare project host",
    );
    for (const [value, why] of REJECTED_URLS) {
      assert(!validateAgentUrl(value).valid, `refused (${why}): ${JSON.stringify(value)}`);
    }
  });

  await check("MEDIUM-7: a hostile audience in storage still buys no token", async () => {
    for (const scope of [
      "https://graph.microsoft.com/.default",
      "https://management.azure.com/.default",
      "499b84ac-1321-427f-aa17-267ca6975798/.default",
    ]) {
      sdk.reset();
      msal.reset();
      sdk.state.documents.set(`User:${SETTINGS_KEY}`, {
        agentUrl: FOUNDRY_URL,
        entraClientId: "11111111-2222-3333-4444-555555555555",
  entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        agentScope: scope,
      });
      bootFetchStub();
      const host = createAdoHost();
      await host.initialize();
      const loaded = await host.loadSettings();
      assertEqual(loaded.settings.agentScope, "", `${scope} never becomes live settings`);

      const sent = [];
      globalThis.fetch = async (url) => {
        sent.push(String(url));
        return { ok: true, status: 202, async json() { return { runId: "r" }; } };
      };
      const error = await rejects(host.submitRun({ version: "1" }), "submitRun refuses to run");
      assert(error instanceof AgentRequestError, "as a configuration failure");
      assertEqual(
        msal.state.silentCalls.length + msal.state.popupCalls.length,
        0,
        "no token was requested for it",
      );
      assertEqual(sent.length, 0, "and nothing was sent anywhere");
    }
  });

  await check("MEDIUM-7: a non-Foundry URL with the right audience buys no token either", async () => {
    sdk.reset();
    msal.reset();
    sdk.state.documents.set(`User:${SETTINGS_KEY}`, {
      agentUrl: "https://evil.test",
      entraClientId: "11111111-2222-3333-4444-555555555555",
  entraTenantId: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      agentScope: FOUNDRY_SCOPE,
    });
    bootFetchStub();
    const host = createAdoHost();
    await host.initialize();
    const loaded = await host.loadSettings();
    assertEqual(loaded.settings.agentUrl, "", "the hostile URL is discarded on load");

    const sent = [];
    globalThis.fetch = async (url) => {
      sent.push(String(url));
      return { ok: true, status: 202, async json() { return { runId: "r" }; } };
    };
    await rejects(host.submitRun({ version: "1" }), "submitRun refuses to run");
    assertEqual(
      msal.state.silentCalls.length + msal.state.popupCalls.length,
      0,
      "the Foundry token was never minted for a non-Foundry host",
    );
    assertEqual(sent.length, 0, "and nothing was sent to evil.test");
  });
}
