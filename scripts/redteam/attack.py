"""Authorized local red-team run against a two-tenant copy of genos-api (MIP-2267).

Scope and guardrails: the Decision note on MIP-2267. Local target only (`guard.check_local`), the
credential-free test environment (no AI call can bill), a throwaway database.

    python attack.py --base http://127.0.0.1:18267 --manifest manifest.json \
        --routes routes.jsonl --team-a team_A.json --team-b team_B.json --out results.json \
        [--phase setup,idor,auth,mcp]

Team B (the attacker, Japanese sample workspace) tries team A's ids (the victim, English workspace).
A probe counts as a LEAK when a team-B response carries team A's own text (a `marker`: a title,
name or body that exists only in team A) or one of team A's object ids that the request did not
send. Every read is first made as team A's owner (the positive control), so a refusal to B on a
route that also refuses A says nothing and is reported as `inconclusive`, not as `held`.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys

from guard import LocalClient, check_local

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Routes whose write acts on the CALLER (signing out, deleting the account, changing the password):
# a cross-tenant write there means nothing and would end the attacker's own session mid-run.
CALLER_ONLY = re.compile(
    r"signout|sign-out|logout|delete-account|account/delete|deactivate|password|revoke-all|"
    r"sessions/revoke|user/delete|leave|unsubscribe|demo|signup|signin|token/refresh|"
    r"oauth/|\.well-known|webhook/$|stripe|github/app|billing|checkout|erase|export",
    re.I,
)


def _redis_flush(db: int = 13, port: int = 16379) -> bool:
    """Reset the local target's throttle counters (its own Redis db) so a 429 doesn't hide a result.
    Local only: 127.0.0.1, the target's db number."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(
                f"*2\r\n$6\r\nSELECT\r\n${len(str(db))}\r\n{db}\r\n*1\r\n$7\r\nFLUSHDB\r\n".encode()
            )
            return b"+OK" in s.recv(100)
    except OSError:
        return False


