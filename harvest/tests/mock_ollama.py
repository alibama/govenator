#!/usr/bin/env python3
"""Fake Ollama /api/chat for testing Harvest without a model.

It reads the page text the daemon sends and answers with plausible JSON:
  - units  = headings and nested list items
  - positions = table rows "Name | Title | email | phone"
A model whose name contains "second" answers differently (drops every 4th position,
wraps output in <think>...</think> like Qwen3) so the comparison view has something
to compare. The organize call nests anything called "...Division" under the police
department.

    python3 tests/mock_ollama.py --port 11555
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMAIL = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
PHONE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}-\d{4}")


def fake_extract(page, second):
    units, positions = [], []
    title = (re.search(r"^# (.+)$", page, re.M) or [None, ""])[1]
    jurisdiction = title.split("•")[-1].strip() if "•" in title else ""
    last_top = ""
    for m in re.finditer(r"^( *)- (.+)$", page, re.M):
        name, depth = m.group(2).strip(), len(m.group(1))
        if len(name) > 70:
            continue
        if depth <= 4:
            last_top = name
            units.append({"name": name, "parent": "", "kind": "department"})
        else:
            units.append({"name": name, "parent": last_top, "kind": "division"})
    rows = [r for r in page.splitlines() if " | " in r]
    for i, r in enumerate(rows):
        c = [x.strip() for x in r.split(" | ")]
        if len(c) < 2 or c[0].lower() in ("name", "categories"):
            continue
        if second and i % 4 == 0:
            continue
        em = EMAIL.search(r)
        ph = PHONE.search(r)
        unit = "Police Department" if re.search(r"police|patrol", c[1], re.I) else ""
        positions.append({"title": c[1], "unit": unit,
                          "holder": c[0], "phone": ph.group(0) if ph else "", "email": em.group(1) if em else ""})
    return {"jurisdiction": jurisdiction, "units": units, "positions": positions}


def fake_organize(prompt):
    units = re.findall(r"^(U\d+): ([^\[\n]+?)(?: \[|$| --)", prompt, re.M)
    police = next((i for i, n in units if n.strip().lower() == "police department"), "")
    out = []
    for i, n in units:
        parent = police if (police and i != police and re.search(r"division|bureau", n, re.I)) else ""
        out.append({"id": i, "parent": parent, "same_as": ""})
    by_name = {n.strip().lower(): i for i, n in units}
    pos = []
    for p, t in re.findall(r"^(P\d+): ([^\n]+?)(?: --|$)", prompt, re.M):
        guess = next((i for n, i in by_name.items() if n.split()[0] in t.lower()), "")
        pos.append({"id": p, "unit": guess})
    return {"jurisdiction": "", "units": out, "positions": pos}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        second = "second" in body["model"]
        if "org chart" in system:
            data = fake_organize(user)
        else:
            page = user.split("<<<PAGE>>>", 1)[-1].rsplit("<<<END>>>", 1)[0]
            data = fake_extract(page, second)
        content = json.dumps(data)
        if second:
            content = "<think>Let me read the page carefully.</think>\nHere is the JSON:\n" + content
        b = json.dumps({"model": body["model"], "message": {"role": "assistant", "content": content}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=11555)
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
