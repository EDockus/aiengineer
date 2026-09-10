/*
 * A DOM small enough to run src/hub.ts under Node, and no smaller.
 *
 * The hub touches the DOM through src/dom.ts only: createElement, createTextNode,
 * textContent, append, replaceChildren, setAttribute/removeAttribute, dataset,
 * hidden, className, value/disabled/readOnly/open, focus/select, and
 * addEventListener. That is the whole surface implemented here.
 *
 * Elements are seeded from the real src/hub.html — every id in the shipped
 * markup, with its real tag name — so a check fails if the markup and the code
 * disagree about an id, exactly as requireEl() would fail in a browser.
 */
import { readFileSync } from "node:fs";

class StubNode {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.dataset = {};
    this.listeners = new Map();
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.open = false;
    this.value = "";
    this.placeholder = "";
    this.focusCount = 0;
    this._text = "";
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.children = [];
    this._text = value == null ? "" : String(value);
  }

  append(...nodes) {
    for (const node of nodes) {
      this.children.push(typeof node === "string" ? createTextNode(node) : node);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.listeners.get(type) ?? [];
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  dispatchEvent(event) {
    for (const handler of [...(this.listeners.get(event.type) ?? [])]) handler(event);
    return true;
  }

  focus() {
    this.focusCount += 1;
  }

  select() {}
}

function createTextNode(text) {
  const node = new StubNode("#text");
  node.textContent = text;
  return node;
}

/** Depth-first walk over an element tree. */
export function walk(node, visit) {
  visit(node);
  for (const child of node.children) walk(child, visit);
}

/** Every descendant with the given tag name. */
export function findAll(node, tagName) {
  const wanted = tagName.toUpperCase();
  const found = [];
  walk(node, (n) => {
    if (n.tagName === wanted) found.push(n);
  });
  return found;
}

/** Reads every `id="..."` in the shipped markup, with the tag it belongs to. */
function idsFromMarkup(html) {
  const found = [];
  const pattern = /<([a-zA-Z][a-zA-Z0-9-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;
  let match;
  while ((match = pattern.exec(html)) !== null) {
    const id = /\sid="([^"]+)"/.exec(match[2]);
    if (id) found.push({ tag: match[1], id: id[1] });
  }
  return found;
}

/**
 * Installs the stub document/window globals and returns a handle to the page.
 * `hubHtmlPath` is the real src/hub.html.
 */
export function installDom(hubHtmlPath) {
  const html = readFileSync(hubHtmlPath, "utf8");
  const byId = new Map();
  const body = new StubNode("body");

  for (const { tag, id } of idsFromMarkup(html)) {
    const node = new StubNode(tag);
    node.setAttribute("id", id);
    node.id = id;
    byId.set(id, node);
    body.append(node);
  }

  const documentStub = {
    body,
    createElement: (tag) => new StubNode(tag),
    createTextNode,
    querySelector(selector) {
      if (!selector.startsWith("#")) throw new Error(`stub DOM: unsupported selector ${selector}`);
      return byId.get(selector.slice(1)) ?? null;
    },
  };

  const windowListeners = new Map();
  const windowStub = {
    addEventListener(type, handler) {
      if (!windowListeners.has(type)) windowListeners.set(type, []);
      windowListeners.get(type).push(handler);
    },
    removeEventListener() {},
    open() {},
    location: { origin: "https://checks.invalid", search: "" },
  };

  globalThis.document = documentStub;
  globalThis.window = windowStub;
  globalThis.HTMLElement = StubNode;

  return {
    document: documentStub,
    window: windowStub,
    body,
    el: (id) => {
      const node = byId.get(id);
      if (!node) throw new Error(`stub DOM: no element with id "${id}" in hub.html`);
      return node;
    },
    /** Fires a window-level event, e.g. beforeunload, to exercise teardown. */
    fireWindow(type) {
      for (const handler of [...(windowListeners.get(type) ?? [])]) handler({ type });
    },
    /** Fires a submit event on a form, the way the browser would. */
    submitForm(id) {
      let prevented = false;
      const form = byId.get(id);
      if (!form) throw new Error(`stub DOM: no form with id "${id}"`);
      form.dispatchEvent({
        type: "submit",
        target: form,
        preventDefault() {
          prevented = true;
        },
      });
      return prevented;
    },
  };
}