class Run:
    def __init__(self, args):
        self.args = args
        self.c = LocalClient(args.base)
        self.m = json.load(open(args.manifest))
        self.a = json.load(open(args.team_a))
        self.b = json.load(open(args.team_b))
        self.routes = [json.loads(line) for line in open(args.routes) if line.startswith("{")]
        self.tokens: dict[str, str] = {}
        self.created: dict[str, dict] = {"A": {}, "B": {}}
        self.results: dict = {
            "run": self.m["run"],
            "findings": [],
            "probes": [],
            "notes": [],
            "stats": {},
        }
        self.throttled = 0
        b_text = json.dumps(self.b)
        # Team A markers that never appear in team B's own data (the demo seeds both teams from the
        # same story in two languages; a shared proper noun would be a false alarm).
        self.markers = [mk for mk in self.a["markers"] if mk not in b_text and len(mk) >= 18]
        self.markers += [mk for mk in self.a.get("team_markers", []) if mk not in b_text]
        self.a_ids = {
            self.m["A"]["team_id"],
            self.m["A"]["owner"]["id"],
            self.m["A"]["viewer"]["id"],
        }
        for obj in self.a["objects"].values():
            if obj["pk_type"] == "uuid":
                self.a_ids.update(obj["ids"])

    # ---------------------------------------------------------------- helpers
    def note(self, text):
        print(text, flush=True)
        self.results["notes"].append(text)

    def finding(self, severity, title, **detail):
        row = {"severity": severity, "title": title, **detail}
        print(f"!! [{severity}] {title} {json.dumps(detail)[:300]}", flush=True)
        self.results["findings"].append(row)

    def req(self, method, path, who=None, retry=True, **kw):
        token = self.tokens.get(who) if who else None
        res = self.c.request(method, path, token=token, **kw)
        if res.status == 429 and retry:
            self.throttled += 1
            _redis_flush()
            res = self.c.request(method, path, token=token, **kw)
        return res

    def leak(self, body: str, sent: str) -> list[str]:
        hits = [mk for mk in self.markers if mk in body]
        hits += [i for i in self.a_ids if i in body and i not in sent]
        return hits[:5]

    def a_id(self, model, n=0):
        obj = self.a["objects"].get(f"origin.{model}")
        if not obj or len(obj["ids"]) <= n:
            return None
        return obj["ids"][n]

    # ---------------------------------------------------------------- sign in
    def signin(self):
        for team, roles in (
            ("A", ("owner", "viewer")),
            ("B", ("owner", "viewer")),
            ("C", ("owner",)),
        ):
            for role in roles:
                who = f"{team}_{role}"
                cred = self.m[team][role]
                res = self.req(
                    "POST",
                    "/api/v2/user/signin/",
                    json_body={"email": cred["email"], "password": cred["password"]},
                )
                data = res.json() or {}
                token = data.get("access") or (data.get("data") or {}).get("access")
                if not token:
                    self.note(f"sign-in failed for {who}: {res.status} {res.text[:200]}")
                    continue
                self.tokens[who] = token
        self.note(f"signed in: {sorted(self.tokens)}")

    # ---------------------------------------------------------------- setup objects not in the seed
    def setup(self):
        for team in ("A", "B"):
            who = f"{team}_owner"
            tid = self.m[team]["team_id"]
            made = self.created[team]
            for scope in ("read", "write"):
                res = self.req(
                    "POST",
                    "/api/v2/api-keys/",
                    who,
                    json_body={"name": f"rt-{scope}", "scope": scope},
                )
                d = res.json() or {}
                if d.get("key"):
                    made[f"key_{scope}"] = d["key"]
                    made[f"key_{scope}_id"] = str(d.get("id") or d.get("key_id") or "")
                else:
                    self.note(f"setup: {team} api key {scope}: {res.status} {res.text[:160]}")
            res = self.req(
                "POST",
                "/api/v2/api-keys/",
                who,
                json_body={"name": "rt-team", "scope": "write", "team_id": tid},
            )
            d = res.json() or {}
            if d.get("key"):
                made["key_team"] = d["key"]
                made["key_team_id"] = str(d.get("id") or "")
            res = self.req(
                "POST",
                "/api/v2/webhooks/",
                who,
                json_body={
                    "team_id": tid,
                    "url": "https://example.com/rt-hook",
                    "events": ["task.created"],
                },
            )
            d = res.json() or {}
            made["webhook_id"] = str(d.get("id") or (d.get("webhook") or {}).get("id") or "")
            if not made["webhook_id"]:
                self.note(f"setup: {team} webhook: {res.status} {res.text[:160]}")
            res = self.req(
                "POST",
                "/api/v2/skills/",
                who,
                json_body={
                    "team_id": tid,
                    "title": f"RT {team} private skill {self.m['run']}",
                    "description": f"Red-team canary skill of team {team}, private to its owner",
                    "bodyMarkdown": f"RTCANARY-{team}-SKILL-{self.m['run']} secret instructions of team {team}",
                    "visibility": "private",
                },
            )
            d = res.json() or {}
            sk = d.get("skill") if isinstance(d.get("skill"), dict) else d
            made["skill_id"] = str((sk or {}).get("id") or (sk or {}).get("skill_id") or "")
            if not made["skill_id"]:
                self.note(f"setup: {team} skill: {res.status} {res.text[:200]}")
            res = self.req(
                "POST",
                "/api/v2/schedules/",
                who,
                json_body={
                    "team_id": tid,
                    "name": f"RT {team} schedule",
                    "job": "what_to_do_today",
                    "frequency": "daily",
                    "time_of_day": "09:00",
                },
            )
            d = res.json() or {}
            made["schedule_id"] = str(d.get("id") or (d.get("schedule") or {}).get("id") or "")
            if not made["schedule_id"]:
                self.note(f"setup: {team} schedule: {res.status} {res.text[:160]}")
        with open(self.args.out + ".created.json", "w") as fh:
            json.dump(self.created, fh)
        self.adopt_created()

    def adopt_created(self):
        # The canary markers created through the API belong to team A too.
        self.markers.append(f"RTCANARY-A-SKILL-{self.m['run']}")
        for k in (
            "key_read_id",
            "key_write_id",
            "key_team_id",
            "webhook_id",
            "skill_id",
            "schedule_id",
        ):
            if self.created["A"].get(k):
                self.a_ids.add(self.created["A"][k])
        self.note(
            "setup made: "
            + json.dumps({t: sorted(k for k, v in d.items() if v) for t, d in self.created.items()})
        )

    # ---------------------------------------------------------------- the IDOR walk
    def kwarg_value(self, name, kind, note_type=None):
        A = self.m["A"]
        made = self.created["A"]
        table = {
            "task_id": self.a_id("TaskMaster"),
            "project_id": self.a_id("ProjectMaster"),
            "milestone_id": self.a_id("MilestoneMaster"),
            "sprint_id": self.a_id("Sprint"),
            "folder_id": self.a_id("PersonalNoteFolder"),
            "channel_id": self.a_id("Channel"),
            "chat_id": self.a_id("Channel"),
            "message_id": self.a_id("Message"),
            "comment_id": self.a_id("TaskComments"),
            "category_id": self.a_id("ToDoCategory"),
            "dependency_id": self.a_id("TaskDependency"),
            "group_id": self.a_id("MentionGroupMaster"),
            "label_id": self.a_id("ProjectLabel"),
            "user_id": A["owner"]["id"],
            "team_id": A["team_id"],
            "version_no": "1",
            "skill_id": made.get("skill_id"),
            "key_id": made.get("key_write_id"),
            "webhook_id": made.get("webhook_id"),
        }
        if name == "item_id":
            return self.a_id("ToDoItem")
        if name == "tag_id":
            return self.a_id("ProjectTags")
        if name == "schedule_id":
            return made.get("schedule_id") if kind == "uuid" else None
        if name == "note_id":
            model = {"task": "TaskNoteMaster", "chat": "ChatNoteMaster"}.get(
                note_type or "personal", "PersonalNoteMaster"
            )
            return self.a_id(model)
        if name == "note_type":
            return note_type or "personal"
        if name == "activity_id" and kind == "int":
            return self.a_id("TaskActivity")
        return table.get(name)

    def param_values(self):
        """Team A's value for every id-like request parameter name the views read."""
        A = self.m["A"]
        made = self.created["A"]
        user = A["owner"]["id"]
        task = self.a_id("TaskMaster")
        vals = {
            "team_id": A["team_id"],
            "team": A["team_id"],
            "default_team_id": A["team_id"],
            "target_team_id": A["team_id"],
            "project_id": self.a_id("ProjectMaster"),
            "project": self.a_id("ProjectMaster"),
            "task_id": task,
            "task": task,
            "object_id": task,
            "blocked_task_id": task,
            "blocker_task_id": self.a_id("TaskMaster", 1),
            "task_ids": [task],
            "channel_id": self.a_id("Channel"),
            "chat_id": self.a_id("Channel"),
            "thread_id": self.a_id("Message"),
            "thread_root_id": self.a_id("Message"),
            "message_id": self.a_id("Message"),
            "last_read_message_id": self.a_id("Message"),
            "note_id": self.a_id("PersonalNoteMaster"),
            "note": self.a_id("PersonalNoteMaster"),
            "parent_note_id": self.a_id("PersonalNoteMaster"),
            "folder_id": self.a_id("PersonalNoteFolder"),
            "parent_folder_id": self.a_id("PersonalNoteFolder"),
            "milestone_id": self.a_id("MilestoneMaster"),
            "sprint_id": self.a_id("Sprint"),
            "comment_id": self.a_id("TaskComments"),
            "item_id": self.a_id("ToDoItem"),
            "parent_item_id": self.a_id("ToDoItem"),
            "up_to_item_id": self.a_id("ToDoItem"),
            "category_id": self.a_id("ToDoCategory"),
            "group_id": self.a_id("MentionGroupMaster"),
            "skill_id": made.get("skill_id"),
            "user_id": user,
            "attendee_id": user,
            "owner_id": user,
            "owner_user_id": user,
            "target_user_id": user,
            "other_user_id": user,
            "reporter_id": user,
            "sender_id": user,
            "receiver_id": user,
            "user_ids": [user],
            "member_user_ids": [user],
            "assignee_ids": [user],
            "mentioned_user_ids": [user],
            "note_type": "personal",
        }
        return {k: v for k, v in vals.items() if v is not None}

    def spray(self, route=None):
        vals = self.param_values()
        if route is None or not route.get("params"):
            keys = (
                "team_id",
                "project_id",
                "task_id",
                "channel_id",
                "chat_id",
                "note_id",
                "folder_id",
                "milestone_id",
                "user_id",
            )
            return {k: vals[k] for k in keys if k in vals}
        out = {k: vals[k] for k in route["params"] if k in vals}
        if "id" in route["params"]:
            out["id"] = vals.get("task_id")
        out.setdefault("team_id", vals["team_id"])
        return out

    def build(self, route):
        path = route["route"]
        variants = []
        note_types = ["personal", "task", "chat"] if "note_type" in route["kwargs"] else [None]
        for nt in note_types:
            p = path
            ok = True
            for name, kind in route["kwargs"].items():
                val = self.kwarg_value(name, kind, nt)
                if val is None:
                    ok = False
                    break
                p = re.sub(r"<(?:[^>:]+:)?%s>" % re.escape(name), str(val), p)
            if ok and "<" not in p and "(" not in p:
                variants.append(p)
        return variants

    def idor(self, do_writes):
        stats = {
            "tried": 0,
            "leak": 0,
            "same_as_victim": 0,
            "held": 0,
            "inconclusive": 0,
            "skipped": 0,
            "write_2xx": 0,
        }
        for route in self.routes:
            spray = self.spray(route)
            if route["route"].startswith("/media") or "<path" in route["route"]:
                continue
            paths = self.build(route)
            if not paths:
                stats["skipped"] += 1
                continue
            for path in paths:
                for method in route["methods"]:
                    if method in WRITE_METHODS and not do_writes:
                        continue
                    if method in WRITE_METHODS and (
                        route["access"] in ("selfonly", "signedin", "public")
                        or CALLER_ONLY.search(path)
                    ):
                        continue
                    if (
                        method not in WRITE_METHODS
                        and CALLER_ONLY.search(path)
                        and "export" not in path
                    ):
                        continue
                    stats["tried"] += 1
                    kw = {"query": spray} if method in ("GET", "DELETE") else {"json_body": spray}
                    sent = path + json.dumps(spray)
                    control = None
                    ctl_body = None
                    if method == "GET":
                        ctl = self.req(method, path, "A_owner", **kw)
                        ctl_body = ctl.body
                        control = {"status": ctl.status, "markers": bool(self.leak(ctl.text, sent))}
                    for who in ("B_owner", "anon") if method == "GET" else ("B_owner",):
                        res = self.req(method, path, None if who == "anon" else who, **kw)
                        hits = self.leak(res.text, sent) if res.status < 500 else []
                        probe = {
                            "who": who,
                            "method": method,
                            "path": path,
                            "status": res.status,
                            "control": control,
                        }
                        if hits:
                            stats["leak"] += 1
                            probe["hits"] = hits
                            self.finding(
                                "High" if who == "B_owner" else "Critical",
                                f"{who} read team A data via {method} {route['route']}",
                                path=path,
                                status=res.status,
                                hits=hits,
                                view=route["view"],
                            )
                        elif (
                            route["access"] == "checked"
                            and control
                            and 200 <= control["status"] < 300
                            and 200 <= res.status < 300
                            and ctl_body
                            and len(ctl_body) > 80
                            and res.body == ctl_body
                        ):
                            # Byte-identical to what the victim's owner sees on a team-scoped route.
                            stats["same_as_victim"] += 1
                            probe["same_as_victim"] = True
                            probe["body"] = res.text[:300]
                        elif method in WRITE_METHODS and 200 <= res.status < 300:
                            stats["write_2xx"] += 1
                            probe["body"] = res.text[:300]
                        elif control and (control["status"] >= 400 or not control["markers"]):
                            stats["inconclusive"] += 1
                        else:
                            stats["held"] += 1
                        if res.status >= 500:
                            probe["body"] = res.text[:200]
                        self.results["probes"].append(probe)
        stats["throttled_retries"] = self.throttled
        self.results["stats"]["idor_writes" if do_writes else "idor_reads"] = stats
        self.note(f"idor ({'writes' if do_writes else 'reads'}): {stats}")

    # ---------------------------------------------------------------- save
    def save(self):
        with open(self.args.out, "w") as fh:
            json.dump(self.results, fh, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--routes", required=True)
    ap.add_argument("--team-a", required=True)
    ap.add_argument("--team-b", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--phase", default="setup,idor")
    ap.add_argument("--created", help="the .created.json of an earlier run's setup phase")
    ap.add_argument(
        "--extra", default="", help="which probe groups of the extra phase (default all)"
    )
    args = ap.parse_args()
    check_local(args.base)
    run = Run(args)
    phases = args.phase.split(",")
    run.signin()
    try:
        if "setup" in phases:
            run.setup()
        elif args.created:
            run.created = json.load(open(args.created))
            run.adopt_created()
        if "idor" in phases:
            run.idor(do_writes=False)
        if "idor-writes" in phases:
            run.idor(do_writes=True)
        if "extra" in phases:
            import probes

            probes.run_all(run)
    finally:
        run.save()
    print(f"findings: {len(run.results['findings'])}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
