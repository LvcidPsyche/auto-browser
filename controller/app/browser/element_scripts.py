"""The JavaScript an action runs on the element it names.

By element_id a function here is sent through ``element_world.element_declaration``
(or, for ``UPLOAD_UNMARK_SCRIPT``, as a world function of its own), so it runs in
the page's isolated world, which the page's scripts cannot reach: nothing it calls
can be replaced by them. By selector, ELEMENT_HAS_FOCUS_SCRIPT,
ELEMENT_FOCUS_PLACE_SCRIPT, the FIELD_HOLD_* scripts and NAMED_FIELDS_SCRIPT run on
a locator in the page's own world, and FIELD_ATTRIBUTES_SCRIPT runs only there (the
TOTP autofill's read of a candidate field); so do the scripts of click_target.py and
actions.py that start with DOM_JS. There the page answers for what they read off a
node or a document, but for what DOM_JS reads through the prototypes (its comment).
"""

from __future__ import annotations

from .element_world import ELEMENT_REGISTRY_SCRIPT

MARKER_ATTRIBUTE = "data-auto-browser-target"

# DOM reads through the prototypes' own getters and methods, taken as the script
# sets itself up. A form's named controls shadow the form's own properties, in
# this world too: in <form><input name="parentNode"></form>, form.parentNode is
# that input, so a walk up the tree that read it could be steered onto the
# element it was looking for, or sent round the form and its control for good.
# Defines `dom`. Also run in the page's own world (a selector target's focus
# check, hold and approval read, and the scripts of click_target.py and
# actions.py that start with it), where more stands in for a property: a
# document's named elements (with <img name="activeElement"> on the page,
# document.activeElement is that img there, and not here), and what a page
# defines on a node of its own (a shadow root's `host`, a label's `control`).
# The getters answer for neither. A page that replaced a prototype's getter
# before the script ran is not answered for: the getter taken then is the page's.
DOM_JS = """const dom = (() => {
  const getter = (proto, name) => {
    const descriptor = Object.getOwnPropertyDescriptor(proto, name);
    if (descriptor && typeof descriptor.get === 'function') return descriptor.get;
    return function () { return this[name]; };
  };
  const method = (proto, name) => {
    const fn = proto[name];
    if (typeof fn === 'function') return (el, ...args) => fn.apply(el, args);
    return (el, ...args) => el[name](...args);
  };
  const parentNode = getter(Node.prototype, 'parentNode');
  const shadowRoot = getter(Element.prototype, 'shadowRoot');
  const tagName = getter(Element.prototype, 'tagName');
  const isConnected = getter(Node.prototype, 'isConnected');
  const ownerDocument = getter(Node.prototype, 'ownerDocument');
  const nodeType = getter(Node.prototype, 'nodeType');
  const documentActive = getter(Document.prototype, 'activeElement');
  const shadowActive = getter(ShadowRoot.prototype, 'activeElement');
  const host = getter(ShadowRoot.prototype, 'host');
  const elementSlot = getter(Element.prototype, 'assignedSlot');
  const textSlot = getter(Text.prototype, 'assignedSlot');
  const control = getter(HTMLLabelElement.prototype, 'control');
  const editable = getter(HTMLElement.prototype, 'isContentEditable');
  const elementAll = method(Element.prototype, 'querySelectorAll');
  const fragmentAll = method(DocumentFragment.prototype, 'querySelectorAll');
  const documentAt = method(Document.prototype, 'elementFromPoint');
  const shadowAt = method(ShadowRoot.prototype, 'elementFromPoint');
  // What holds a document of its own, or is a plugin, whose inside this document cannot see: keys sent to one go
  // there. A portal is listed though the Chromium measured (156) has no such element: one so named is then the page's.
  const FRAME_TAGS = ['IFRAME', 'FRAME', 'OBJECT', 'EMBED', 'FENCEDFRAME', 'PORTAL'];
  const dom = {
    frame: (el) => FRAME_TAGS.includes(dom.tag(el)),
    // The element above: the parent element, or the host of the shadow root the element is in.
    up(el) {
      const parent = parentNode.call(el);
      if (!parent) return null;
      const type = nodeType.call(parent);
      if (type === 11) return dom.host(parent);
      return type === 1 ? parent : null;
    },
    // The node above in the tree as it is shown: the slot an element or a text is assigned to, else its parent; a
    // shadow root's host; null above a document, or above a fragment that is no shadow root.
    flatUp(node) {
      if (nodeType.call(node) === 11) return dom.host(node);
      return dom.slot(node) || parentNode.call(node);
    },
    // The slot an element or a text is assigned to: null for one in none, and for any other node.
    slot(node) {
      const type = nodeType.call(node);
      if (type === 1) return elementSlot.call(node);
      if (type === 3) return textSlot.call(node);
      return null;
    },
    // A shadow root's host: null for any other node, for which the prototype's getter throws (see active()).
    host(root) {
      try {
        return host.call(root);
      } catch (error) {
        return null;
      }
    },
    type: (node) => nodeType.call(node),
    // The control a label stands for: null for one that stands for none, and for what is no label.
    control(label) {
      try {
        return control.call(label);
      } catch (error) {
        return null;
      }
    },
    // Whether the element is editable (contenteditable, its own or what it is in): false for what is no HTML element.
    editable(el) {
      try {
        return editable.call(el) === true;
      } catch (error) {
        return false;
      }
    },
    // The elements under an element or a shadow root that match `css`.
    all: (root, css) => (nodeType.call(root) === 11 ? fragmentAll : elementAll)(root, css),
    listen: method(EventTarget.prototype, 'addEventListener'),
    unlisten: method(EventTarget.prototype, 'removeEventListener'),
    // Whether `node` is `el` or inside it, through shadow roots.
    within(node, el) {
      for (let at = node; at; at = dom.up(at)) if (at === el) return true;
      return false;
    },
    root: method(Node.prototype, 'getRootNode'),
    // The focused element of a root, a document or a shadow root: null in the root of a tree that is in neither (a
    // fragment, or an element the page took out of its tree). Whether a root is a shadow root is not read off the root:
    // a page can give its own open shadow root a `host` (as it can an activeElement), which read as a property made the
    // walk stop at the component. The prototype's getter is asked instead, and it throws for a fragment that is no
    // shadow root, which has no focused element.
    active(root) {
      const type = nodeType.call(root);
      if (type === 9) return documentActive.call(root);
      if (type !== 11) return null;
      try {
        return shadowActive.call(root);
      } catch (error) {
        return null;
      }
    },
    shadowRoot: (el) => shadowRoot.call(el),
    tag: (el) => tagName.call(el).toUpperCase(),
    // Attached, and to this document: one a same-origin frame adopted has the frame's box.
    here: (node) => isConnected.call(node) && ownerDocument.call(node) === document,
    attr: method(Element.prototype, 'getAttribute'),
    has: method(Element.prototype, 'hasAttribute'),
    unset: method(Element.prototype, 'removeAttribute'),
    matches: method(Element.prototype, 'matches'),
    closest: method(Element.prototype, 'closest'),
    rect: method(Element.prototype, 'getBoundingClientRect'),
    // The element at (x, y) of a document or a shadow root, by the prototype's method: read off the document, an
    // element the page named elementFromPoint stood in for it.
    at: (root, x, y) => (nodeType.call(root) === 9 ? documentAt : shadowAt)(root, x, y),
  };
  return dom;
})();"""

