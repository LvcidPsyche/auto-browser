"""Browser-side JavaScript constants used by BrowserManager for page observation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

# Injected before every page load via add_init_script().
# Removes automation signals, mocks realistic browser properties.
# add_init_script runs the source as written, so this is an invoked function
# expression. A bare arrow function would be defined and never called, and no
# patch would apply.
STEALTH_INIT_SCRIPT = r"""
(() => {
  // Remove webdriver flag
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined, configurable: true,
    });
  } catch (_) {}

  // Chrome runtime object (many sites check for this). Headless Chromium already
  // defines window.chrome (loadTimes, csi, app) but not runtime, so fill in only
  // the members that are missing instead of skipping when the object exists.
  try {
    const chromeStub = {
      runtime: {
        onMessage: { addListener: () => {}, removeListener: () => {} },
        connect: () => ({ onDisconnect: { addListener: () => {} }, postMessage: () => {} }),
        sendMessage: () => {},
        id: undefined,
      },
      loadTimes: () => ({}),
      csi: () => ({}),
      app: { isInstalled: false },
    };
    if (!window.chrome) window.chrome = {};
    for (const key of Object.keys(chromeStub)) {
      if (!(key in window.chrome)) window.chrome[key] = chromeStub[key];
    }
  } catch (_) {}

  // Realistic navigator.plugins (headless has none by default)
  try {
    const plugins = [
      { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
      { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
      { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ];
    const arr = { length: plugins.length, refresh: () => {}, item: (i) => plugins[i], namedItem: (n) => plugins.find(p => p.name === n) || null };
    plugins.forEach((p, i) => { arr[i] = p; });
    Object.setPrototypeOf(arr, PluginArray.prototype);
    Object.defineProperty(navigator, 'plugins', { get: () => arr });
  } catch (_) {}

  // Languages
  try {
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  } catch (_) {}

  // Permissions API patch
  try {
    const origQuery = window.Permissions.prototype.query;
    window.Permissions.prototype.query = function(params) {
      if (params.name === 'notifications') {
        return Promise.resolve({ state: Notification.permission, onchange: null });
      }
      return origQuery.call(this, params);
    };
  } catch (_) {}

  // Canvas fingerprint noise
  try {
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    // The pixel is read with the native getImageData, captured here. The
    // fingerprint script (STEALTH_PROFILE=light) wraps getImageData with its own
    // noise; reading through that and writing the result back would change the
    // canvas a little more on every export.
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
      const ctx = this.getContext('2d');
      if (ctx && this.width > 0 && this.height > 0) {
        try {
          const img = origGetImageData.call(ctx, 0, 0, 1, 1);
          img.data[0] ^= 1;
          ctx.putImageData(img, 0, 0);
          const result = origToDataURL.call(this, type, quality);
          img.data[0] ^= 1;
          ctx.putImageData(img, 0, 0);
          return result;
        } catch (_) {}
      }
      return origToDataURL.call(this, type, quality);
    };
  } catch (_) {}

  // WebGL vendor / renderer — realistic Intel values
  try {
    const patchGL = (proto) => {
      const orig = proto.getParameter;
      proto.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return orig.call(this, p);
      };
    };
    patchGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patchGL(WebGL2RenderingContext.prototype);
  } catch (_) {}

  // Realistic hardware values
  try { Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 }); } catch (_) {}
  try { Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 }); } catch (_) {}
  try { Object.defineProperty(screen, 'colorDepth', { get: () => 24 }); } catch (_) {}
})();
"""

# Page-controlled strings are bounded where a script reads them: an attribute-like value (name, id, type, role,
# class, form method) like a label, a URL longer, a title in between. A cut URL ends in AB_TRUNCATED_MARK, still within
# its cap, so no consumer mistakes it for the real address. Injected into every script that returns page strings
# (``/*__AB_CAP__*/`` or through _ELEMENT_NAMING_JS), and into the scripts in tool_gateway and cdp.
AB_ATTR_MAX = 120
AB_HREF_MAX = 2000
AB_TITLE_MAX = 200
# The visible text of a found element, and the text that matched a find_elements query (a regex can match a whole node).
AB_TEXT_MAX = 200
AB_MATCH_MAX = 500
# The attributes a find_elements result sends only so the Python side can decide whether a value is a secret (they
# never reach the caller). Read past the display cap, or a long name could push `password` out of the check; one
# longer still is flagged `truncated`, and its value is withheld because the check did not see all of it.
AB_FIELD_ATTR_MAX = 1024
# How many attributes (and inline event handler names) one element read returns.
AB_MAX_ATTRIBUTES = 50
AB_TRUNCATED_MARK = chr(0x2026) + "[truncated]"

AB_CAP_JS = (
    f"""
  const AB_ATTR_MAX = {AB_ATTR_MAX};
  const AB_HREF_MAX = {AB_HREF_MAX};
  const AB_TITLE_MAX = {AB_TITLE_MAX};
  const AB_TEXT_MAX = {AB_TEXT_MAX};
  const AB_MATCH_MAX = {AB_MATCH_MAX};
  const AB_TRUNCATED_MARK = {json.dumps(AB_TRUNCATED_MARK)};
