const $ = (id) => document.getElementById(id);

async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: 'status' });
  if (!s || !s.ok) {
    $('status').innerHTML = 'Daemon not running. Start it with<br><code>python3 daemon/harvestd.py</code>';
    return;
  }
  const p = s.pages || {};
  const queued = (p.pending || 0) + (p.parsing || 0);
  $('status').textContent = `${queued} waiting · ${p.done || 0} parsed` + (p.error ? ` · ${p.error} failed` : '')
    + ` · ${s.sites} site(s) · worker ${s.worker.alive ? s.worker.status : 'stopped'}`;
}

$('index').onclick = async () => {
  $('index').disabled = true;
  $('msg').className = ''; $('msg').textContent = 'Capturing…';
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const r = await chrome.runtime.sendMessage({ type: 'index', tab });
  if (r && r.ok) {
    $('msg').className = 'ok';
    $('msg').textContent = `Indexed${r.selection ? ' (selection)' : ''} · ${r.site} · ${r.status === 'done' ? 'unchanged since last time' : 'queued for parsing'}`;
  } else {
    $('msg').className = 'err'; $('msg').textContent = (r && r.error) || 'Failed.';
  }
  $('index').disabled = false;
  refresh();
};

chrome.storage.local.get('daemon').then((s) => { $('daemon').value = s.daemon || ''; });
$('daemon').onchange = () => chrome.storage.local.set({ daemon: $('daemon').value.trim() });
refresh();