# What covers the element at (x, y): null when the pointer hits the element or
# something inside it, through open shadow roots, else what it hits instead. A
# label for the element is a cover (a hidden "Pay now" holding a label for
# "Cancel" over Cancel would take the click), and so is an embedded frame, even
# inside the element: what is in a frame is never what observe described.
# Every page-chosen part of the description is bounded. A frame behind a closed
# shadow root is not seen: the hit stays on the root's host.
_COVER_AT_JS = r"""(el, x, y) => {
  let hit = document.elementFromPoint(x, y);
  for (;;) {
    const root = hit && dom.shadowRoot(hit);
    const inner = root && root.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return { tag: null };
  const frame = dom.frame(hit);
  const inside = dom.within(hit, el);
  if (!frame && inside) return null;
  const word = (text) => (text || '').trim().split(/\s+/)[0].slice(0, 40).replace(/[^\w:.-]/g, '_');
  const label = dom.closest(hit, 'label');
  const labelsIt = !!label && label.control === el;
  const target = labelsIt ? label.htmlFor : '';
  return {
    tag: word(dom.tag(hit).toLowerCase()),
    id: word(dom.attr(hit, 'id')),
    cls: word(dom.attr(hit, 'class')),
    frame,
    inside,
    label: labelsIt,
    inLabel: labelsIt && label !== hit,
    labelSelector: target && target.length <= 100 ? 'label[for="' + CSS.escape(target) + '"]' : null,
  };
}"""

# Playwright's 'follow-label' retargeting (1.62, injectedScript.ts retarget): an
# element that is not a control itself goes to the button-like element it is in,
# and then, when that is not a control either, to the control of the label it is
# in. Its enabled check, its file transfer and its selection act on that control.
# Needs `dom`.
_FOLLOW_LABEL_JS = """(el) => {
    let target = el;
    if (!dom.matches(target, 'input, textarea, select') && !target.isContentEditable) {
      target = dom.closest(target, 'button, [role=button], [role=checkbox], [role=radio]') || target;
    }
    const control = 'a, input, textarea, button, select, [role=link], [role=button], [role=checkbox], [role=radio]';
    if (!dom.matches(target, control) && !target.isContentEditable) {
      const label = dom.closest(target, 'label');
      if (label && label.control) target = label.control;
    }
    return target;
  }"""

