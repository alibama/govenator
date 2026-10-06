// capture.js — runs IN THE PAGE (injected by background.js via chrome.scripting).
// Must be self-contained: the function is serialized, so no outer variables.
//
// Turns the *rendered* DOM into compact text that keeps the structure an LLM
// needs: headings (#), list items (-), table rows (a | b | c), mailto/tel
// targets, and <select> option nesting (CivicPlus directories encode their
// department hierarchy as indented options). No per-site rules.

function capturePage() {
  var MAX = 150000;
  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, SVG: 1, IFRAME: 1, TEMPLATE: 1, CANVAS: 1, VIDEO: 1,
    AUDIO: 1, PICTURE: 1, IMG: 1, BUTTON: 1, INPUT: 1, TEXTAREA: 1, OBJECT: 1, EMBED: 1 };
  var BLOCK = { P: 1, DIV: 1, SECTION: 1, ARTICLE: 1, ASIDE: 1, HEADER: 1, FOOTER: 1, NAV: 1, UL: 1, OL: 1,
    DL: 1, DT: 1, DD: 1, TABLE: 1, THEAD: 1, TBODY: 1, TFOOT: 1, CAPTION: 1, BLOCKQUOTE: 1, ADDRESS: 1,
    FIGURE: 1, FIGCAPTION: 1, MAIN: 1, FIELDSET: 1, LEGEND: 1, BR: 1, HR: 1, PRE: 1, FORM: 1 };
  var out = [], line = '', size = 0;

  function clean(s) { return String(s || '').replace(/[\u00a0\s]+/g, ' ').trim(); }
  function push(t) { if (t && size < MAX) { out.push(t); size += t.length + 1; } }
  function flush() { push(clean(line)); line = ''; }
  function contactSuffix(el, already) {
    var s = '';
    var as = el.querySelectorAll ? el.querySelectorAll('a[href^="mailto:"], a[href^="tel:"]') : [];
    for (var i = 0; i < as.length; i++) {
      var v = (as[i].getAttribute('href') || '').replace(/^(mailto|tel):/i, '').split('?')[0].trim();
      var digits = v.replace(/\D/g, '');
      var shown = digits.length >= 7 && already.replace(/\D/g, '').indexOf(digits.slice(-7)) >= 0;
      if (v && already.indexOf(v) < 0 && !shown) s += ' <' + v + '>';
    }
    return s;
  }
  // Text of an element without script/style content. Obfuscated-email scripts
  // (CivicPlus: var w="user"; var x="domain"  or  new Array("u","s","e","r"...))
  // are decoded instead of dropped, so the address survives.
  function decodeScript(src) {
    var w = src.match(/\bw\s*=\s*"([^"]*)"/), x = src.match(/\bx\s*=\s*"([^"]*)"/);
    if (w && x && w[1] && x[1]) return ' <' + w[1] + '@' + x[1] + '>';
    var a = src.match(/new Array\(([^)]*)\)/);
    if (a) { var e = (a[1].match(/"([^"]*)"/g) || []).map(function (q) { return q.slice(1, -1); }).join('');
      if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) return ' <' + e + '>'; }
    return '';
  }
  function textOf(el) {
    var s = '';
    (function t(n) {
      if (n.nodeType === 3) { s += n.nodeValue; return; }
      if (n.nodeType !== 1) return;
      var tg = String(n.tagName || '').toUpperCase();
      if (tg === 'SCRIPT') { s += decodeScript(n.textContent || ''); return; }
      if (SKIP[tg]) return;
      var c = n.childNodes; for (var i = 0; i < c.length; i++) t(c[i]);
      if (BLOCK[tg]) s += ' ';
    })(el);
    return clean(s);
  }
  function cellText(td) { var t = textOf(td); return t + contactSuffix(td, t); }
  function kids(el) { var c = el.childNodes; for (var i = 0; i < c.length; i++) walk(c[i]); }
  function walk(n) {
    if (size >= MAX) return;
    if (n.nodeType === 3) { line += n.nodeValue; return; }
    if (n.nodeType !== 1) return;
    var tag = String(n.tagName || '').toUpperCase();
    if (tag === 'SCRIPT') { line += decodeScript(n.textContent || ''); return; }
    if (SKIP[tag] || n.hidden || (n.getAttribute && n.getAttribute('aria-hidden') === 'true')) return;
    if (/^H[1-6]$/.test(tag)) { flush(); line = new Array(+tag[1] + 1).join('#') + ' '; kids(n); flush(); return; }
    if (tag === 'LI') { flush(); line = '- '; kids(n); flush(); return; }
    if (tag === 'TR') {
      flush();
      var cells = [], ch = n.children || [];
      for (var i = 0; i < ch.length; i++) if (/^T[DH]$/i.test(ch[i].tagName)) { var c = cellText(ch[i]); if (c) cells.push(c); }
      push(cells.join(' | '));
      return;
    }
    if (tag === 'SELECT') {
      flush();
      var opts = n.querySelectorAll('option');
      for (var j = 0; j < opts.length; j++) {
        var raw = String(opts[j].textContent || '').replace(/\s+$/, '');
        var lead = (raw.match(/^[\s\u00a0]*/) || [''])[0].length;
        var t = clean(raw);
        if (t) push(new Array(Math.min(8, Math.floor(lead / 2)) + 1).join('  ') + '- ' + t);
      }
      return;
    }
    if (tag === 'A') {
      var h = n.getAttribute('href') || '';
      kids(n);
      if (/^mailto:/i.test(h)) line += ' <' + h.slice(7).split('?')[0] + '>';
      else if (/^tel:/i.test(h)) line += ' <tel:' + h.slice(4) + '>';
      return;
    }
    var block = !!BLOCK[tag];
    if (block) flush();
    kids(n);
    if (block) flush();
  }

  // Selected text wins (lets you index just part of a page).
  var sel = '';
  try { sel = String(window.getSelection ? window.getSelection() : '').trim(); } catch (e) {}

  var root = document.body;
  var picks = ['main', '[role="main"]', '#main', '#content', '#maincontent', '.main-content', 'article'];
  for (var k = 0; k < picks.length; k++) {
    var el = document.querySelector(picks[k]);
    if (el && clean(el.textContent).length > 400) { root = el; break; }
  }

  var crumb = '';
  try {
    var bc = document.querySelector('[class*="breadcrumb"], nav[aria-label*="readcrumb"]');
    if (bc) crumb = textOf(bc).slice(0, 300);
  } catch (e) {}

  var body;
  if (sel) body = sel.slice(0, MAX);
  else {
    walk(root); flush();
    var dedup = [];
    for (var m = 0; m < out.length; m++) if (out[m] !== out[m - 1]) dedup.push(out[m]);
    body = dedup.join('\n');
  }

  var title = clean(document.title);
  var head = ['# ' + title, 'URL: ' + location.href];
  if (crumb) head.push('Breadcrumb: ' + crumb);
  return {
    url: location.href,
    title: title,
    text: head.join('\n') + '\n\n' + body,
    lang: (document.documentElement && document.documentElement.lang) || '',
    captured_at: new Date().toISOString(),
    selection: !!sel,
  };
}
