#!/usr/bin/env python3

#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Test client: run SQL as a consumer, with the grant-database output on
# stdin. Runs as a module user (runagent) and uses the module's runtime
# image for psql; the password goes through a temporary 0600 pgpass file.
#   pg-probe.py SQL [PORT_KEY] [SSLMODE]
#   PORT_KEY: port (default) or ro_port; SSLMODE: libpq sslmode (prefer)
# Output: {"ok": bool, "output": "...", "error": "..."}

import json
import os
import subprocess
import sys
import tempfile

grant = json.load(sys.stdin)
sql = sys.argv[1]
port = grant[sys.argv[2] if len(sys.argv) > 2 else "port"]
sslmode = sys.argv[3] if len(sys.argv) > 3 else "prefer"
tmp = tempfile.mkdtemp()
try:
    pgpass = os.path.join(tmp, "pgpass")
    fd = os.open(pgpass, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as fp:
        fp.write(f"*:*:*:{grant['username']}:{grant['password']}\n")
    proc = subprocess.run(["podman", "run", "--rm", "-i", "--network=host", "--entrypoint=psql",
                           f"--volume={tmp}:/probe:z", "--env=PGPASSFILE=/probe/pgpass", "--env=PGCONNECT_TIMEOUT=5",
                           f"--env=PGSSLMODE={sslmode}",
                           os.environ["POSTGRESQL19C_SERVER_IMAGE"], "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                           "-h", grant["host"], "-p", str(port), "-U", grant["username"], "-d", grant["database"]],
                          input=sql, capture_output=True, text=True, timeout=120)
    print(json.dumps({"ok": proc.returncode == 0, "output": proc.stdout.strip(), "error": proc.stderr.strip()[-500:]}))
finally:
    for f in os.listdir(tmp):
        os.unlink(os.path.join(tmp, f))
    os.rmdir(tmp)