# Playwright's enabled check (1.62: elementState 'enabled'), on the element it
# retargets to ('follow-label'): getAriaDisabled, that is natively disabled (a
# BUTTON, INPUT, SELECT, TEXTAREA, OPTION or OPTGROUP with the attribute, in a
# disabled optgroup, or in a disabled fieldset outside its first legend), or
# aria-disabled="true" on it or the nearest ancestor that says either way, for
# an element whose ARIA role takes aria-disabled. Ported from its injected
# script (roleUtils.ts, injectedScript.ts). Of the implicit roles, only those
# that take aria-disabled are listed: every other tag's role answers the same
# as none here, so the role it would have does not change the answer.
_IS_DISABLED_JS = (
    """(() => {
  const ARIA_DISABLED_ROLES = ['application', 'button', 'composite', 'gridcell', 'group', 'input', 'link', 'menuitem',
    'scrollbar', 'separator', 'tab', 'checkbox', 'columnheader', 'combobox', 'grid', 'listbox', 'menu', 'menubar',
    'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'radiogroup', 'row', 'rowheader', 'searchbox', 'select',
    'slider', 'spinbutton', 'switch', 'tablist', 'textbox', 'toolbar', 'tree', 'treegrid', 'treeitem'];
  const VALID_ROLES = ['alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote', 'button', 'caption',
    'cell', 'checkbox', 'code', 'columnheader', 'combobox', 'complementary', 'contentinfo', 'definition', 'deletion',
    'dialog', 'directory', 'document', 'emphasis', 'feed', 'figure', 'form', 'generic', 'grid', 'gridcell', 'group',
    'heading', 'img', 'insertion', 'link', 'list', 'listbox', 'listitem', 'log', 'main', 'mark', 'marquee', 'math',
    'meter', 'menu', 'menubar', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'navigation', 'none', 'note',
    'option', 'paragraph', 'presentation', 'progressbar', 'radio', 'radiogroup', 'region', 'row', 'rowgroup',
    'rowheader', 'scrollbar', 'search', 'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong',
    'subscript', 'superscript', 'switch', 'tab', 'table', 'tablist', 'tabpanel', 'term', 'textbox', 'time', 'timer',
    'toolbar', 'tooltip', 'tree', 'treegrid', 'treeitem'];
  const NAMING_PROHIBITED = ['caption', 'code', 'deletion', 'emphasis', 'generic', 'insertion', 'paragraph',
    'presentation', 'strong', 'subscript', 'superscript'];
  const GLOBAL_ARIA = [['aria-atomic'], ['aria-busy'], ['aria-controls'], ['aria-current'], ['aria-describedby'],
    ['aria-details'], ['aria-dropeffect'], ['aria-flowto'], ['aria-grabbed'], ['aria-hidden'], ['aria-keyshortcuts'],
    ['aria-label', NAMING_PROHIBITED], ['aria-labelledby', NAMING_PROHIBITED], ['aria-live'], ['aria-owns'],
    ['aria-relevant'], ['aria-roledescription', ['generic']]];
  const PRESENTATION_PARENTS = { DD: ['DL', 'DIV'], DIV: ['DL'], DT: ['DL', 'DIV'], LI: ['OL', 'UL'],
    TBODY: ['TABLE'], TD: ['TR'], TFOOT: ['TABLE'], TH: ['TR'], THEAD: ['TABLE'], TR: ['THEAD', 'TBODY', 'TFOOT', 'TABLE'] };
  const INPUT_ROLES = { button: 'button', checkbox: 'checkbox', image: 'button', number: 'spinbutton', radio: 'radio',
    range: 'slider', reset: 'button', submit: 'button' };
  const closestAcrossShadows = (el, css) => {
    for (let at = el; at; at = dom.host(dom.root(at))) {
      const found = dom.closest(at, css);
      if (found) return found;
    }
    return null;
  };
  const inDisabledFieldset = (el) => {
    const fieldset = dom.closest(el, 'FIELDSET[DISABLED]');
    if (!fieldset) return false;
    const legend = fieldset.querySelector(':scope > LEGEND');
    return !legend || !legend.contains(el);
  };
  const nativelyDisabled = (el) =>
    ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'OPTGROUP'].includes(dom.tag(el)) &&
    (dom.has(el, 'disabled') || (dom.tag(el) === 'OPTION' && !!dom.closest(el, 'OPTGROUP[DISABLED]')) ||
      inDisabledFieldset(el));
  const nativelyFocusable = (el) => {
    const tag = dom.tag(el);
    if (['BUTTON', 'DETAILS', 'SELECT', 'TEXTAREA'].includes(tag)) return true;
    if (tag === 'A' || tag === 'AREA') return dom.has(el, 'href');
    if (tag === 'INPUT') return !el.hidden;
    return false;
  };
  const hasTabIndex = (el) => !Number.isNaN(Number(String(dom.attr(el, 'tabindex'))));
  const focusable = (el) => !nativelyDisabled(el) && (nativelyFocusable(el) || hasTabIndex(el));
  const hasGlobalAria = (el, role) =>
    GLOBAL_ARIA.some(([name, prohibited]) => !(prohibited && prohibited.includes(role || '')) && dom.has(el, name));
  const conflictResolved = (el, role) => hasGlobalAria(el, role) || focusable(el);
  const explicitRole = (el) =>
    (dom.attr(el, 'role') || '').split(' ').map((role) => role.trim()).find((role) => VALID_ROLES.includes(role)) || null;
  const headerCell = (el) => !!el && dom.tag(el) === 'TH';
  const dataCell = (el) => !!el && dom.tag(el) === 'TD' && !!((el.textContent || '').trim() || el.children.length > 0);
  const link = (el) => (dom.has(el, 'href') ? 'link' : null);
  const IMPLICIT_ROLES = {
    A: link,
    AREA: link,
    BUTTON: () => 'button',
    DATALIST: () => 'listbox',
    DETAILS: () => 'group',
    FIELDSET: () => 'group',
    HR: () => 'separator',
    INPUT: (el) => {
      const type = el.type.toLowerCase();
      // Playwright answers combobox for one with a datalist: both take aria-disabled.
      if (['email', 'search', 'tel', 'text', 'url', ''].includes(type)) return type === 'search' ? 'searchbox' : 'textbox';
      if (type === 'hidden') return null;
      if (type === 'file') return 'button';
      return INPUT_ROLES[type] || 'textbox';
    },
    OPTGROUP: () => 'group',
    OPTION: () => 'option',
    SELECT: (el) => (dom.has(el, 'multiple') || el.size > 1 ? 'listbox' : 'combobox'),
    TD: (el) => {
      const table = closestAcrossShadows(el, 'table');
      const role = table ? explicitRole(table) : '';
      return role === 'grid' || role === 'treegrid' ? 'gridcell' : 'cell';
    },
    TEXTAREA: () => 'textbox',
    TH: (el) => {
      const scope = dom.attr(el, 'scope');
      if (scope === 'col' || scope === 'colgroup') return 'columnheader';
      if (scope === 'row' || scope === 'rowgroup') return 'rowheader';
      const next = el.nextElementSibling;
      const previous = el.previousElementSibling;
      const parent = el.parentElement;
      const row = parent && dom.tag(parent) === 'TR' ? parent : null;
      if (!next && !previous) {
        const table = row && closestAcrossShadows(row, 'table');
        return table && table.rows.length <= 1 ? null : 'columnheader';
      }
      if (headerCell(next) && headerCell(previous)) return 'columnheader';
      if (dataCell(next) || dataCell(previous)) return 'rowheader';
      return 'columnheader';
    },
    TR: () => 'row',
  };
  const implicitRole = (el) => {
    const implied = IMPLICIT_ROLES[dom.tag(el)];
    const role = implied ? implied(el) : null;
    if (!role) return null;
    for (let at = el; ;) {
      const parent = dom.up(at);
      const parents = PRESENTATION_PARENTS[dom.tag(at)];
      if (!parents || !parent || !parents.includes(dom.tag(parent))) break;
      const parentRole = explicitRole(parent);
      if ((parentRole === 'none' || parentRole === 'presentation') && !conflictResolved(parent, parentRole)) {
        return parentRole;
      }
      at = parent;
    }
    return role;
  };
  const ariaRole = (el) => {
    const explicit = explicitRole(el);
    if (!explicit) return implicitRole(el);
    if (explicit === 'none' || explicit === 'presentation') {
      const implicit = implicitRole(el);
      if (conflictResolved(el, implicit)) return implicit;
    }
    return explicit;
  };
  const ariaDisabledInChain = (el) => {
    for (let at = el; at; at = dom.up(at)) {
      const value = (dom.attr(at, 'aria-disabled') || '').toLowerCase();
      if (value === 'true') return true;
      if (value === 'false') return false;
    }
    return false;
  };
  const followLabel = """
    + _FOLLOW_LABEL_JS
    + """;
  // An option is checked as itself: from an option in a select a label wraps, following the label reaches the select.
  return (el, follow = true) => {
    const target = follow ? followLabel(el) : el;
    return nativelyDisabled(target) || (ARIA_DISABLED_ROLES.includes(ariaRole(target) || '') && ariaDisabledInChain(target));
  };
})()"""
)