"""
    + r"""
  // UTF-16 units: a cut that lands inside a surrogate pair backs off one unit, so no lone surrogate is left.
  function abSlice(value, end) {
    const cut = value.slice(0, end);
    const last = cut.charCodeAt(cut.length - 1);
    return last >= 0xD800 && last <= 0xDBFF ? cut.slice(0, -1) : cut;
  }
  function abCap(value, max) {
    return typeof value === 'string' && value.length > max ? abSlice(value, max) : value;
  }
  function abCapUrl(value, max = AB_HREF_MAX) {
    if (typeof value !== 'string' || value.length <= max) return value;
    return abSlice(value, max - AB_TRUNCATED_MARK.length) + AB_TRUNCATED_MARK;
  }
  // A list of words (class) is cut between words when it can be.
  function abCapWords(value, max) {
    const cut = abCap(value, max);
    if (typeof value !== 'string' || cut === value || /\s/.test(value.charAt(cut.length))) return cut;
    const space = cut.search(/\s\S*$/);
    return space > 0 ? cut.slice(0, space) : cut;
  }
"""
)


def cap_text(value: Any, max_len: int) -> Any:
    """The Python side of abCap, for a string a script did not cap (page.title()) or one a hostile page could have."""
    return value[:max_len] if isinstance(value, str) else value


def cap_title(value: Any) -> Any:
    return cap_text(value, AB_TITLE_MAX)


def cap_url(value: Any, max_len: int = AB_HREF_MAX) -> Any:
    """The Python side of abCapUrl: a cut URL still ends in the marker, within its cap."""
    if not isinstance(value, str) or len(value) <= max_len:
        return value
    return value[: max_len - len(AB_TRUNCATED_MARK)] + AB_TRUNCATED_MARK


def cap_words(value: str, max_len: int) -> str:
    """The Python side of abCapWords: cut between words when the value has any before the cut."""
    if len(value) <= max_len or value[max_len].isspace():
        return value[:max_len]
    cut = value[:max_len]
    space = max((i for i, char in enumerate(cut) if char.isspace()), default=0)
    return cut[:space] if space > 0 else cut


# The caps of each string a find_elements result carries; any other string is held to the URL cap.
_FOUND_FIELD_CAPS = {
    "tag": AB_ATTR_MAX,
    "text": AB_TEXT_MAX,
    "match": AB_MATCH_MAX,
    "value": AB_ATTR_MAX,
    "id": AB_ATTR_MAX,
}
_FOUND_FLAGGED_FIELDS = ("value", "id", "class")


def _cap_found_strings(value: Any, caps: dict[str, int]) -> Any:
    """Every string in `value`, recursively, cut to the cap of its key (the URL cap, with the marker, if it has none)."""
    if isinstance(value, str):
        return cap_url(value)
    if isinstance(value, list):
        return [_cap_found_strings(item, caps) for item in value]
    if not isinstance(value, dict):
        return value
    capped: dict[str, Any] = {}
    for key, item in value.items():
        cap = caps.get(key)
        if not isinstance(item, str):
            capped[key] = _cap_found_strings(item, caps)
        elif key == "class":
            capped[key] = cap_words(item, AB_ATTR_MAX)
        else:
            capped[key] = cap_text(item, cap) if cap is not None else cap_url(item)
        if key in _FOUND_FLAGGED_FIELDS and isinstance(item, str) and capped[key] != item:
            capped[f"{key}_truncated"] = True
    return capped


def _cap_field(field: Any) -> dict[str, Any] | None:
    """The attributes the secret check reads, each held to AB_FIELD_ATTR_MAX; a cut sets `truncated`."""
    if not isinstance(field, dict):
        return None
    capped: dict[str, Any] = {}
    for key, item in field.items():
        capped[key] = cap_text(item, AB_FIELD_ATTR_MAX)
        if capped[key] != item:
            capped["truncated"] = True
    return capped


def cap_found_elements(elements: Any, context: int, is_secret_field: Callable[[Mapping[str, Any]], bool]) -> Any:
    """What find_elements returns, made safe again on the Python side.

    The scripts cap their strings, in the controller's isolated world, where the page's own JS cannot replace
    String.prototype.slice to get a megabyte through as it could in its own (NAV-12); every string is cut again here
    all the same, a second bound that holds whatever world ran them. A field's `value` is
    withheld (null, `value_redacted: true`) when `is_secret_field` says its attributes (sent by the script as `field`,
    which is removed) name a password, a one-time code and the like, or when one was too long to read whole; the type
    action does not echo such values either.
    """
    caps = {**_FOUND_FIELD_CAPS, "context_text": AB_MATCH_MAX + 2 * context}
    if not isinstance(elements, list):
        return _cap_found_strings(elements, caps)
    shown = []
    for raw in elements:
        # `field` is the check's input, not output: it is held to its own bound, not the display caps.
        field = _cap_field(raw.get("field")) if isinstance(raw, dict) else None
        element = _cap_found_strings(
            {k: v for k, v in raw.items() if k != "field"} if isinstance(raw, dict) else raw, caps
        )
        if isinstance(element, dict):
            if (
                field is not None
                and element.get("value")
                and (field.get("truncated") is True or is_secret_field(field))
            ):
                element["value"] = None
                element["value_redacted"] = True
                element.pop("value_truncated", None)
        shown.append(element)
    return shown


def with_ab_cap(source: str) -> str:
    return source.replace("/*__AB_CAP__*/", AB_CAP_JS)


# Shared by the observation scripts below, which inject it at the top of their
# function body (``/*__ELEMENT_NAMING__*/``) so all three name elements the same
# way. It follows the order of the accessible-name computation that matters in
# practice: aria-labelledby, aria-label, associated <label>s, button captions and
# image alt text, visible text, title, then placeholder, name and id. Every
# script that includes it takes the element-id registry as `elementIds`.
#
# A field's *value* is never used as its name. The previous fallback chain did,
# so a password typed into a field with no placeholder became that field's
# "label" in every later observation — in the interactables, the active element
# and the form outline — and from there reached model prompts, MCP clients and
# logs, even though the type action itself had redacted the text. Captions of
# submit/button/reset inputs are the one exception: that value is visible text.
# The same holds for contenteditable editors and textbox-like ARIA widgets,
# whose text content is what was typed into them.
_ELEMENT_NAMING_JS = (
    AB_CAP_JS
    + r"""
  function abClean(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function abLabelText(label) {
    const clone = label.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button').forEach((node) => node.remove());
    return abClean(clone.textContent);
  }

  function abName(el) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const labelledBy = (el.getAttribute('aria-labelledby') || '')
      .split(/\s+/)
      .filter(Boolean)
      .map((id) => document.getElementById(id))
      .filter(Boolean)
      .map((node) => abClean(node.innerText || node.textContent))
      .filter(Boolean)
      .join(' ');
    if (labelledBy) return labelledBy;
    const aria = abClean(el.getAttribute('aria-label'));
    if (aria) return aria;
    if (el.labels && el.labels.length) {
      const fromLabels = Array.from(el.labels).map(abLabelText).filter(Boolean).join(' ');
      if (fromLabels) return fromLabels;
    }
    if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) {
      const caption = abClean(el.value);
      if (caption) return caption;
    }
    if (tag === 'img' || (tag === 'input' && type === 'image')) {
      const alt = abClean(el.getAttribute('alt'));
      if (alt) return alt;
    }
    // What a field holds is its value, not its name. That covers rich-text
    // editors (contenteditable) and ARIA widgets whose content is their value
    // as well as native inputs: typed text into any of them must not come back
    // as the element's label.
    const explicitRole = abClean(el.getAttribute('role')).split(' ')[0];
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select'
      || el.isContentEditable
      || ['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'].includes(explicitRole);
    if (!isField) {
      const text = abClean(el.innerText);
      if (text) return text;
      const image = el.querySelector ? el.querySelector('img[alt]') : null;
      const alt = image ? abClean(image.getAttribute('alt')) : '';
      if (alt) return alt;
    }
    return abClean(
      el.getAttribute('title')
        || el.getAttribute('placeholder')
        || el.getAttribute('name')
        || el.id
        || (tag === 'a' ? el.getAttribute('href') : '')
    );
  }

  function abRole(el) {
    const explicit = abCap(abClean(el.getAttribute('role')).split(' ')[0], AB_ATTR_MAX);
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    if (tag === 'input') {
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return 'searchbox';
      if (type === 'file') return 'file';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return tag;
  }

  function abChecked(el) {
    const aria = el.getAttribute('aria-checked') || el.getAttribute('aria-pressed') || el.getAttribute('aria-selected');
    if (aria === 'true' || aria === 'mixed') return aria === 'true' ? true : 'mixed';
    if (aria === 'false') return false;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName.toLowerCase() === 'input' && (type === 'checkbox' || type === 'radio')) return Boolean(el.checked);
    return null;
  }

  function abActiveElement() {
    const el = document.activeElement;
    if (!el) return null;
    return {
      tag: el.tagName.toLowerCase(),
      element_id: elementIds ? elementIds.idOf(el) : null,
      name: abCap(el.getAttribute('name'), AB_ATTR_MAX),
      id: abCap(el.id || null, AB_ATTR_MAX),
      label: abSlice(abName(el), 120)
    };
  }
