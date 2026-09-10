import { rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
for (const dir of ["dist", "dist-vsix", "dev/dist", "dev/dist-checks"]) {
  await rm(resolve(root, dir), { recursive: true, force: true });
}
console.log("cleaned");
