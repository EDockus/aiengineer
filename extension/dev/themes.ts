/**
 * Azure DevOps theme palettes for the dev harness.
 *
 * In production the host sends these values over the SDK handshake and
 * SDK.applyTheme writes them onto :root as `--<name>: <value>`. Here the harness
 * does the same thing with the same variable names, so styles.css is exercised
 * exactly as it will be in the host — including the dark theme.
 *
 * The dark values are transcribed from Azure DevOps' own dark palette. The light
 * values are the standard ADO light ramp; treat them as a close approximation
 * rather than a byte-for-byte copy, since the authoritative values only exist at
 * runtime inside a real organization.
 */

export type ThemeName = "light" | "dark";

const SHARED = {
  "palette-primary": "0, 120, 212",
  "communication-background": "rgba(var(--palette-primary), 1)",
};

const LIGHT: Record<string, string> = {
  ...SHARED,
  "palette-neutral-0": "255, 255, 255",
  "palette-neutral-2": "250, 249, 248",
  "palette-neutral-4": "244, 243, 242",
  "palette-neutral-6": "239, 238, 237",
  "palette-neutral-8": "234, 233, 232",
  "palette-neutral-10": "218, 217, 216",
  "palette-neutral-20": "200, 198, 196",
  "palette-neutral-30": "166, 166, 166",
  "palette-neutral-60": "96, 94, 92",
  "palette-neutral-70": "72, 70, 68",
  "palette-neutral-80": "51, 50, 49",
  "palette-neutral-100": "0, 0, 0",
  "palette-accent1-light": "249, 209, 208",
  "palette-accent1": "205, 74, 69",
  "palette-accent2-light": "223, 246, 221",
  "palette-accent2": "16, 124, 16",
  "palette-accent3-light": "253, 243, 209",
  "palette-accent3": "241, 171, 0",
  "background-color": "rgba(var(--palette-neutral-0), 1)",
  "text-primary-color": "rgba(var(--palette-neutral-100), .9)",
  "text-secondary-color": "rgba(var(--palette-neutral-100), .55)",
  "text-disabled-color": "rgba(var(--palette-neutral-100), .38)",
  "text-on-communication-background": "rgba(var(--palette-neutral-0), 1)",
  "border-subtle-color": "rgba(var(--palette-neutral-100), .08)",
};

/** Transcribed from the Azure DevOps dark theme custom properties. */
const DARK: Record<string, string> = {
  ...SHARED,
  "palette-neutral-0": "32, 31, 30",
  "palette-neutral-2": "37, 36, 35",
  "palette-neutral-4": "41, 40, 39",
  "palette-neutral-6": "50, 49, 48",
  "palette-neutral-8": "59, 58, 57",
  "palette-neutral-10": "72, 70, 68",
  "palette-neutral-20": "96, 94, 92",
  "palette-neutral-30": "121, 119, 117",
  "palette-neutral-60": "161, 159, 157",
  "palette-neutral-70": "190, 187, 184",
  "palette-neutral-80": "225, 223, 221",
  "palette-neutral-100": "255, 255, 255",
  "palette-accent1-light": "68, 39, 38",
  "palette-accent1": "241, 112, 123",
  "palette-accent2-light": "57, 61, 27",
  "palette-accent2": "146, 195, 83",
  "palette-accent3-light": "67, 53, 25",
  "palette-accent3": "253, 185, 19",
  "background-color": "rgba(var(--palette-neutral-0), 1)",
  "text-primary-color": "rgba(var(--palette-neutral-100), .95)",
  "text-secondary-color": "rgba(var(--palette-neutral-100), .80)",
  "text-disabled-color": "rgba(var(--palette-neutral-100), .5)",
  "text-on-communication-background": "var(--text-primary-color)",
  "border-subtle-color": "rgba(var(--palette-neutral-100), .08)",
};

export const THEMES: Record<ThemeName, Record<string, string>> = { light: LIGHT, dark: DARK };

let styleElement: HTMLStyleElement | undefined;

/**
 * Mirrors SDK.applyTheme: writes the palette to :root as custom properties.
 * Same mechanism, same variable names.
 */
export function applyTheme(name: ThemeName): void {
  const theme = THEMES[name];
  if (!styleElement) {
    styleElement = document.createElement("style");
    document.head.append(styleElement);
  }
  const vars = Object.entries(theme).map(([key, value]) => `--${key}: ${value}`);
  styleElement.textContent = `:root { ${vars.join("; ")} } body { color: var(--text-primary-color) }`;
}
