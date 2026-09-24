"""Browser-side JavaScript constants used by BrowserManager for page observation."""

from __future__ import annotations

# Injected before every page load via add_init_script().
# Removes automation signals, mocks realistic browser properties.
STEALTH_INIT_SCRIPT = r"""
() => {
  // Remove webdriver flag
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => undefined, configurable: true,
    });
  } catch (_) {}

  // Chrome runtime object (many sites check for this)
  if (!window.chrome) {
    window.chrome = {
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
  }

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
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {
      const ctx = this.getContext('2d');
      if (ctx && this.width > 0 && this.height > 0) {
        try {
          const img = ctx.getImageData(0, 0, 1, 1);
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
}
"""

# Shared by the observation scripts below, which inject it at the top of their
# function body (``/*__ELEMENT_NAMING__*/``) so all three name elements the same
# way. It follows the order of the accessible-name computation that matters in
# practice: aria-labelledby, aria-label, associated <label>s, button captions and
# image alt text, visible text, title, then placeholder, name and id.
#
# A field's *value* is never used as its name. The previous fallback chain did,
# so a password typed into a field with no placeholder became that field's
# "label" in every later observation — in the interactables, the active element
# and the form outline — and from there reached model prompts, MCP clients and
# logs, even though the type action itself had redacted the text. Captions of
# submit/button/reset inputs are the one exception: that value is visible text.
_ELEMENT_NAMING_JS = r"""
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
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
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
    const explicit = abClean(el.getAttribute('role')).split(' ')[0];
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
"""


def _with_element_naming(source: str) -> str:
    return source.replace("/*__ELEMENT_NAMING__*/", _ELEMENT_NAMING_JS)


INTERACTABLES_SCRIPT = _with_element_naming(
    r"""
(limit) => {
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
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    out.push({
      element_id: el.dataset.operatorId,
      selector_hint: `[data-operator-id="${el.dataset.operatorId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: abRole(el),
      label: abName(el).slice(0, 160),
      checked: abChecked(el),
      disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      href: el.href || null,
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
() => {
/*__ELEMENT_NAMING__*/
  const el = document.activeElement;
  if (!el) return null;
  return {
    tag: el.tagName.toLowerCase(),
    element_id: el.dataset?.operatorId || null,
    name: el.getAttribute('name'),
    id: el.id || null,
    label: abName(el).slice(0, 120)
  };
}
"""
)

PAGE_SUMMARY_SCRIPT = _with_element_naming(
    r"""
(textLimit) => {
/*__ELEMENT_NAMING__*/
  const squash = (value, maxLength = textLimit) =>
    String(value || '').replace(/\s+/g, ' ').trim().slice(0, maxLength);

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
      action: form.getAttribute('action') || null,
      method: (form.getAttribute('method') || 'get').toLowerCase(),
      fields: Array.from(form.querySelectorAll('input, textarea, select, button'))
        .slice(0, 8)
        .map((field) => ({
          tag: field.tagName.toLowerCase(),
          type: field.getAttribute('type') || null,
          name: field.getAttribute('name') || null,
          label: abName(field).slice(0, 80),
          disabled: Boolean(field.disabled || field.getAttribute('aria-disabled') === 'true')
        }))
    }));

  return {
    text_excerpt: squash(document.body?.innerText || '', textLimit),
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
)

# Feed/profile extraction helpers
EXTRACT_POSTS_SCRIPT = r"""
(limit) => {
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
      .map(a => a.href).filter(h => h && !h.startsWith('javascript'));
    const imgs = [...el.querySelectorAll('img[src]')].map(i => i.src).slice(0, 3);
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

EXTRACT_PROFILE_SCRIPT = r"""
() => {
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
    page_title: document.title,
    url: window.location.href,
    username: first(['[data-testid="UserName"]', '.username', 'h1', '[class*="username"]']),
    display_name: first(['[data-testid="UserName"] span', '[class*="display-name"]', '[class*="displayName"]']),
    bio: first(['[data-testid="UserDescription"]', '[class*="bio"]', '[class*="about"]', '[class*="description"]']),
    followers: findCount('follower'),
    following: findCount('following'),
    post_count: findCount('post') || findCount('tweet') || findCount('video'),
    avatar_url: document.querySelector(
      'img[src*="profile_image"], img[src*="avatar"], img[class*="avatar"], img[class*="profile"]'
    )?.src || null,
  };
}
"""

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

FIND_ACCESSIBLE_TARGET_SCRIPT = r"""
(options) => {
  const keywords = (options?.keywords || []).map((item) => String(item).toLowerCase());
  const excludeKeywords = (options?.excludeKeywords || []).map((item) => String(item).toLowerCase());
  const index = Number.isInteger(options?.index) ? options.index : 0;
  const requireTextbox = Boolean(options?.requireTextbox);
  const selector = options?.selector || [
    'button',
    'a',
    'input',
    'textarea',
    'select',
    '[role="button"]',
    '[role="link"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
  ].join(',');

  const isVisible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };

  const isTextbox = (el) => {
    const role = (el.getAttribute('role') || '').toLowerCase();
    return (
      el.isContentEditable ||
      el.getAttribute('contenteditable') === 'true' ||
      el.tagName === 'TEXTAREA' ||
      (el.tagName === 'INPUT' && !['checkbox', 'radio', 'submit', 'button'].includes((el.type || '').toLowerCase())) ||
      role === 'textbox'
    );
  };

  const describe = (el) => {
    return [
      el.getAttribute('aria-label'),
      el.getAttribute('title'),
      el.getAttribute('placeholder'),
      el.getAttribute('name'),
      el.getAttribute('id'),
      el.innerText,
      el.textContent,
      el.value,
    ]
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
  };

  const matches = [];
  for (const el of document.querySelectorAll(selector)) {
    if (!isVisible(el) || el.closest('[aria-hidden="true"]')) continue;
    if (requireTextbox && !isTextbox(el)) continue;
    const description = describe(el);
    if (keywords.length && !keywords.some((keyword) => description.includes(keyword))) continue;
    if (excludeKeywords.some((keyword) => description.includes(keyword))) continue;
    if (!el.dataset.operatorId) {
      el.dataset.operatorId = `op-${Math.random().toString(36).slice(2, 10)}`;
    }
    const rect = el.getBoundingClientRect();
    matches.push({
      selector: `[data-operator-id="${el.dataset.operatorId}"]`,
      element_id: el.dataset.operatorId,
      x: Math.round(rect.x + rect.width / 2),
      y: Math.round(rect.y + rect.height / 2),
      label: description.slice(0, 160),
      tag: el.tagName.toLowerCase(),
    });
  }
  return matches[index] || matches[0] || null;
}
"""


async def apply_stealth(page: object) -> None:
    """Inject stealth init script into a page before any navigation."""
    add_init_script = getattr(page, "add_init_script", None)
    if callable(add_init_script):
        await add_init_script(STEALTH_INIT_SCRIPT)