# The element's box while it is visible the way Playwright means it, else null.
_VISIBLE_BOX_JS = """(el) => {
  if (!el.isConnected || (el.checkVisibility && !el.checkVisibility())) return null;
  if (getComputedStyle(el).visibility !== 'visible') return null;
  const rect = el.getBoundingClientRect();
  if (!(rect.width > 0 && rect.height > 0)) return null;
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
}"""

# The part of the element the pointer goes to: the first of its client rects
# that has area inside the viewport, clipped to it, else null. Its centre is
# Playwright's clickable point (the middle of the first viewport-clipped content
# quad); the centre of the whole box can be off screen, or between the lines of
# a link that wraps.
_POINTER_RECT_JS = """(el) => {
  for (const rect of el.getClientRects()) {
    const left = Math.max(rect.left, 0);
    const top = Math.max(rect.top, 0);
    const right = Math.min(rect.right, innerWidth);
    const bottom = Math.min(rect.bottom, innerHeight);
    if (right > left && bottom > top && (right - left) * (bottom - top) > 0.99) {
      return { x: left, y: top, width: right - left, height: bottom - top };
    }
  }
  return null;
}"""

# The longest wait between two checks when no animation frame comes.
_FRAME_TIMER_MS = 50
# How long a wait goes without a single animation frame before two checks a
# timer apart may count as settled (see _SETTLING_JS).
_NO_FRAME_MS = 1000

# Whether an element's box has settled: the same box on two checks made in
# consecutive animation frames, as Playwright requires. next() waits for the next
# frame, or _FRAME_TIMER_MS where none comes. Only a check woken by a frame reads
# the box at that frame's animation time: CSS animations advance only when a
# frame is drawn, so on a renderer too slow to draw one in 50 ms a check woken by
# the timer reads the box of the frame before however the element moves, and a
# check made outside a frame (the first one, or a timer's) was seen to read the
# same box as the frame drawn 1.4 ms later on a moving element. A timer's check
# still checks the element (gone, visible, the deadline) and drops the box when
# it moved, but never finds it settled. Only when no frame at all has come for
# _NO_FRAME_MS of the wait (a page that is not being rendered, such as a hidden
# tab with animation frames paused) do two checks a timer apart count.
# forget() drops the recorded boxes.
_SETTLING_JS = f"""() => {{
  const started = performance.now();
  const same = (a, b) => a !== null && ['x', 'y', 'width', 'height'].every((key) => a[key] === b[key]);
  let frames = 0;
  let framed = false;
  let last = null;
  let quiet = null;
  return {{
    next: () => new Promise((resolve) => {{
      let woken = false;
      const wake = (byFrame) => {{
        if (woken) return;
        woken = true;
        framed = byFrame;
        resolve();
      }};
      requestAnimationFrame(() => {{
        frames += 1;
        wake(true);
      }});
      setTimeout(() => wake(false), {_FRAME_TIMER_MS});
    }}),
    settled(box) {{
      if (framed) {{
        quiet = null;
        if (same(last, box)) return true;
        last = box;
        return false;
      }}
      if (!same(last, box)) last = null;
      if (frames > 0) return false;
      if (same(quiet, box) && performance.now() - started >= {_NO_FRAME_MS}) return true;
      quiet = box;
      return false;
    }},
    forget() {{
      last = null;
      quiet = null;
    }},
  }};
}}"""

