// background.js — the whole plugin. Three ways to index the current page
// (toolbar popup, right-click menu, Alt+Shift+I); each captures the rendered
// page and POSTs it to the local daemon. Parsing happens there, not here.
importScripts('capture.js'); // defines capturePage()

const DEFAULT_DAEMON = 'http://127.0.0.1:8765';
async function daemonUrl() {
  const s = await chrome.storage.local.get('daemon');
  return (s.daemon || DEFAULT_DAEMON).replace(/\/$/, '');
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: 'index-page', title: 'Index this page', contexts: ['page', 'selection', 'link'] });
});
chrome.contextMenus.onClicked.addListener((info, tab) => { if (info.menuItemId === 'index-page') indexTab(tab); });
chrome.commands.onCommand.addListener(async (cmd, tab) => {
  if (cmd !== 'index-page') return;
  if (!tab) [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  indexTab(tab);
});
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type === 'index') { indexTab(msg.tab).then(reply); return true; }
  if (msg.type === 'status') { getStatus().then(reply); return true; }
});

async function indexTab(tab) {
  try {
    if (!tab || !tab.id || /^(chrome|edge|about|chrome-extension|view-source|devtools):/i.test(tab.url || '')) {
      throw new Error("This kind of page can't be indexed.");
    }
    const [res] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: capturePage });
    const page = res && res.result;
    if (!page || !page.text) throw new Error('Nothing was captured from this page.');
    let r;
    try {
      r = await fetch((await daemonUrl()) + '/pages', {
        method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(page),
      });
    } catch (e) {
      throw new Error('The Harvest daemon is not running. Start it with: python3 daemon/harvestd.py');
    }
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(out.error || ('daemon HTTP ' + r.status));
    flash(tab.id, '✓', '#1f7a6d');
    return { ok: true, ...out, chars: page.text.length, selection: page.selection };
  } catch (e) {
    flash(tab && tab.id, '!', '#a3352b');
    return { ok: false, error: String((e && e.message) || e) };
  }
}

async function getStatus() {
  try {
    const r = await fetch((await daemonUrl()) + '/status');
    return { ok: true, ...(await r.json()) };
  } catch (e) {
    return { ok: false };
  }
}

function flash(tabId, text, color) {
  if (!tabId) return;
  chrome.action.setBadgeBackgroundColor({ tabId, color });
  chrome.action.setBadgeText({ tabId, text });
  setTimeout(() => chrome.action.setBadgeText({ tabId, text: '' }), 2500);
}
