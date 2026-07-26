/**
 * Salesforce Lightning DOM capture probe — agent A1
 *
 * The hard parts this solves:
 * 1. Shadow DOM: Lightning Web Components hide the real target behind shadow roots.
 *    event.target is a lie — it's the shadow host. event.composedPath()[0] is truth.
 * 2. Redaction: MUST NOT write secrets, even once. Check field names + value patterns
 *    BEFORE building the event object.
 * 3. Capture phase: Lightning stops propagation everywhere. Listening in bubble phase
 *    misses most component interactions. We listen in capture phase on document.
 * 4. Cross-frame isolation: This script runs once per document (top + each iframe).
 *    Cannot reach across cross-origin boundaries; must report frame chain from inside.
 *
 * Injected via Playwright's add_init_script before any page script runs.
 * Survives as long as the document lives; re-injected on navigate.
 */

(function(global) {
  'use strict';

  // Skip browser code when running in Node for tests
  if (typeof global.window === 'undefined') {
    // Node.js environment — skip to exports only
    if (typeof module !== 'undefined' && module.exports) {
      module.exports = getNodeExports();
    }
    return;
  }

  // ============================================================================
  // GLOBALS & STATE
  // ============================================================================

  const VERSION = 1;
  const INPUT_DEBOUNCE_MS = 250;

  let sequenceNumber = 0; // Monotonic per-document; resets on navigate/reload
  let lastInputEvent = null; // { element, timeoutId }
  const eventBuffer = []; // fallback when window.__sfCaptureSink is missing

  /**
   * capture_session: stable ID for this document's recording session.
   * Critical for cross-document ordering: `seq` restarts per document, but `t`
   * (Date.now()) is the global ordering key. Python side uses `t` to merge/sort
   * events from multiple frames and across navigations.
   */
  const captureSessionId = `${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;

  // ============================================================================
  // REDACTION (HARD REQUIREMENT)
  // ============================================================================

  /**
   * Sensitive field name patterns. Case-insensitive match on name, id, aria-label,
   * or ancestor <label> text.
   */
  const SENSITIVE_NAME_PATTERN = /pass(word)?|secret|token|ssn|social.?security|credit.?card|card.?number|cvv|cvc|pin|auth(entication)?|api.?key|private.?key|routing|account.?number/i;

  /**
   * Sensitive value patterns: credit-card-like (13-19 digits), SSN-like.
   * Applied even when field name looks safe.
   */
  const CREDIT_CARD_PATTERN = /^\d[\d\s\-]{11,17}\d$/; // 13-19 digits, optional separators
  const SSN_PATTERN = /^\d{3}[\-\s]?\d{2}[\-\s]?\d{4}$/;

  function isSensitiveField(element) {
    if (element.type === 'password') return true;

    const nameish = [
      element.name,
      element.id,
      element.getAttribute('aria-label'),
      element.getAttribute('placeholder'),
      element.getAttribute('data-field-api-name'),
      element.getAttribute('field-name')
    ].filter(Boolean).join(' ');

    if (SENSITIVE_NAME_PATTERN.test(nameish)) return true;

    // Check ancestor <label>
    const label = element.labels?.[0] || element.closest('label');
    if (label && SENSITIVE_NAME_PATTERN.test(label.textContent || '')) return true;

    return false;
  }

  function isSensitiveValue(value) {
    if (typeof value !== 'string') return false;
    return CREDIT_CARD_PATTERN.test(value) || SSN_PATTERN.test(value);
  }

  function maybeRedactValue(element, rawValue) {
    if (isSensitiveField(element) || isSensitiveValue(rawValue)) {
      return { value: null, value_redacted: true };
    }
    return { value: rawValue, value_redacted: false };
  }

  // ============================================================================
  // SHADOW DOM & SELECTOR COMPUTATION
  // ============================================================================

  /**
   * Returns [innermost target, shadow depth].
   * event.target is the shadow host; event.composedPath()[0] is the real element.
   */
  function getTrueTarget(event) {
    const path = event.composedPath();
    const target = path[0] || event.target;

    // Count ShadowRoot boundaries from target up to document
    let depth = 0;
    let node = target;
    while (node) {
      if (node instanceof ShadowRoot) depth++;
      node = node.parentNode || node.host;
    }

    return { target, shadow_depth: depth };
  }

  /**
   * Compute ARIA role + accessible name.
   * Role: explicit `role` attr, else implicit from tag.
   * Name: aria-label > aria-labelledby (resolved) > textContent > title > alt.
   */
  function getRoleAndName(element) {
    const explicitRole = element.getAttribute('role');
    const implicitRole = {
      button: 'button', a: 'link', input: getInputRole(element.type),
      select: 'combobox', textarea: 'textbox', img: 'img',
      h1: 'heading', h2: 'heading', h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
      nav: 'navigation', main: 'main', aside: 'complementary', footer: 'contentinfo', header: 'banner'
    }[element.tagName.toLowerCase()] || null;

    const role = explicitRole || implicitRole;

    // Accessible name
    const ariaLabel = element.getAttribute('aria-label');
    if (ariaLabel) return { role, name: ariaLabel };

    const labelledby = element.getAttribute('aria-labelledby');
    if (labelledby) {
      const ids = labelledby.split(/\s+/);
      const texts = ids.map(id => {
        const el = document.getElementById(id);
        return el ? (el.textContent || '').trim() : '';
      }).filter(Boolean);
      if (texts.length) return { role, name: texts.join(' ') };
    }

    const text = (element.textContent || '').trim();
    if (text) return { role, name: text };

    const title = element.getAttribute('title');
    if (title) return { role, name: title };

    const alt = element.getAttribute('alt');
    if (alt) return { role, name: alt };

    return { role, name: null };
  }

  function getInputRole(inputType) {
    const roleMap = {
      button: 'button', submit: 'button', reset: 'button', image: 'button',
      checkbox: 'checkbox', radio: 'radio',
      range: 'slider', number: 'spinbutton',
      search: 'searchbox'
    };
    return roleMap[inputType] || 'textbox';
  }

  /**
   * CSS path with shadow-piercing ` >>> ` separator.
   * Walks from element up to document, emitting tagname.class selectors.
   * When crossing a ShadowRoot boundary, emit ` >>> `.
   */
  function buildCssPath(element) {
    const parts = [];
    let node = element;

    while (node && node !== document) {
      let selector = node.tagName.toLowerCase();
      if (node.id) {
        selector += `#${node.id}`;
      } else if (node.classList.length) {
        selector += '.' + Array.from(node.classList).join('.');
      }
      parts.unshift(selector);

      const parent = node.parentNode;
      if (parent instanceof ShadowRoot) {
        parts.unshift(' >>> ');
        node = parent.host;
      } else {
        node = parent;
      }
    }

    return parts.join(' > ');
  }

  /**
   * XPath (diagnostic only, never primary selector).
   */
  function buildXPath(element) {
    if (element.id) return `//*[@id='${element.id}']`;

    const parts = [];
    let node = element;

    while (node && node.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = node.previousSibling;
      while (sibling) {
        if (sibling.nodeType === Node.ELEMENT_NODE && sibling.tagName === node.tagName) {
          index++;
        }
        sibling = sibling.previousSibling;
      }

      const tagName = node.tagName.toLowerCase();
      parts.unshift(`${tagName}[${index}]`);

      // Stop at shadow root
      if (node.parentNode instanceof ShadowRoot) break;
      node = node.parentNode;
    }

    return '/' + parts.join('/');
  }

  /**
   * Compute all selectors from contract section 2.1.
   * Returns null for any selector that cannot be derived.
   */
  function computeSelectors(element) {
    const testId = element.getAttribute('data-testid') || element.getAttribute('data-qa');
    const ariaLabel = element.getAttribute('aria-label');
    const roleNameObj = getRoleAndName(element);

    // label[for] association (inputs only)
    let labelFor = null;
    if (element.id && (element.tagName.toLowerCase() === 'input' || element.tagName.toLowerCase() === 'textarea' || element.tagName.toLowerCase() === 'select')) {
      const label = document.querySelector(`label[for='${element.id}']`);
      if (label) labelFor = (label.textContent || '').trim();
    }

    // Salesforce field API name
    const sfField = element.getAttribute('data-field-api-name')
      || element.getAttribute('data-name')
      || element.getAttribute('field-name')
      || element.closest('[data-field-api-name]')?.getAttribute('data-field-api-name')
      || null;

    const text = (element.textContent || '').trim().substring(0, 100); // cap at 100 chars

    return {
      test_id: testId ? `[data-testid='${testId}']` : null,
      aria: ariaLabel ? `${element.tagName.toLowerCase()}[aria-label='${ariaLabel}']` : null,
      role_name: roleNameObj,
      label_for: labelFor,
      sf_field: sfField,
      css_path: buildCssPath(element),
      text: text || null,
      xpath: buildXPath(element)
    };
  }

  // ============================================================================
  // SALESFORCE CONTEXT
  // ============================================================================

  /**
   * Parse Salesforce context from URL.
   * /lightning/r/<Object>/<RecordId>/view  => record_home
   * /lightning/o/<Object>/list             => list
   * /lightning/n/<AppPage>                 => app_page
   * /lightning/setup/                      => setup
   */
  function parseSalesforceContext(url) {
    const u = new URL(url);
    const path = u.pathname;

    const recordMatch = path.match(/\/lightning\/r\/([^\/]+)\/([^\/]+)\/view/);
    if (recordMatch) {
      return {
        object: recordMatch[1],
        record_id: recordMatch[2],
        page_type: 'record_home',
        app: null // App name not in URL; would need DOM scrape of .appName or nav context
      };
    }

    const listMatch = path.match(/\/lightning\/o\/([^\/]+)\/list/);
    if (listMatch) {
      return {
        object: listMatch[1],
        record_id: null,
        page_type: 'list',
        app: null
      };
    }

    const appPageMatch = path.match(/\/lightning\/n\/([^\/]+)/);
    if (appPageMatch) {
      return {
        object: null,
        record_id: null,
        page_type: 'app_page',
        app: appPageMatch[1]
      };
    }

    if (path.includes('/lightning/setup/')) {
      return {
        object: null,
        record_id: null,
        page_type: 'setup',
        app: null
      };
    }

    return {
      object: null,
      record_id: null,
      page_type: 'unknown',
      app: null
    };
  }

  /**
   * Detect if element is inside a modal.
   * Returns { is_in_modal: bool, modal_label: string | null }.
   */
  function detectModal(element) {
    let node = element;
    while (node && node !== document) {
      if (node.getAttribute?.('role') === 'dialog'
          || node.classList?.contains('slds-modal')
          || node.classList?.contains('uiModal')) {
        // Find modal heading
        const heading = node.querySelector('h1, h2, .slds-modal__header');
        const label = heading ? (heading.textContent || '').trim() : null;
        return { is_in_modal: true, modal_label: label };
      }

      const parent = node.parentNode;
      node = parent instanceof ShadowRoot ? parent.host : parent;
    }

    return { is_in_modal: false, modal_label: null };
  }

  // ============================================================================
  // FRAME PATH
  // ============================================================================

  /**
   * Build frame_path: ordered iframe selector chain, outermost first.
   * This script runs per-frame. If we're not top, walk up via window.frameElement
   * (guarding for cross-origin SecurityError).
   */
  function buildFramePath() {
    const path = [];
    let win = window;

    while (win !== win.top) {
      try {
        const frameElement = win.frameElement;
        if (frameElement) {
          // Build a simple selector for this iframe
          const tag = frameElement.tagName.toLowerCase();
          const id = frameElement.id ? `#${frameElement.id}` : '';
          const name = frameElement.name ? `[name='${frameElement.name}']` : '';
          path.unshift(`${tag}${id}${name}`);
        } else {
          // Cross-origin: cannot access frameElement
          path.unshift('iframe[cross-origin]');
          break;
        }
        win = win.parent;
      } catch (e) {
        // SecurityError on cross-origin
        path.unshift('iframe[cross-origin]');
        break;
      }
    }

    return path;
  }

  // ============================================================================
  // EVENT CAPTURE
  // ============================================================================

  function captureEvent(type, event) {
    try {
      const { target, shadow_depth } = getTrueTarget(event);
      const selectors = computeSelectors(target);
      const { is_in_modal, modal_label } = detectModal(target);
      const sf = parseSalesforceContext(window.location.href);

      // Extract value for input/change/select
      let rawValue = null;
      if ((type === 'input' || type === 'change') && (target.value !== undefined)) {
        rawValue = target.value;
      }

      const { value, value_redacted } = maybeRedactValue(target, rawValue);

      const eventObj = {
        v: VERSION,
        seq: ++sequenceNumber,
        t: Date.now(),
        type,
        url: window.location.href,
        frame_path: buildFramePath(),
        selectors,
        element: {
          tag: target.tagName.toLowerCase(),
          type: target.type || null,
          name: target.name || null,
          id: target.id || null,
          classes: Array.from(target.classList || []),
          aria_label: target.getAttribute('aria-label') || null,
          text: (target.textContent || '').trim().substring(0, 100) || null,
          is_in_modal,
          modal_label,
          shadow_depth
        },
        value,
        value_redacted,
        sf,
        capture_session: captureSessionId
      };

      emit(eventObj);
    } catch (err) {
      // NEVER throw from a recorder — a broken recorder that crashes the page is worse than no recorder.
      console.error('[__sfCapture] Event capture failed:', err);
    }
  }

  function emit(eventObj) {
    if (typeof window.__sfCaptureSink === 'function') {
      window.__sfCaptureSink(eventObj);
    } else {
      eventBuffer.push(eventObj);
    }
  }

  // ============================================================================
  // DEBOUNCED INPUT HANDLER
  // ============================================================================

  function handleInput(event) {
    const { target } = getTrueTarget(event);

    // Cancel previous debounce for this element
    if (lastInputEvent && lastInputEvent.element === target) {
      clearTimeout(lastInputEvent.timeoutId);
    }

    // Debounce: wait 250ms after last input on same element
    const timeoutId = setTimeout(() => {
      captureEvent('input', event);
      lastInputEvent = null;
    }, INPUT_DEBOUNCE_MS);

    lastInputEvent = { element: target, timeoutId };
  }

  // ============================================================================
  // LISTENER INSTALLATION
  // ============================================================================

  function installListeners() {
    // Capture phase is REQUIRED — Lightning stops propagation everywhere.
    document.addEventListener('click', (e) => captureEvent('click', e), true);
    document.addEventListener('input', handleInput, true);
    document.addEventListener('change', (e) => captureEvent('change', e), true);
    document.addEventListener('submit', (e) => captureEvent('submit', e), true);
    document.addEventListener('keydown', (e) => {
      // Capture only modified keydowns (ctrl/cmd/alt)
      if (e.ctrlKey || e.metaKey || e.altKey) {
        captureEvent('keydown', e);
      }
    }, true);
    document.addEventListener('scroll', (e) => captureEvent('scroll', e), true);
  }

  // ============================================================================
  // PUBLIC API
  // ============================================================================

  window.__sfCapture = {
    start() {
      installListeners();
    },

    stop() {
      // Cleanup not implemented: this script runs per-document; cleanup happens on unload.
      // Explicit removeEventListener would require storing listener refs; not needed for PoC.
    },

    drain() {
      const buffered = eventBuffer.slice();
      eventBuffer.length = 0;
      return buffered;
    }
  };

  // Auto-start on load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.__sfCapture.start());
  } else {
    window.__sfCapture.start();
  }

  // ============================================================================
  // NODE.JS TEST EXPORTS (for agent A8's Python tests to shell out to node)
  // ============================================================================

  function getNodeExports() {
    // Extract pure helper functions for testing
    const SENSITIVE_NAME_PATTERN = /pass(word)?|secret|token|ssn|social.?security|credit.?card|card.?number|cvv|cvc|pin|auth(entication)?|api.?key|private.?key|routing|account.?number/i;
    const CREDIT_CARD_PATTERN = /^\d[\d\s\-]{11,17}\d$/;
    const SSN_PATTERN = /^\d{3}[\-\s]?\d{2}[\-\s]?\d{4}$/;

    return {
      isSensitiveField: function(elementMock) {
        if (elementMock.type === 'password') return true;
        const nameish = [
          elementMock.name,
          elementMock.id,
          elementMock['aria-label'],
          elementMock.placeholder,
          elementMock['data-field-api-name'],
          elementMock['field-name']
        ].filter(Boolean).join(' ');
        return SENSITIVE_NAME_PATTERN.test(nameish);
      },

      isSensitiveValue: function(value) {
        if (typeof value !== 'string') return false;
        return CREDIT_CARD_PATTERN.test(value) || SSN_PATTERN.test(value);
      },

      parseSalesforceContext: function(urlString) {
        const url = new URL(urlString);
        const path = url.pathname;

        const recordMatch = path.match(/\/lightning\/r\/([^\/]+)\/([^\/]+)\/view/);
        if (recordMatch) {
          return {
            object: recordMatch[1],
            record_id: recordMatch[2],
            page_type: 'record_home',
            app: null
          };
        }

        const listMatch = path.match(/\/lightning\/o\/([^\/]+)\/list/);
        if (listMatch) {
          return {
            object: listMatch[1],
            record_id: null,
            page_type: 'list',
            app: null
          };
        }

        const appPageMatch = path.match(/\/lightning\/n\/([^\/]+)/);
        if (appPageMatch) {
          return {
            object: null,
            record_id: null,
            page_type: 'app_page',
            app: appPageMatch[1]
          };
        }

        if (path.includes('/lightning/setup/')) {
          return {
            object: null,
            record_id: null,
            page_type: 'setup',
            app: null
          };
        }

        return {
          object: null,
          record_id: null,
          page_type: 'unknown',
          app: null
        };
      }
    };
  }

})(typeof window !== 'undefined' ? window : global);