# Waits up to options.timeoutMs for the element to be attached, visible, stable
# (the same box in two consecutive animation frames: see _SETTLING_JS), enabled
# when options.enabled, inside the viewport, and, when options.pointer,
# uncovered at the centre of its pointer rect; scrolls it into view as needed.
# A cover makes it scroll again
# with the next alignment, as Playwright's retries do (a fixed header over the
# element), and wait for the box to settle; a cover that never moves (a label for
# the element, a frame inside it) is refused after one round of alignments.
# Answers { rect } (the pointer rect) or { refused: <code>, cover, early, scrolled }
# with the last reason it waited on.
ELEMENT_ACTIONABLE_SCRIPT = f"""async (el, options) => {{
  {DOM_JS}
  const coverAt = {_COVER_AT_JS};
  const isDisabled = {_IS_DISABLED_JS};
  const visibleBox = {_VISIBLE_BOX_JS};
  const pointerRect = {_POINTER_RECT_JS};
  const settling = ({_SETTLING_JS})();
  const alignments = ['end', 'center', 'start', 'nearest'];
  const deadline = performance.now() + options.timeoutMs;
  let rescrolls = 0;
  let cover = null;
  const check = () => {{
    if (!dom.here(el)) return 'element_gone';
    if (!visibleBox(el)) return 'element_not_visible';
    if (options.enabled && isDisabled(el)) return 'element_disabled';
    if (el.scrollIntoViewIfNeeded) el.scrollIntoViewIfNeeded(true);
    else el.scrollIntoView({{ block: 'center', inline: 'center' }});
    const box = visibleBox(el);
    if (!box) return 'element_not_visible';
    if (!settling.settled(box)) return 'element_not_stable';
    const rect = pointerRect(el);
    if (!rect) return 'element_not_in_view';
    if (options.pointer) {{
      cover = coverAt(el, rect.x + rect.width / 2, rect.y + rect.height / 2);
      if (cover) {{
        const permanent = cover.label || (cover.frame && cover.inside);
        if (permanent && rescrolls >= alignments.length) return 'element_covered_for_good';
        const alignment = alignments[rescrolls % alignments.length];
        rescrolls += 1;
        el.scrollIntoView({{ block: alignment, inline: alignment }});
        settling.forget();
        return 'element_covered';
      }}
    }}
    return rect;
  }};
  let previous = null;
  for (;;) {{
    const result = check();
    if (typeof result !== 'string') return {{ rect: result }};
    if (result === 'element_covered_for_good') {{
      return {{ refused: 'element_covered', cover, early: true, scrolled: true }};
    }}
    if (result === 'element_not_visible') settling.forget();
    // Settling after the scroll a cover caused is still waiting on the cover.
    const reason = result === 'element_not_stable' && previous === 'element_covered' ? previous : result;
    previous = result;
    if (result === 'element_gone' || performance.now() >= deadline) {{
      return {{ refused: reason, cover: reason === 'element_covered' ? cover : null, scrolled: rescrolls > 0 }};
    }}
    await settling.next();
  }}
}}"""

# The element's pointer rect now, without scrolling: null when no part of it is in the viewport.
ELEMENT_POINTER_RECT_SCRIPT = f"""(el) => ({_POINTER_RECT_JS})(el)"""

# What covers the element at (x, y), or null (see _COVER_AT_JS).
ELEMENT_COVER_SCRIPT = f"""(el, [x, y]) => {{
  {DOM_JS}
  return ({_COVER_AT_JS})(el, x, y);
}}"""

ELEMENT_FOCUS_SCRIPT = "(el) => { el.focus(); }"

# Whether keyboard focus is in the element or inside it, shadow roots included.
# Focus behind an open shadow root shows as its host, so the check follows it
# down to the innermost focused element first. With refuseFrames (an element_id
# target), focus in a frame inside the element is not focus in it: keys sent
# then go to the frame's document. Also run in the main world, on a locator, for
# a selector target, without it. _FOCUS_PLACE_JS is that walk (needs `dom`).
_FOCUS_PLACE_JS = """(el) => {
    let active = dom.active(dom.root(el));
    for (let root = active && dom.shadowRoot(active); root && dom.active(root); root = dom.shadowRoot(active)) {
      active = dom.active(root);
    }
    if (!active || !dom.within(active, el)) return 'out';
    return active !== el && dom.frame(active) ? 'framed' : 'in';
  }"""
ELEMENT_HAS_FOCUS_SCRIPT = f"""(el, refuseFrames) => {{
  {DOM_JS}
  const place = ({_FOCUS_PLACE_JS})(el);
  return place === 'in' || (place === 'framed' && !refuseFrames);
}}"""
# Where keyboard focus is, read as ELEMENT_HAS_FOCUS_SCRIPT reads it, in one answer: 'in' the element, 'framed' (in a
# frame inside it, the frame not the element itself) or 'out'. For a type's checks through its clear by selector,
# whose first check decides whether a frame counts (_TypedField.require_focus): asked as two questions, frames
# refused and then not, focus that moved between them set that from two moments.
ELEMENT_FOCUS_PLACE_SCRIPT = f"""(el) => {{
  {DOM_JS}
  return ({_FOCUS_PLACE_JS})(el);
}}"""

# A field's own attributes, as is_sensitive_field reads them. Needs `dom`.
_FIELD_ATTRIBUTES_JS = """(node) => {
    const read = (name) => dom.attr(node, name) || '';
    return {
      type: read('type'),
      name: read('name'),
      id: read('id'),
      placeholder: read('placeholder'),
      'aria-label': read('aria-label'),
      autocomplete: read('autocomplete'),
    };
  }"""
