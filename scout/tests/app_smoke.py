"""Drive the app headless: map metrics, one Needs-you answer, the export preview.
   SCOUT_DB=<db built by tests/e2e.sh> python3 tests/app_smoke.py"""
import os, sys
from streamlit.testing.v1 import AppTest
mode = sys.argv[1] if len(sys.argv) > 1 else "local"
at = AppTest.from_file("../app.py", default_timeout=180).run()
def errs(): return [str(e.value)[:300] for e in at.exception]
e = errs()
print(f"[{mode}] first load exceptions:", e or "none", "| maps:", len(at.get("plotly_chart")), "| metrics:", len(at.metric))
for m in ["Average score", "Coverage", "Progress", "County government"]:
    at.radio[0].set_value(m).run(); e += errs()
# Needs you: enter a name, take the first item, answer it
next(t for t in at.text_input if t.label == "Your name").set_value("anson").run()
heading = [m.value for m in at.markdown if m.value.startswith("### ")]
print(f"[{mode}] needs-you item:", heading)
# inside a form, set every field and then submit once (that's how Streamlit forms deliver values)
next(r for r in at.radio if r.label == "What did you find?").set_value("Found the official website")
next(t for t in at.text_input if t.label == "Official website").set_value("https://www.example.org/")
next(t for t in at.text_area if t.label.startswith("Social media links")).set_value("https://facebook.com/SomeTownVA")
next(c for c in at.checkbox if c.label == "Check the website now").uncheck()
next(b for b in at.button if b.label == "Save and next").click().run(); e += errs()
heading2 = [m.value for m in at.markdown if m.value.startswith("### ")]
print(f"[{mode}] after save -> next item:", heading2)
# Export tab content
codes = [c.value for c in at.code if "P856" in (c.value or "")]
print(f"[{mode}] export QS preview:", codes[0].splitlines()[:3] if codes else "none")
print(f"[{mode}] all exceptions:", e or "none")

assert not e, e
assert heading and heading2 and heading != heading2, "Needs-you did not advance"
assert codes and "P856" in codes[0], "export preview missing"
print(f"[{mode}] APP SMOKE OK")
