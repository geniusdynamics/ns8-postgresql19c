#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""In-process fake of the OpenBao HTTP API and of the ns8-openbao client
provisioning actions, for unit tests (no network, no agent package).

Shared with ns8-kumomta (tests/fake_bao_api.py), plus the database\nengine mount ns8-db-<module id>. Adapted from\ntests/unit/test_client_provisioning.py of ns8-openbao: response
wrapping (single use, creation_path), entity alias confinement of the
templated policies (ns8-kv/*/modules/<alias>/*, groups/<alias>/*),
renew-self, revoke-self and KV version 2.
"""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WRAP_PATH = "auth/token/create/ns8-client"


class FakeBao:
    def __init__(self):
        self.tokens = {"root": {"alias": "", "kind": "root"}}
        self.wraps = {}
        self.kv = {}          # (mount, path) -> data
        self.requests = []
        self.database = {}    # mount -> [(method, kind, name, body)]
        self.approved = {}    # module_id -> {"database_engine": bool, "groups": [...]}
        self.pending = {}
        self.enabled = True
        self.lease = 86400
        self.sealed = False
        self.wrap_path = WRAP_PATH
        self.server = None

    # -- HTTP --------------------------------------------------------------
    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                status, payload = fake.handle(method, self.path[len("/v1/"):], self.headers, body)
                raw = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._any("GET")

            def do_POST(self):
                self._any("POST")

            def do_DELETE(self):
                self._any("DELETE")

            def do_LIST(self):
                self._any("LIST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def address(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _allowed(self, token, mount, path):
        info = self.tokens.get(token)
        if not info:
            return False
        if info["kind"] == "root":
            return True
        if info["kind"] == "module":
            return mount == "ns8-kv" and path.startswith(f"modules/{info['alias']}/")
        if info["kind"] == "group":
            return mount == "ns8-kv" and path.startswith(f"groups/{info['alias']}/")
        return False

    def handle(self, method, path, headers, body):
        self.requests.append((method, path))
        token = headers.get("X-Vault-Token", "")
        if self.sealed:
            return 503, {"errors": ["sealed"]}
        if path == "sys/wrapping/lookup":
            wrap = self.wraps.get(body.get("token"))
            return (200, {"data": {"creation_path": wrap["path"]}}) if wrap else (400, {"errors": ["invalid"]})
        if path == "sys/wrapping/unwrap":
            wrap = self.wraps.pop(token, None)
            if not wrap:
                return 400, {"errors": ["wrapping token is not valid"]}
            return 200, {"auth": {"client_token": wrap["token"], "lease_duration": self.lease}}
        if path == "auth/token/renew-self":
            if token not in self.tokens:
                return 403, {"errors": ["permission denied"]}
            return 200, {"auth": {"client_token": token, "lease_duration": self.lease}}
        if path == "auth/token/revoke-self":
            self.tokens.pop(token, None)
            return 204, None
        mount, _, rest = path.partition("/")
        kind, _, key = rest.partition("/")
        if mount.startswith("ns8-db-") and kind in ("config", "rotate-root", "roles"):
            info = self.tokens.get(token) or {}
            grant = self.approved.get(info.get("alias")) or {}
            if info.get("kind") != "root" and not (info.get("kind") == "module" and mount == f"ns8-db-{info['alias']}"
                                                   and grant.get("database_engine")):
                return 403, {"errors": ["permission denied"]}
            self.database.setdefault(mount, []).append((method, kind, key, body))
            return 204, None
        if kind in ("data", "metadata"):
            if not self._allowed(token, mount, key):
                return 403, {"errors": ["permission denied"]}
            if method == "GET":
                if (mount, key) not in self.kv:
                    return 404, {"errors": []}
                return 200, {"data": {"data": self.kv[(mount, key)]}}
            if method == "POST":
                if (body.get("options") or {}).get("cas") == 0 and (mount, key) in self.kv:
                    return 400, {"errors": ["check-and-set parameter did not match"]}
                self.kv[(mount, key)] = body["data"]
                return 200, {}
            if method == "DELETE":
                self.kv.pop((mount, key), None)
                return 204, None
            if method == "LIST":
                keys = sorted({k[len(key):].split("/")[0] for m, k in self.kv if m == mount and k.startswith(key)})
                return (200, {"data": {"keys": keys}}) if keys else (404, {})
        return 404, {"errors": ["unsupported"]}

    # -- ns8-openbao actions (agent.tasks.run) -------------------------------
    def _wrap(self, kind, alias):
        token = "s." + secrets.token_hex(8)
        self.tokens[token] = {"alias": alias, "kind": kind}
        wrapper = "s.wrap" + secrets.token_hex(8)
        self.wraps[wrapper] = {"token": token, "path": self.wrap_path}
        return wrapper

    def revoke(self, module_id):
        self.approved.pop(module_id, None)
        for token, info in list(self.tokens.items()):
            if info["kind"] == "module" and info["alias"] == module_id:
                del self.tokens[token]

    def _error(self, code):
        return {"exit_code": 3, "output": [{"field": "module_id", "parameter": "module_id", "value": "",
                                            "error": code}]}

    def _status(self, module_id):
        grant = self.approved.get(module_id)
        req = self.pending.get(module_id) or {}
        return {
            "module_id": module_id, "client_provisioning": self.enabled, "ready": self.enabled,
            "status": "approved" if grant is not None else ("pending" if module_id in self.pending else "none"),
            "database_engine": bool(grant and grant.get("database_engine")),
            "database_engine_requested": bool(req.get("database_engine")),
            "groups": list((grant or {}).get("groups", [])),
            "groups_requested": list(req.get("groups", [])),
            "address": self.address, "address_type": "vpn",
            "kv_mount": "ns8-kv", "kv_prefix": f"modules/{module_id}",
        }

    def runner(self, module_id):
        """An agent.tasks.run replacement calling as module/<module_id>"""
        def run(agent_id, action, data=None, extra=None):
            data = data or {}
            if not self.enabled:
                return self._error("client_provisioning_disabled")
            if self.sealed and action == "provision-client":
                return self._error("openbao_unavailable")
            if action == "get-client-status":
                return {"exit_code": 0, "output": self._status(module_id)}
            if action == "request-client-access":
                req = self.pending.setdefault(module_id, {"database_engine": False, "groups": []})
                req["database_engine"] = req["database_engine"] or bool(data.get("database_engine"))
                req["groups"] = sorted(set(req["groups"]) | set(data.get("groups") or []))
                return {"exit_code": 0, "output": self._status(module_id)}
            if action == "provision-client":
                grant = self.approved.get(module_id)
                if grant is None:
                    self.pending.setdefault(module_id, {"database_engine": bool(data.get("database_engine")),
                                                        "groups": []})
                    return self._error("client_not_approved")
                with_db = bool(grant.get("database_engine"))
                return {"exit_code": 0, "output": {
                    "module_id": module_id, "address": self.address, "address_type": "vpn",
                    "wrapped_token": self._wrap("module", module_id), "wrap_ttl": 120,
                    "wrap_creation_path": WRAP_PATH, "kv_mount": "ns8-kv", "kv_version": 2,
                    "kv_prefix": f"modules/{module_id}", "database_engine": with_db,
                    "database_mount": f"ns8-db-{module_id}" if with_db else "",
                    "groups": [{"group_id": g, "kv_prefix": f"groups/{g}", "wrapped_token": self._wrap("group", g),
                                "wrap_ttl": 120} for g in grant.get("groups", [])],
                }}
            return {"exit_code": 1, "output": ""}
        return run