"""
)


def _with_element_naming(source: str) -> str:
    return source.replace("/*__ELEMENT_NAMING__*/", _ELEMENT_NAMING_JS)


INTERACTABLES_SCRIPT = _with_element_naming(
    r"""
(limit, elementIds) => {
/*__ELEMENT_NAMING__*/
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  const selector = [
    'a',
    'button',
    'input',
    'textarea',
    'select',
    'summary',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[role="searchbox"]',
    '[role="combobox"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[role="option"]',
    '[role="slider"]',
    '[contenteditable="true"]',
    '[tabindex]'
  ].join(',');

  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    // Nothing is written into the page: the id lives in the registry. The
    // hint keeps its old form, and the controller reads it as this element_id
    // when it comes back as a selector; no element carries that attribute.
    const elementId = elementIds ? elementIds.issue(el) : null;
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: elementId,
      selector_hint: elementId ? `[data-operator-id="${elementId}"]` : null,
      tag: el.tagName.toLowerCase(),
      type: abCap(el.getAttribute('type'), AB_ATTR_MAX),
      role: abRole(el),
      label: abSlice(abName(el), 160),
      checked: abChecked(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      href: abCapUrl(el.href || null),
      bbox: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""
)

ACTIVE_ELEMENT_SCRIPT = _with_element_naming(
    r"""
(_unused, elementIds) => {
/*__ELEMENT_NAMING__*/
  return abActiveElement();
}
"""
)

# Page text as a model should read it: line breaks kept, and innerText's tab
# between table cells, with runs of spaces and blank lines collapsed. Squashing
# every run of whitespace to one space ran a table's cells, a list's items and
# separate paragraphs together into one line. Shared by the observation's
# text_excerpt and browser.get_html(text_only=true) so both read the same.
_READABLE_TEXT_JS = r"""
  const readable = (value) =>
    String(value || '')
      .replace(/\r\n?/g, '\n')
      .replace(/[^\S\n\t]+/g, ' ')
      .replace(/ *([\n\t]) */g, '$1')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
"""

# Run in the controller's isolated world (element_ids.evaluate), as its caller's other observation scripts are.
PAGE_TEXT_SCRIPT = (
    "(_unused, _elementIds) => {\n"
    + _READABLE_TEXT_JS
    + "  return readable(document.body ? document.body.innerText : '');\n}"
)

PAGE_SUMMARY_SCRIPT = _with_element_naming(
    r"""
(textLimit, elementIds) => {
/*__ELEMENT_NAMING__*/
  const squash = (value, maxLength = textLimit) =>
    abSlice(String(value || '').replace(/\s+/g, ' ').trim(), maxLength);

/*__READABLE_TEXT__*/

  const headings = Array.from(document.querySelectorAll('h1,h2,h3'))
    .slice(0, 8)
    .map((el) => ({
      level: el.tagName.toLowerCase(),
      text: squash(el.innerText, 160)
    }))
    .filter((item) => item.text);

  const forms = Array.from(document.forms)
    .slice(0, 3)
    .map((form) => ({
      action: abCapUrl(form.getAttribute('action') || null),
      method: abCap((form.getAttribute('method') || 'get').toLowerCase(), AB_ATTR_MAX),
      fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
        .slice(0, 8)
        .map((field) => ({
          tag: field.tagName.toLowerCase(),
          type: abCap(field.getAttribute('type') || null, AB_ATTR_MAX),
          name: abCap(field.getAttribute('name') || null, AB_ATTR_MAX),
          label: abSlice(abName(field), 80),
          disabled: Boolean(field.disabled || field.getAttribute('aria-disabled') === 'true')
        }))
    }));

  // The title and focused element ride along so an observation costs one
  // evaluate round trip instead of three (page.title() reads document.title).
  return {
    title: abCap(document.title, AB_TITLE_MAX),
    active_element: abActiveElement(),
    text_excerpt: abSlice(readable(document.body?.innerText), textLimit),
    dom_outline: {
      headings,
      forms,
      counts: {
        links: document.querySelectorAll('a').length,
        buttons: document.querySelectorAll('button, [role="button"]').length,
        inputs: document.querySelectorAll('input, textarea, select').length,
        forms: document.forms.length
      }
    }
  };
}
"""
).replace("/*__READABLE_TEXT__*/", _READABLE_TEXT_JS)

# Feed/profile extraction helpers
EXTRACT_POSTS_SCRIPT = with_ab_cap(
    r"""
(limit) => {
/*__AB_CAP__*/
  const candidates = [];
  const seen = new Set();
  const els = document.querySelectorAll(
    'article, [role="article"], [data-testid*="tweet"], ' +
    '[data-testid*="post"], [class*="post-content"], ' +
    '[class*="feed-item"], [class*="timeline-item"]'
  );
  for (const el of els) {
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (text.length < 20 || seen.has(text.slice(0, 100))) continue;
    seen.add(text.slice(0, 100));
    const rect = el.getBoundingClientRect();
    if (rect.width === 0) continue;
    const links = [...el.querySelectorAll('a[href]')]
      .map(a => a.href).filter(h => h && !h.startsWith('javascript')).map(h => abCapUrl(h));
    const imgs = [...el.querySelectorAll('img[src]')].map(i => abCapUrl(i.src)).slice(0, 3);
    candidates.push({
      text: text.slice(0, 600),
      links: links.slice(0, 5),
      images: imgs,
      tag: el.tagName.toLowerCase(),
      y_position: Math.round(rect.top + window.scrollY),
    });
    if (candidates.length >= limit) break;
  }
  return candidates;
}
"""
)

EXTRACT_PROFILE_SCRIPT = with_ab_cap(
    r"""
() => {
/*__AB_CAP__*/
  function first(selectors) {
    for (const s of selectors) {
      const el = document.querySelector(s);
      const text = el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : null;
      if (text && text.length > 0) return text.slice(0, 300);
    }
    return null;
  }
  function findCount(keyword) {
    for (const el of document.querySelectorAll('a, span, div')) {
      const text = (el.innerText || '').toLowerCase();
      if (text.includes(keyword) && /[\d,]+/.test(text)) {
        const m = text.match(/([\d,.]+\s*[kmb]?)/i);
        return m ? m[0].trim() : null;
      }
    }
    return null;
  }
  return {
    page_title: abCap(document.title, AB_TITLE_MAX),
    url: abCapUrl(window.location.href),
    username: first(['[data-testid="UserName"]', '.username', 'h1', '[class*="username"]']),
    display_name: first(['[data-testid="UserName"] span', '[class*="display-name"]', '[class*="displayName"]']),
    bio: first(['[data-testid="UserDescription"]', '[class*="bio"]', '[class*="about"]', '[class*="description"]']),
    followers: findCount('follower'),
    following: findCount('following'),
    post_count: findCount('post') || findCount('tweet') || findCount('video'),
    avatar_url: abCapUrl(document.querySelector(
      'img[src*="profile_image"], img[src*="avatar"], img[class*="avatar"], img[class*="profile"]'
    )?.src || null),
  };
}
"""
)

# The fields both find_elements modes return for an element. A value, id or class that was cut says so
# (`<field>_truncated: true`), because a client reads a value back to check what it typed and builds selectors from id
# and class. `field` carries what the Python side needs to decide whether the value is a secret (see
# cap_found_elements); it never reaches the caller.
_FIND_FIELDS_JS = r"""
  function abFoundFields(el) {
    const out = {};
    const raw = {
      value: el.value || null,
      id: el.id || null,
      class: (typeof el.className === 'string' ? el.className : el.getAttribute('class')) || null,
    };
    for (const key of ['value', 'id', 'class']) {
      out[key] = key === 'class' ? abCapWords(raw[key], AB_ATTR_MAX) : abCap(raw[key], AB_ATTR_MAX);
      if (typeof raw[key] === 'string' && out[key] !== raw[key]) out[key + '_truncated'] = true;
    }
    out.href = abCapUrl(el.href || null);
    if (out.value) {
      out.field = {};
      for (const name of ['type', 'name', 'id', 'autocomplete', 'placeholder', 'aria-label']) {
        const attr = el.getAttribute(name) || '';
        out.field[name] = abCap(attr, AB_FIELD_ATTR_MAX);
        if (out.field[name] !== attr) out.field.truncated = true;
      }
    }
    return out;
  }
""".replace("AB_FIELD_ATTR_MAX", str(AB_FIELD_ATTR_MAX))


def _with_find_fields(source: str) -> str:
    return with_ab_cap(source).replace("/*__FIND_FIELDS__*/", _FIND_FIELDS_JS)


# find_elements: text search and selector modes. Each returns page-controlled strings, so each is capped. Both run in
# the controller's isolated world (element_ids.evaluate, NAV-12), which calls them as ``(arg, elementIds)``.
FIND_ELEMENTS_TEXT_SCRIPT = _with_find_fields(
    r"""([query, isRegex, context, limit], _elementIds) => {
/*__AB_CAP__*/
/*__FIND_FIELDS__*/
    let matcher = null;
    if (isRegex) {
        try {
            matcher = new RegExp(query, 'gi');
        } catch (e) {
            return {__invalid_regex: String((e && e.message) || e)};
        }
    }
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
    const results = [];
    let node;
    while ((node = walker.nextNode()) && results.length < limit) {
        const text = node.textContent || '';
        if (!text.trim()) continue;
        let matchIndex = -1;
        let matchText = '';
        if (isRegex) {
            matcher.lastIndex = 0;
            const m = matcher.exec(text);
            if (m) { matchIndex = m.index; matchText = m[0]; }
        } else {
            const idx = text.toLowerCase().indexOf(query.toLowerCase());
            if (idx !== -1) { matchIndex = idx; matchText = text.substr(idx, query.length); }
        }
        if (matchIndex === -1) continue;
        const el = node.parentElement;
        if (!el) continue;
        const r = el.getBoundingClientRect();
        // A regex can match a whole text node: the match is cut, and the context is `context` on each side of it.
        const shownMatch = abCap(matchText, AB_MATCH_MAX);
        // The window is cut by units: step off a low surrogate at its start and back off a high one at its end.
        let start = Math.max(0, matchIndex - context);
        if (text.charCodeAt(start) >= 0xDC00 && text.charCodeAt(start) <= 0xDFFF) start++;
        let end = Math.min(text.length, matchIndex + shownMatch.length + context);
        if (text.charCodeAt(end - 1) >= 0xD800 && text.charCodeAt(end - 1) <= 0xDBFF) end--;
        results.push({
            tag: el.tagName.toLowerCase(),
            text: abCap(text.trim(), AB_TEXT_MAX),
            match: shownMatch,
            context_text: text.substring(start, end).trim(),
            ...abFoundFields(el),
            visible: r.width > 0 && r.height > 0,
            x: Math.round(r.x), y: Math.round(r.y),
            width: Math.round(r.width), height: Math.round(r.height),
        });
    }
    return results;
}
"""
)

FIND_ELEMENTS_SELECTOR_SCRIPT = _with_find_fields(
    r"""([selector, limit], _elementIds) => {
/*__AB_CAP__*/
/*__FIND_FIELDS__*/
    let found;
    try {
        found = document.querySelectorAll(selector);
    } catch (e) {
        // Reported here, as the regex is: the world reports a script that threw as a stale element.
        return {__invalid_selector: String((e && e.message) || e)};
    }
    const els = [...found].slice(0, limit);
    return els.map(el => {
        const r = el.getBoundingClientRect();
        return {
            tag: el.tagName.toLowerCase(),
            text: abCap(el.innerText, AB_TEXT_MAX) || '',
            ...abFoundFields(el),
            visible: r.width > 0 && r.height > 0,
            x: Math.round(r.x), y: Math.round(r.y),
            width: Math.round(r.width), height: Math.round(r.height),
        };
    });
}
"""
)

# CDP element intelligence: the asset references of one element. ``__SELECTOR__`` is replaced by a JS string literal.
ELEMENT_ASSETS_SCRIPT = with_ab_cap(
    r"""
(function() {
/*__AB_CAP__*/
    const el = document.querySelector(__SELECTOR__);
    if (!el) return {};
    return {
        src: abCapUrl(el.src || null),
        href: abCapUrl(el.href || null),
        currentSrc: abCapUrl(el.currentSrc || null),
        backgroundImage: abCapUrl(window.getComputedStyle(el).backgroundImage)
    };
})()
"""
)

SMOOTH_SCROLL_SCRIPT = r"""
(deltaY) => {
  return new Promise((resolve) => {
    const direction = Math.sign(deltaY) || 1;
    const target = Math.abs(deltaY);
    const overshoot = target > 240 ? Math.round(target * (0.04 + Math.random() * 0.08)) : 0;
    let travelled = 0;
    let correcting = false;

    function nextDelay() {
      return 14 + Math.random() * 22;
    }

    function tick() {
      const remaining = correcting ? travelled : (target + overshoot - travelled);
      if (remaining <= 0) {
        if (!correcting && overshoot > 0) {
          correcting = true;
          travelled = overshoot;
          setTimeout(tick, nextDelay());
          return;
        }
        resolve();
        return;
      }

      const baseStep = 45 + Math.random() * 65;
      const amount = Math.min(remaining, baseStep);
      const signed = correcting ? (-direction * amount) : (direction * amount);
      window.scrollBy({ top: signed, behavior: 'auto' });
      travelled += amount;
      setTimeout(tick, nextDelay());
    }
    tick();
  });
}
"""

# Social interaction scripts — find and click engagement buttons
FIND_LIKE_BUTTON_SCRIPT = r"""
(postIndex) => {
  const likeSelectors = [
    '[data-testid="like"]',
    '[aria-label*="like" i]:not([aria-pressed="true"])',
    '[aria-label*="heart" i]:not([aria-pressed="true"])',
    '[aria-label*="love" i]:not([aria-pressed="true"])',
    'button[class*="like"]:not([class*="liked"])',
    'button[class*="heart"]',
    '[role="button"][class*="like"]',
  ];
  const allButtons = [];
  for (const sel of likeSelectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        allButtons.push({
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
        });
      }
    }
  }
  const target = allButtons[postIndex] || allButtons[0] || null;
  return target;
}
"""

FIND_FOLLOW_BUTTON_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="followButton"]',
    '[aria-label*="follow" i]:not([aria-label*="unfollow" i])',
    'button:not([class*="unfollow"]):not([class*="following"])',
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
      if (!text.includes('follow')) continue;
      if (text.includes('unfollow') || text.includes('following')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        return {
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80),
        };
      }
    }
  }
  return null;
}
"""

FIND_UNFOLLOW_BUTTON_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="unfollow"]',
    '[aria-label*="following" i]',
    '[aria-label*="unfollow" i]',
    'button[class*="following"]',
    'button[class*="unfollow"]',
  ];
  for (const sel of selectors) {
    const els = [...document.querySelectorAll(sel)];
    for (const el of els) {
      const text = (el.innerText || el.getAttribute('aria-label') || '').toLowerCase();
      if (!text.includes('following') && !text.includes('unfollow')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        return {
          selector: sel,
          x: Math.round(rect.x + rect.width / 2),
          y: Math.round(rect.y + rect.height / 2),
          label: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80),
        };
      }
    }
  }
  return null;
}
"""

