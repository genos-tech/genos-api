# Local red-team harness (MIP-2267)

An authorized, **local-only** attack run against a throwaway two-tenant copy of genos-api. Scope
and guardrails: the Decision note on MIP-2267 ("scope and guardrails of the authorized local AI
red-team"). Never point any of this at production or a real domain: every request goes through
`guard.LocalClient`, which refuses any host other than `localhost` / `127.0.0.1` / `::1`, refuses
credentials in the URL and never follows a redirect (`test_guard.py` pins it).

## What it does

| File | Runs where | What |
| --- | --- | --- |
| `seed.py` | inside the target (`manage.py shell`) | Three tenants: A (victim, English sample workspace), B (attacker, Japanese sample workspace, so any English sample text in a B response is A's), C (control). Refuses unless the database is `origin_rt*` on a local host. Prints a manifest with random passwords (keep it in a 0600 temp file, never in the repo). |
| `inventory.py` | inside the target | One team's object ids in every table, a digest of its rows, and marker strings of its own text. Run before and after an attack to prove nothing of team A changed. |
| `dump_routes.py` | inside the target | Every routed view: path kwargs, methods, the `route_access` kind, and the request parameter names its source reads. |
| `attack.py` | a sibling container | Sign-in, setup (API keys, webhooks, skills), the IDOR walk (reads with a team-A positive control, then writes), and `--phase extra` for `probes.py`. |
| `probes.py` | (imported) | MCP tool-by-tool calls with team A's ids, auth bypass (forged/`alg: none` tokens, keys on the wrong surface, read-scoped writes, revoked keys, token after sign-out), viewer privilege escalation, stored-text fences, display-id collisions, public skills, media files and GPS EXIF. |
| `poke.py` | a sibling container | One request as one identity, to reproduce a finding by hand. |

## Run it

The target uses the credential-free `docker/test.env` (dummy LLM keys: no AI call can bill) with
DEBUG off, its own database, OpenSearch index and Redis db. On Docker Desktop, `--network host`
is the VM's loopback, so the harness runs in a second container on the same network.

1. Env: copy `genos-platform/docker/test.env`, set `DJANGO_DEBUG=false`, fresh random
   `DJANGO_SECRET_KEY` / `JWT_SECRET_KEY` / `FIELD_ENCRYPTION_KEYS` / `BLIND_INDEX_KEYS`,
   `ALLOWED_HOSTS=127.0.0.1,localhost`, `DATABASE_URL=postgres://...@127.0.0.1:15432/origin_rt<N>`,
   `REDIS_URL=redis://127.0.0.1:16379/13`, `OPENSEARCH_INDEX=rt<N>_chunks`, `GENOS_SKILLS=true`.
   Production settings refuse plain-http OpenSearch; for the local copy only, a settings module
   outside the repo can relax just that check (`build_client_kwargs(cfg, debug=True)`) and then
   `from apis.settings import *`.
2. Start the target: `migrate`, then `gunicorn apis.wsgi:application --bind 127.0.0.1:18267`.
3. Seed and inventory (pipe scripts over stdin; Docker Desktop's file share can serve a stale copy
   of an edited file):
   `docker exec -i <target> python manage.py shell < scripts/redteam/seed.py > manifest.json`
   `docker exec -i -e RT_TEAM=<team uuid> <target> python manage.py shell < scripts/redteam/inventory.py > team_A.json`
   `docker exec -i <target> python manage.py shell < scripts/redteam/dump_routes.py > routes.jsonl`
4. Attack from a sibling container with the scripts and those files copied in (`docker cp`):
   `python attack.py --base http://127.0.0.1:18267 --manifest manifest.json --routes routes.jsonl --team-a team_A.json --team-b team_B.json --out results.json --phase setup,idor`
   then `--phase idor-writes --created results.json.created.json`, then `--phase extra`.
   Re-run `inventory.py` for team A and compare the digests.
5. Teardown: remove both containers and drop the `origin_rt<N>` database and the index.

Sockets and collab: collab's access check is the api route `collab/access/check/` (walked here);
the sockets service relays notices through api routes that the walk covers. Neither service was
run live in the first pass.
