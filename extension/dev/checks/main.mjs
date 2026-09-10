import { run as runAudienceChecks } from "./checks-audience.mjs";
import { run as runHostChecks } from "./checks-host.mjs";
import { run as runHubChecks } from "./checks-hub.mjs";
import { summary } from "./harness.mjs";

console.log("");
console.log("ticket-to-pr extension — runtime checks");
console.log("");
console.log("src/host.ts + src/ado-host.ts — token audience and destination host");
await runAudienceChecks();
console.log("");
console.log("src/ado-host.ts (ADO SDK, MSAL and fetch stubbed)");
await runHostChecks();
console.log("");
console.log("src/hub.ts (real bootstrap over real hub.html markup, HubHost faked)");
await runHubChecks();

process.exit(summary() ? 0 : 1);