FIND_REPLY_BUTTON_SCRIPT = r"""
(postIndex) => {
  const selectors = [
    '[data-testid="reply"]',
    '[data-testid="comment"]',
    '[aria-label*="reply" i]',
    '[aria-label*="comment" i]',
    'button[class*="reply"]',
    'button[class*="comment"]',
  ];
  const buttons = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      buttons.push({
        selector: sel,
        x: Math.round(rect.x + rect.width / 2),
        y: Math.round(rect.y + rect.height / 2),
        label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
      });
    }
  }
  return buttons[postIndex] || buttons[0] || null;
}
"""

FIND_REPOST_BUTTON_SCRIPT = r"""
(postIndex) => {
  const selectors = [
    '[data-testid="retweet"]',
    '[data-testid="repost"]',
    '[aria-label*="retweet" i]',
    '[aria-label*="repost" i]',
    '[aria-label*="share post" i]',
    'button[class*="retweet"]',
    'button[class*="repost"]',
  ];
  const buttons = [];
  for (const sel of selectors) {
    for (const el of document.querySelectorAll(sel)) {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      buttons.push({
        selector: sel,
        x: Math.round(rect.x + rect.width / 2),
        y: Math.round(rect.y + rect.height / 2),
        label: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 80),
      });
    }
  }
  return buttons[postIndex] || buttons[0] || null;
}
"""

