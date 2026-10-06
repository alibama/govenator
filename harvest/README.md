# Harvest

Mark a page, and a local model does the rest. You browse government websites and
click **Index this page** on anything useful. A local model (Ollama) reads each
page in the background, and once a site's pages are in, it organizes them into
an org chart: departments, divisions, offices, who holds them, and the published
contacts.

```
browser plugin ──"Index this page"──▶ harvestd (local daemon) ──▶ harvest.db (SQLite)
  captures the page you are                │
  already looking at                       ├─ 1. parse each new page with every model
                                           └─ 2. when a site goes quiet, organize all of
                                                 its pages into one org chart
dashboard (Streamlit) ── reads harvest.db: org charts · pages · model comparison
```

## Quick start

```bash
ollama pull qwen3.6                                  # and optionally a second model
python3 daemon/harvestd.py --models qwen3.6          # inbox + background parser, stdlib only
pip install -r dashboard/requirements.txt
streamlit run dashboard/app.py                        # http://localhost:8501
```

Or run both at once with `./start.sh`.

Load the plugin from `chrome://extensions` → Developer mode → **Load unpacked** →
the `extension/` folder. To index a page, use the toolbar button, the right-click
menu, or **Alt+Shift+I**. If you select text first, only the selection is indexed.

## Why the capture is a plugin (and not Playwright)

The browser you're reading in has already rendered the JavaScript, decoded the
obfuscated emails, and passed whatever bot checks the site runs. A person saving
the page they're reading isn't a crawler, which is what robots.txt governs.
Headless browsers get blocked by the same bot defenses, and defeating those means
stealth/fingerprint tricks that this project shouldn't use. Playwright could
still be added later as an optional crawler for robots-permitted pages.

Everything after capture lives outside the browser, because a plugin can't run
a database or a long model job well. Streamlit can't easily receive the plugin's
uploads, so a tiny daemon sits in between.

## What the model does, and what code does

**Capture** (`extension/capture.js`, no model): the rendered page becomes compact
text that keeps its structure. Headings become `#`, list items become `-`, table
rows become `a | b | c`, mailto/tel targets are kept, nested `<select>` options
keep their indentation, and obfuscated-email scripts are decoded. A real
Charlottesville staff page comes to about 3 KB.

**Parse** (model, per page, per model): extracts the jurisdiction, units
(departments, divisions, boards, …) with their parent if the page shows one, and
positions (title, unit, holder, office phone/email). Output is JSON-schema
constrained at temperature 0. Long pages are chunked with the title and URL
repeated on each chunk.

**Organize** (model, per site, once it goes quiet): the model gets a numbered
list of every unit and position pooled from all pages and models. It answers
with ids only: each unit's parent, any duplicate units (`same_as`), and a unit
for positions the pages didn't place. **Code then builds the tree**, so it is
always valid:

- what a page showed (an indented list, "Division of X") is placed first;
  the model only fills in where the pages were silent
- any edge that would create a cycle is refused
- merged duplicates keep their alternate names, contacts, and positions
- if the organize call fails, the tree is still built from the page evidence

**Compare** (code): with two models, every fact records which models found it.
Dashed boxes in the chart were found by only one model. The comparison tab shows
per-page agreement on units, positions, and contacts, and exactly what each model
found that the other didn't.

## Choosing a second model

Pick one from a **different model family** than Qwen, so the two make different
mistakes and agreement means something. Mistral Small or IBM Granite are good
candidates and both are Apache-2.0. Use whatever tag `ollama list` shows:

```bash
python3 daemon/harvestd.py --models "qwen3.6,<second model tag>"
```

Each extra model adds its own parse time per page. The first model listed also
runs the organize pass.

## Settings worth knowing

| flag | default | why |
|---|---|---|
| `--num-ctx` | 16384 | Ollama **silently truncates** prompts beyond its default context window; this sets it explicitly for page parsing |
| `--organize-ctx` | 32768 | the organize pass sees a whole site |
| `--chunk-chars` | 7000 | page chunk size sent per call |
| `--quiet-seconds` | 20 | how long a site must be idle before it's organized, so a browsing burst is organized once |
| `--no-worker` | off | inbox only; start the worker later |

Qwen3 notes: requests send `think: false`, and the reply parser strips any
`<think>` block and code fences and pulls out the JSON, because some Ollama
builds mishandle the schema format when thinking is toggled.

Scraped text is untrusted. It's fenced as data in every prompt, the model is
told never to follow instructions inside it, and lines that look like
prompt-injection attempts are flagged on the page in the dashboard.

## Testing without a model

```bash
python3 tests/test_harvestd.py   # unit tests: messy JSON, merges, cycles, precedence, dedupe
bash tests/e2e.sh                # mock Ollama + daemon + a synthetic town page → org chart
```

`tests/mock_ollama.py` imitates two models that disagree slightly (one wraps
output in `<think>` like Qwen3), which is also handy for working on the
dashboard without a GPU.

## Honest limits

- Verified here: capture on real Charlottesville pages (in a DOM emulator, not
  Chrome), the full daemon pipeline against the mock, the dashboard headless,
  and the unit tests. **Not** verified: real Qwen output quality, and the plugin
  running in an actual browser. The first real run is the real test.
- Speed depends on your machine. Expect tens of seconds per page per model on a
  laptop.
- Re-indexing a page whose content hasn't changed does nothing. Changed content
  is re-parsed automatically.
- People appear only as holders of positions the site publishes. Contacts are
  the ones the site lists for the office or unit.
- The org chart is a working model for review, not a source of truth.

Apache-2.0 (see the repository LICENSE).
