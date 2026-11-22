#!/usr/bin/env python3

#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

# A minimal in-memory OpenBao/Vault KV v2 server for the tests (from
# ns8-kumomta, extended with LIST, DELETE and a sealed mode):
#   fake_openbao.py PORT TOKEN [BIND_ADDRESS]
# GET/POST /v1/<mount>/data/<path>, LIST and DELETE /v1/<mount>/metadata/<path>
# with X-Vault-Token. POST /fake/seal and /fake/unseal switch the sealed
# mode (every KV request then answers 503, as a sealed OpenBao does).

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

STORE = {}
STATE = {"sealed": False, "verbose": False}
STORE_LOCK = Lock()


def make_handler(token):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body=None):
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _key(self, kind="data"):
            parts = self.path.split("?", 1)[0].split("/")
            # ['', 'v1', mount, kind, path...]
            if len(parts) < 4 or parts[1] != "v1" or parts[3] != kind:
                return None
            return parts[2] + ":" + "/".join(parts[4:])

        def _guard(self):
            if STATE["sealed"]:
                self._reply(503, {"errors": ["Vault is sealed"]})
                return False
            if self.headers.get("X-Vault-Token") != token:
                self._reply(403, {"errors": ["permission denied"]})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            key = self._key()
            with STORE_LOCK:
                entry = STORE.get(key)
            if entry is None:
                self._reply(404, {"errors": []})
                return
            version, data = entry
            self._reply(200, {"data": {"data": data, "metadata": {"version": version}}})

        def do_POST(self):
            if self.path == "/fake/seal":
                STATE["sealed"] = True
                return self._reply(204)
            if self.path == "/fake/unseal":
                STATE["sealed"] = False
                return self._reply(204)
            if not self._guard():
                return
            key = self._key()
            if key is None:
                self._reply(404, {"errors": []})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            cas = body.get("options", {}).get("cas")
            with STORE_LOCK:
                version = STORE.get(key, (0, None))[0]
                conflict = cas is not None and cas != version
                if not conflict:
                    version += 1
                    STORE[key] = version, body.get("data") or {}
            if conflict:
                self._reply(400, {"errors": ["check-and-set parameter did not match the current version"]})
            else:
                self._reply(200, {"data": {"version": version}})

        def do_LIST(self):
            if not self._guard():
                return
            prefix = (self._key("metadata") or "").rstrip("/") + "/"
            with STORE_LOCK:
                keys = sorted({k[len(prefix):].split("/", 1)[0] + ("/" if "/" in k[len(prefix):] else "")
                               for k in STORE if k.startswith(prefix)})
            if not keys:
                self._reply(404, {"errors": []})
            else:
                self._reply(200, {"data": {"keys": keys}})

        def do_DELETE(self):
            if not self._guard():
                return
            key = self._key("metadata")
            with STORE_LOCK:
                STORE.pop(key, None)
            self._reply(204)

        def log_message(self, fmt, *args):
            if STATE["verbose"]:
                print(fmt % args, file=sys.stderr)

    return Handler


def serve(port, token, bind="127.0.0.1"):
    return ThreadingHTTPServer((bind, port), make_handler(token))


if __name__ == "__main__":
    STATE["verbose"] = True
    serve(int(sys.argv[1]), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1").serve_forever()