FIND_SEARCH_INPUT_SCRIPT = r"""
() => {
  const selectors = [
    '[data-testid="SearchBox_Search_Input"]',
    'input[type="search"]',
    'input[name="q"]',
    '[aria-label*="search" i]',
    '[placeholder*="search" i]',
    'input[role="searchbox"]',
    '[role="searchbox"]',
    'input[type="text"][name*="search"]',
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) {
      const rect = el.getBoundingClientRect();
      if (rect.width > 0) return sel;
    }
  }
  return null;
}
"""

EXTRACT_COMMENTS_SCRIPT = r"""
(limit) => {
  const out = [];
  const seen = new Set();
  const selectors = [
    'article',
    '[role="article"]',
    '[data-testid*="reply"]',
    '[data-testid*="comment"]',
    '[class*="comment"]',
    '[class*="reply"]',
    'li'
  ];
  for (const el of document.querySelectorAll(selectors.join(','))) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (text.length < 12) continue;
    const signature = text.slice(0, 120);
    if (seen.has(signature)) continue;
    seen.add(signature);
    const author = el.querySelector('[data-testid="User-Name"], [class*="author"], strong, h3, h4')?.textContent || null;
    out.push({
      author: author ? author.replace(/\s+/g, ' ').trim().slice(0, 120) : null,
      text: text.slice(0, 800),
      y_position: Math.round(rect.top + window.scrollY),
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""


async def apply_stealth(target: object) -> None:
    """Inject the stealth init script into a page or context before any navigation.

    Pass the context to cover every page it opens, popups included.
    """
    add_init_script = getattr(target, "add_init_script", None)
    if callable(add_init_script):
        await add_init_script(STEALTH_INIT_SCRIPT)