# The same attributes of a field on its own, for the TOTP autofill's look at a field it might type a code into
# (actions._find_one_time_code_field). It runs in the page's own world, on a locator, by Element.prototype's
# getAttribute (see DOM_JS): a copy of its own read them through the field's, which a page can define on the field.
FIELD_ATTRIBUTES_SCRIPT = f"""(el) => {{
  {DOM_JS}
  return ({_FIELD_ATTRIBUTES_JS})(el);
}}"""

# A type's hold on the node its text goes to. Defines `held`.
# take(el): the node keys go to while focus is in the element: the innermost focused element from the element's
# root, through open shadow roots, where it is the element or inside it. Not a frame: keys sent then go to its
# document's own focused node, which this document cannot hold. A blur or a focus on the node from then on says
# focus left it, though it came back (a field whose key sent focus to a password field, whose own key sent it back).
# So does a focusin in the node's own open shadow root: focus that goes from the node into its shadow tree and back
# fires neither a blur nor a focus on it (both ends of the move are the node, seen from outside the tree), and a
# move into a root further down passes through that root too. A root the node has only since the take was heard by
# nothing, so it says the same. Each listener goes on and comes off on its own: by selector they are put on with what
# the page has made of EventTarget.prototype, and one that threw as it went on ended the take with those before it
# left on the node, and as it came off left those after it. A hold one of whose listeners did not go on cannot hear
# every move, and says focus left; a release whose removal of one threw takes the others off and then throws.
# read(hold): the node's own attributes, as is_sensitive_field reads them, while it is still the innermost focused
# element of its root and focus never left it; else null. One the page took out of its document is no root's
# focused element. Run in the main world too, on a locator, for a selector target (see DOM_JS).
_HELD_FIELD_JS = f"""{DOM_JS}
const held = {{
  focused(from) {{
    let active = dom.active(dom.root(from));
    for (let root = active && dom.shadowRoot(active); root && dom.active(root); root = dom.shadowRoot(active)) {{
      active = dom.active(root);
    }}
    return active || null;
  }},
  take(el) {{
    const hold = {{ node: null, root: null, target: el, left: false, listener: null }};
    const active = held.focused(el);
    if (!active || !dom.within(active, el) || dom.frame(active)) {{
      return hold;
    }}
    hold.node = active;
    hold.root = dom.shadowRoot(active);
    hold.listener = () => {{ hold.left = true; }};
    for (const [node, type] of held.listened(hold)) {{
      try {{
        dom.listen(node, type, hold.listener, true);
      }} catch (error) {{
        hold.left = true;
      }}
    }}
    return hold;
  }},
  // The nodes the hold's listeners go on, each with its event: the node's blur and focus, its shadow root's focusin.
  listened(hold) {{
    const on = [[hold.node, 'blur'], [hold.node, 'focus']];
    if (hold.root) on.push([hold.root, 'focusin']);
    return on;
  }},
  read(hold) {{
    const node = hold.node;
    if (!node || hold.left || dom.shadowRoot(node) !== hold.root || held.focused(node) !== node) return null;
    return ({_FIELD_ATTRIBUTES_JS})(node);
  }},
  release(hold) {{
    if (!hold.node) return;
    const failures = [];
    for (const [node, type] of held.listened(hold)) {{
      try {{
        dom.unlisten(node, type, hold.listener, true);
      }} catch (error) {{
        failures.push(error);
      }}
    }}
    if (failures.length) throw failures[0];
  }},
}};"""

# By selector the hold is a handle of the page's, taken on the locator's element: read, then read once more as it
# is released (the handle is then given back). The release is in a finally: a read that throws (in the main world
# a page can have replaced what it calls) left the hold's listeners on the node.
FIELD_HOLD_TAKE_SCRIPT = f"""(el) => {{
  {_HELD_FIELD_JS}
  return held.take(el);
}}"""
FIELD_HOLD_READ_SCRIPT = f"""(hold) => {{
  {_HELD_FIELD_JS}
  return held.read(hold);
}}"""
FIELD_HOLD_RELEASE_SCRIPT = f"""(hold) => {{
  {_HELD_FIELD_JS}
  try {{
    return held.read(hold);
  }} finally {{
    held.release(hold);
  }}
}}"""
# By element_id the hold is the world's, kept under a nonce (the registry's fieldHolds, made here: a registry an
# earlier version made has none). Taking it answers its first read. Both are reads, which the world sends again
# when an answer is lost: a second take under the nonce reads the hold the first took, and a second release finds
# none and answers null, as for a node that lost focus. The hold keeps the element, so the release finds it by its
# id though the page has taken it out of the document. The hold leaves the registry, and its listeners the node, in
# a finally: taken out first, a read that then threw left listeners nothing could find again.
ELEMENT_FIELD_HOLD_SCRIPT = f"""(el, nonce, registry) => {{
  {_HELD_FIELD_JS}
  if (!registry.fieldHolds) registry.fieldHolds = new Map();
  if (!registry.fieldHolds.has(nonce)) registry.fieldHolds.set(nonce, held.take(el));
  return held.read(registry.fieldHolds.get(nonce));
}}"""
ELEMENT_FIELD_RELEASE_SCRIPT = f"""(el, nonce, registry) => {{
  {_HELD_FIELD_JS}
  const hold = registry.fieldHolds && registry.fieldHolds.get(nonce);
  if (!hold) return null;
  try {{
    return held.read(hold);
  }} finally {{
    registry.fieldHolds.delete(nonce);
    held.release(hold);
  }}
}}"""

