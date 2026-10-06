#!/usr/bin/env python3
"""Fake municipal websites on one port, routed by Host header. Every request is
logged to --log so tests can assert what the crawler did (and didn't) fetch."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STAFF = "".join(
    f"<tr><td>{n}</td><td>{t}</td><td><a href='mailto:{e}@exampleville.gov'>Email</a></td><td>434-555-{1000+i}</td></tr>"
    for i, (n, t, e) in enumerate([("Jordan Ames", "City Manager", "manager"), ("Riley Shaw", "Chief of Police", "chief"),
                                   ("Casey Moore", "Director of Public Works", "pw"), ("Avery Lin", "City Clerk", "clerk"),
                                   ("Morgan Diaz", "Fire Chief", "fire"), ("Quinn Patel", "Finance Director", "finance"),
                                   ("Drew Kim", "Parks Director", "parks"), ("Sage Ortiz", "City Attorney", "attorney")]))

SITES = {
  "exampleville.gov": {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private\n"),
    "/": ("text/html", """<html><head><title>City of Exampleville, Virginia | Official Website</title></head><body>
       <nav><a href="/directory">Staff Directory</a> <a href="/departments">Departments</a> <a href="/news">News</a>
       <a href="/pay">Pay a Bill</a> <a href="/private/directory">Internal Directory</a>
       <a href="https://facebook.com/exampleville">Facebook</a></nav>
       <h1>Welcome to the City of Exampleville</h1><p>City Hall, 1 Main St, Exampleville, VA 22901</p>
       <footer>Government Websites by CivicPlus</footer></body></html>"""),
    "/directory": ("text/html", f"<html><head><title>Staff Directory | Exampleville, VA</title></head><body>"
                                f"<h1>Staff Directory</h1><table><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th></tr>{STAFF}</table></body></html>"),
    "/departments": ("text/html", "<html><head><title>Departments</title></head><body><h1>Departments</h1><ul>"
                                  "<li>Police Department 434-555-2000</li><li>Department of Public Works 434-555-2100</li>"
                                  "<li>Parks and Recreation Department 434-555-2200</li><li>Office of the City Clerk</li></ul></body></html>"),
    "/news": ("text/html", "<html><body>news</body></html>"),
    "/private/directory": ("text/html", "<html><body>SECRET should never be fetched</body></html>"),
  },
  "testtownva.gov": {   # a guessed domain that belongs to a same-named town in another state
    "/": ("text/html", "<html><head><title>Testtown, Ohio</title></head><body><h1>Village of Testtown</h1>"
                       "<p>Testtown, OH 44101</p></body></html>"),
  },
  "blockedburg.gov": {
    "/": (403, "text/html", "<html><head><title>Just a moment...</title></head><body>Checking your browser cf-chl</body></html>"),
  },
  "jsville.gov": {   # content only exists after JavaScript runs
    "/": ("text/html", """<html><head><title>Town of Jsville, Virginia</title><script src="/a.js"></script>
       <script src="/b.js"></script></head><body><div id="app"></div>
       <script>document.getElementById('app').innerHTML = '<h1>Town of Jsville</h1>' +
         '<a href="/staff">Staff Directory</a> <a href="/council">Town Council</a>';</script></body></html>"""),
    "/staff": ("text/html", """<html><head><title>Staff</title><script></script><script></script></head><body><div id="t"></div>
       <script>var rows=[['Pat Lee','Town Manager','manager'],['Sam Roe','Town Clerk','clerk'],['Kai Wu','Police Chief','police'],
       ['Lou Ray','Public Works Director','pw'],['Ari Fox','Treasurer','treasurer']];
       document.getElementById('t').innerHTML='<h1>Town Staff</h1><table>'+rows.map(function(r,i){return '<tr><td>'+r[0]+
       '</td><td>'+r[1]+'</td><td><a href="mailto:'+r[2]+'@jsville.gov">email</a></td><td>540-555-'+(3000+i)+'</td></tr>';}).join('')+'</table>';
       </script></body></html>"""),
    "/a.js": ("application/javascript", "//"), "/b.js": ("application/javascript", "//"),
  },
  "examplecounty.gov": {
    "/": ("text/html", """<html><head><title>Example County, Virginia</title></head><body><h1>Example County Government</h1>
       <a href="/board">Board of Supervisors</a> <a href="/contact">Contact Us</a></body></html>"""),
    "/board": ("text/html", "<html><body><h1>Board of Supervisors</h1><ul>"
                            "<li>Chair — Dana Wells — dwells@examplecounty.gov — 434-555-4000</li>"
                            "<li>Vice Chair — Lee Park — lpark@examplecounty.gov — 434-555-4001</li>"
                            "<li>Supervisor — Rae Cole — rcole@examplecounty.gov — 434-555-4002</li></ul></body></html>"),
    "/contact": ("text/html", "<html><body><h1>Contact Us</h1><p>County Administrator 434-555-4100</p></body></html>"),
  },
}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower().removeprefix("www.")
        path = self.path.split("?")[0]
        with open(self.server.logpath, "a") as f:
            f.write(json.dumps([host, path]) + "\n")
        site = SITES.get(host, {})
        entry = site.get(path)
        if entry is None:
            code, ctype, body = 404, "text/html", "<html><body>not found</body></html>"
        elif len(entry) == 3:
            code, ctype, body = entry
        else:
            code, (ctype, body) = 200, entry
        b = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype + "; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9077)
    ap.add_argument("--log", default="/tmp/mock_web.log")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.logpath = a.log
    srv.serve_forever()
