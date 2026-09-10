/**
 * Production entry point for the hub page.
 *
 * The only thing this file does is choose the real host implementation. The dev
 * harness (dev/main.ts) has an identical shape but passes the stub host, so both
 * run the same hub.ts.
 */
import { bootstrap } from "./hub.js";
import { createAdoHost } from "./ado-host.js";

void bootstrap(createAdoHost()).catch((error: unknown) => {
  // Nothing rendered, so there is no status panel to write into.
  const message =
    error instanceof Error ? error.message : "The extension failed to start.";
  const fallback = document.createElement("p");
  fallback.className = "message";
  fallback.dataset["tone"] = "error";
  fallback.textContent = `The extension could not start: ${message}`;
  document.body.append(fallback);
});