# What an approval reads of the element its type names, before the type runs: every field the text could go to from
# there. No node has focus for the text yet, so there is none to hold; the type's focus can put the text in the
# element itself where it takes text (an input, a textarea, a select, an editable element), in the control of the
# label it is, or in a field inside either, through open shadow roots. All of it in one call: the attributes of
# each such field, and whether a frame is among what is named (the element, or one inside it), whose document is
# not read. An input no typed text goes into (a hidden one, a checkbox, a button) is no field: a form's hidden
# token would say secret of the whole form. Its type is the one the browser has for it, where one it does not know
# (type="hidden ", with a space) is text; and an element is editable where the browser says it is
# (isContentEditable), by its own contenteditable or by what it is in, which contenteditable="inherit" alone is not.
# A field that cannot take text now (disabled, read-only, or not shown) is answered apart, as `idle`: it does not make
# what is named read plain, where the keys go to something else (a component that takes them itself), and its name
# still says secret, since a page can enable or show it as the type focuses. A closed shadow root shows no node past
# it. Run in the main world too, on a selector's element (see DOM_JS).
NAMED_FIELDS_SCRIPT = f"""(el) => {{
  {DOM_JS}
  const noText = ['hidden', 'checkbox', 'radio', 'button', 'submit', 'reset', 'image', 'file', 'range', 'color'];
  const typeOf = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'type').get;
  const inputType = (node) => {{
    try {{
      return typeOf.call(node);
    }} catch (error) {{
      return '';
    }}
  }};
  // A form control takes text by what it is alone: every input in an editor is editable, a hidden one or a checkbox
  // too. What is no form control takes text where it is editable, and in an editor only its editing host and what in
  // it carries a field's name, by an attribute other than id, are read: counted each, the blocks of an editor with
  // ids of their own (20,000 paragraphs) took 3.3 s, past the read's bound. A block named by its id alone is not
  // read, unless it is what the type names (`named`). An editing host is editable and its parent in its own tree is
  // no editable element: an editor at the top of a shadow root is one, though the host it is in is editable (it
  // takes focus and the text itself), where asked across the root it was not, and was not read.
  const naming = ['name', 'placeholder', 'aria-label', 'autocomplete', 'type', 'role'];
  const parentOf = Object.getOwnPropertyDescriptor(Node.prototype, 'parentNode').get;
  const editingHost = (node) => {{
    const parent = parentOf.call(node);
    return !parent || dom.type(parent) !== 1 || !dom.editable(parent);
  }};
  const takesText = (node, named) =>
    dom.matches(node, 'input, textarea, select')
      ? dom.tag(node) !== 'INPUT' || !noText.includes(inputType(node))
      : dom.editable(node) && (named || editingHost(node) || naming.some((name) => dom.has(node, name)));
  const fields = new Set();
  let frame = false;
  const look = (node, named = false) => {{
    if (dom.frame(node)) frame = true;
    else if (takesText(node, named)) fields.add(node);
  }};
  const lookInside = (root) => {{
    for (const node of dom.all(root, '*')) {{
      look(node);
      const shadow = dom.shadowRoot(node);
      if (shadow) lookInside(shadow);
    }}
  }};
  const named = [el];
  const control = dom.control(el);
  if (control) named.push(control);
  for (const node of named) {{
    look(node, true);
    const shadow = dom.shadowRoot(node);
    if (shadow) lookInside(shadow);
    lookInside(node);
  }}
  // Shown (visibility counts: a visibility:hidden field takes no keys) and in no inert subtree, which takes none either.
  // Inert reaches across shadow roots and slots, where closest() does not (an inert host's shadow content is inert),
  // so the walk is the tree as it is shown, bounded as the other walks. A select has no read-only state: it takes
  // keys unless it is disabled.
  const checkVisibility = Element.prototype.checkVisibility;
  const inert = (node) => {{
    for (let at = node, steps = 0; at && steps < 256; at = dom.flatUp(at), steps += 1) {{
      if (dom.type(at) === 1 && dom.has(at, 'inert')) return true;
    }}
    return false;
  }};
  const live = (node) =>
    checkVisibility.call(node, {{ visibilityProperty: true }}) &&
    !inert(node) &&
    (dom.tag(node) === 'SELECT' ? !dom.matches(node, ':disabled') : dom.matches(node, ':read-write'));
  // Fields alike in every attribute are answered once: what is read of them is the same, and each paragraph of an
  // editor counts.
  const answer = (nodes) => {{
    const read = new Map();
    for (const node of nodes) {{
      const attributes = ({_FIELD_ATTRIBUTES_JS})(node);
      const key = JSON.stringify(attributes);
      if (!read.has(key)) read.set(key, attributes);
    }}
    return Array.from(read.values());
  }};
  const found = Array.from(fields);
  return {{ fields: answer(found.filter(live)), idle: answer(found.filter((node) => !live(node))), frame }};
}}"""

