/**
 * Dev harness entry point. Runs *inside* the hub iframe.
 *
 * Mirrors src/main.ts exactly, except that it passes the stub host instead of
 * the ADO/MSAL host, and applies a theme itself (in production the ADO host
 * frame does that over the SDK handshake).
 */
import { bootstrap } from "../src/hub.js";
import { DEFAULT_OPTIONS, SCENARIOS, StubHost, type HarnessOptions, type Scenario } from "./stub-host.js";
import { applyTheme, type ThemeName } from "./themes.js";

interface HarnessBridge {
  setScenario(scenario: Scenario): void;
  setTiming(phaseMs: number, latencyMs: number): void;
  onLog(sink: (line: string) => void): void;
  options: HarnessOptions;
}

declare global {
  interface Window {
    __t2prHarness?: HarnessBridge;
    /** Scenario list, derived from the contract's own error-code array. */
    __t2prScenarios?: typeof SCENARIOS;
  }
}

function readOptions(): { options: HarnessOptions; theme: ThemeName } {
  const params = new URLSearchParams(window.location.search);
  const pick = <T extends string>(key: string, allowed: readonly T[], fallback: T): T => {
    const value = params.get(key);
    return value && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
  };
  const num = (key: string, fallback: number): number => {
    const value = Number(params.get(key));
    return Number.isFinite(value) && value >= 0 ? value : fallback;
  };

  return {
    theme: pick("theme", ["light", "dark"] as const, "light"),
    options: {
      ...DEFAULT_OPTIONS,
      scenario: (params.get("scenario") as Scenario) || DEFAULT_OPTIONS.scenario,
      phaseMs: num("phaseMs", DEFAULT_OPTIONS.phaseMs),
      latencyMs: num("latencyMs", DEFAULT_OPTIONS.latencyMs),
      workItemMode: pick("workItems", ["normal", "empty", "error"] as const, "normal"),
      repositoryMode: pick("repos", ["normal", "empty", "error"] as const, "normal"),
      configured: params.get("configured") !== "false",
      managed: params.get("managed") === "true",
    },
  };
}

const { options, theme } = readOptions();
applyTheme(theme);

const host = new StubHost(options);

window.__t2prScenarios = SCENARIOS;
window.__t2prHarness = {
  options,
  setScenario(scenario) {
    options.scenario = scenario;
    host.setOptions(options);
  },
  setTiming(phaseMs, latencyMs) {
    options.phaseMs = phaseMs;
    options.latencyMs = latencyMs;
    host.setOptions(options);
  },
  onLog(sink) {
    host.onLog(sink);
  },
};

// Tell the parent harness page we are ready to receive control changes.
window.parent?.postMessage({ type: "t2pr-harness-ready" }, window.location.origin);

void bootstrap(host).catch((error: unknown) => {
  const message = error instanceof Error ? error.message : String(error);
  const node = document.createElement("p");
  node.className = "message";
  node.dataset["tone"] = "error";
  node.textContent = `The hub failed to start: ${message}`;
  document.body.append(node);
});
