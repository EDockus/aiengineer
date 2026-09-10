/**
 * Minimal DOM helpers.
 *
 * Everything here builds nodes and sets `textContent`. There is no `innerHTML`
 * anywhere in this extension — work item titles, repository names and agent
 * error messages are all attacker-influenced text and are only ever written
 * into the DOM as text nodes, so markup in them renders as characters.
 */

type Attrs = Record<string, string | number | boolean | undefined>;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: (Node | string)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (key === "class") {
      node.className = String(value);
    } else if (value === true) {
      node.setAttribute(key, "");
    } else {
      node.setAttribute(key, String(value));
    }
  }
  for (const child of children) {
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

/** Replaces an element's children with text. Never parses markup. */
export function setText(node: Element, text: string): void {
  node.textContent = text;
}

export function clear(node: Element): void {
  node.replaceChildren();
}

export function requireEl<T extends Element>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`Missing required element: ${selector}`);
  return node;
}

export function show(node: HTMLElement, visible: boolean): void {
  node.hidden = !visible;
}

/**
 * Generates a correlation id for a run. `crypto.randomUUID` needs a secure
 * context, which both dev (localhost) and the packaged hub satisfy, but the
 * fallback keeps this from being a hard dependency.
 */
export function correlationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `t2pr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
