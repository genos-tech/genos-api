"""One request as one red-team identity, for reproducing a finding by hand (MIP-2267).

python poke.py --manifest manifest.json --who B_owner GET /api/v2/x/ [--query '{"a":1}'] [--json '{}']
--who: A_owner, A_viewer, B_owner, B_viewer, C_owner or anon; --auth sends a raw header instead.
RT_PASSWORD: the run secret seed.py gave every account.
"""

import argparse
import json
import os
import sys

from guard import LocalClient

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:18267")
ap.add_argument("--manifest", required=True)
ap.add_argument("--who", default="B_owner")
ap.add_argument("--query")
ap.add_argument("--json")
ap.add_argument("--auth", help="raw Authorization header value (e.g. 'ApiKey gnos_...')")
ap.add_argument("--max", type=int, default=1500)
ap.add_argument("method")
ap.add_argument("path")
a = ap.parse_args()
c = LocalClient(a.base)
m = json.load(open(a.manifest))
token = None
if a.who != "anon" and not a.auth:
    team, role = a.who.split("_")
    cred = m[team][role]
    r = c.request(
        "POST",
        "/api/v2/user/signin/",
        json_body={"email": cred["email"], "password": os.environ["RT_PASSWORD"]},
    )
    token = (r.json() or {}).get("access")
res = c.request(
    a.method,
    a.path,
    token=token,
    auth=a.auth,
    query=json.loads(a.query) if a.query else None,
    json_body=json.loads(a.json) if a.json else None,
)
print(res.status)
sys.stdout.write(res.text[: a.max] + "\n")