# Playwright's selectOptions, on the element: scrolls an enabled, visible
# <select> into view, waits for its box to settle and for options matching every
# one wanted (one for a single select), selects them and dispatches input then
# change. Answers { values } or { refused }.
ELEMENT_SELECT_SCRIPT = f"""async (el, {{ wanted, timeoutMs }}) => {{
  {DOM_JS}
  const isDisabled = {_IS_DISABLED_JS};
  const visibleBox = {_VISIBLE_BOX_JS};
  const settling = ({_SETTLING_JS})();
  const select = ({_FOLLOW_LABEL_JS})(el);
  if (select.localName !== 'select') return {{ refused: 'element_not_a_select' }};
  const normalize = (text) => text.replace(/[\\u200b\\u00ad]/g, '').trim().replace(/\\s+/g, ' ');
  const attempt = () => {{
    if (!dom.here(el) || !dom.here(select)) return 'element_gone';
    if (!visibleBox(el)) return 'element_not_visible';
    // The select found first is the one checked and changed, whatever the label names by now.
    if (isDisabled(select)) return 'element_disabled';
    if (el.scrollIntoViewIfNeeded) el.scrollIntoViewIfNeeded(true);
    else el.scrollIntoView({{ block: 'center', inline: 'center' }});
    const box = visibleBox(el);
    if (!box) return 'element_not_visible';
    if (!settling.settled(box)) return 'element_not_stable';
    const options = [...select.options];
    const chosen = [];
    let remaining = wanted.slice();
    for (let index = 0; index < options.length; index += 1) {{
      const option = options[index];
      const label = normalize(option.label);
      const matchesLabel = (text) => text === option.label || normalize(text) === label;
      const matches = (want) =>
        (want.valueOrLabel === undefined || want.valueOrLabel === option.value || matchesLabel(want.valueOrLabel)) &&
        (want.label === undefined || matchesLabel(want.label)) &&
        (want.index === undefined || want.index === index);
      if (!remaining.some(matches)) continue;
      if (isDisabled(option, false)) return 'option_disabled';
      chosen.push(option);
      if (!select.multiple) {{ remaining = []; break; }}
      remaining = remaining.filter((want) => !matches(want));
    }}
    if (remaining.length) return 'option_not_found';
    select.value = undefined;
    for (const option of chosen) option.selected = true;
    select.dispatchEvent(new Event('input', {{ bubbles: true, composed: true }}));
    select.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return chosen.map((option) => option.value);
  }};
  const deadline = performance.now() + timeoutMs;
  for (;;) {{
    const result = attempt();
    if (typeof result !== 'string') return {{ values: result }};
    if (result === 'element_not_visible') settling.forget();
    if (result === 'element_gone' || performance.now() >= deadline) return {{ refused: result }};
    await settling.next();
  }}
}}"""

# Marks a file input for one upload and records the files it holds when its
# input and change events reach it (pages often empty the field as they read it,
# on either). The input is the element, or the one a label for it stands for (a
# custom upload button), found once, here: the record keeps it, so the checks,
# the confirmation and the unmark all use that input. The listener is kept in
# the record so the unmark can remove it.
UPLOAD_MARK_SCRIPT = f"""(el, nonce, registry) => {{
  {DOM_JS}
  const input = ({_FOLLOW_LABEL_JS})(el);
  if (input.localName !== 'input' || input.type !== 'file') return false;
  const record = {{ seen: [], listener: null, input }};
  record.listener = () => {{
    record.seen.push(Array.from(input.files || [], (file) => ({{ name: file.name, size: file.size }})));
  }};
  registry.uploads.set(nonce, record);
  input.addEventListener('input', record.listener, {{ capture: true, once: true }});
  input.addEventListener('change', record.listener, {{ capture: true, once: true }});
  input.setAttribute('{MARKER_ATTRIBUTE}', nonce);
  return true;
}}"""

# Whether the marked input carries the marker and is the one element of the
# document that does: moved into a closed shadow root or an iframe, it is not
# found here.
UPLOAD_MARKED_ALONE_SCRIPT = f"""(el, nonce, registry) => {{
  const record = registry.uploads.get(nonce);
  if (!record) return false;
  const marked = document.querySelectorAll('[{MARKER_ATTRIBUTE}="' + nonce + '"]');
  const input = record.input;
  return marked.length === 1 && marked[0] === input && input.getAttribute('{MARKER_ATTRIBUTE}') === nonce;
}}"""

# Null when the element holds exactly one file of the uploaded file's name and
# size, now or when its input or change event reached it; else why not.
# Best-effort: a page can give the input a file of its own with that name and
# size, and a capture listener on the window or document runs first.
UPLOAD_LANDED_SCRIPT = """(el, [nonce, name, size], registry) => {
  const record = registry.uploads.get(nonce);
  const seen = record ? record.seen : [];
  const input = record ? record.input : el;
  const now = Array.from(input.files || [], (file) => ({ name: file.name, size: file.size }));
  const holds = (files) => !!files && files.length === 1 && files[0].name === name && files[0].size === size;
  if (holds(now) || seen.some(holds)) return null;
  if (!now.length && !seen.length) {
    return 'the input holds no file and saw no input or change event: the file may have gone elsewhere';
  }
  return 'the input does not hold a file of this name and size: the page emptied or replaced it';
}"""

# Removes the marker from every element of the document that carries it, open
# shadow roots included, and from the element itself wherever it is now, and
# removes the mark's listeners. Closed shadow roots and frames are not walked. Runs whether or not the element is still
# there, so it is a world function of its own, not an element script.
UPLOAD_UNMARK_SCRIPT = f"""(elementId, nonce) => {{
  {DOM_JS}
  const registry = {ELEMENT_REGISTRY_SCRIPT};
  const roots = [document];
  for (let index = 0; index < roots.length; index += 1) {{
    for (const node of roots[index].querySelectorAll('*')) {{
      const shadow = dom.shadowRoot(node);
      if (shadow) roots.push(shadow);
      if (dom.attr(node, '{MARKER_ATTRIBUTE}') === nonce) dom.unset(node, '{MARKER_ATTRIBUTE}');
    }}
  }}
  const record = registry.uploads.get(nonce);
  const el = record ? record.input : registry.element(elementId);
  if (el && dom.attr(el, '{MARKER_ATTRIBUTE}') === nonce) dom.unset(el, '{MARKER_ATTRIBUTE}');
  if (el && record) {{
    for (const type of ['input', 'change']) el.removeEventListener(type, record.listener, {{ capture: true }});
  }}
  registry.uploads.delete(nonce);
}}"""
