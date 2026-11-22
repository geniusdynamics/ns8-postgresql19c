#!/usr/bin/env python3
#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Robot helper, run inside the module (runagent -m <id> python3 ...):
drive provisioned mode with the module's own Python and pypkg against the
in-process fake of tests/unit/fake_bao_api.py, in a scratch state
directory (the instance state is not touched). Prints a JSON summary."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_bao_api import FakeBao  # noqa: E402
import openbao_client  # noqa: E402
import pgha_secrets as ps  # noqa: E402

module = os.environ["MODULE_ID"]
os.chdir(tempfile.mkdtemp())
os.environ.update({"AGENT_STATE_DIR": os.getcwd(), "PG_VAULT_ENABLED": "1",
                   "PG_VAULT_AUTH": "provisioned", "PG_VAULT_PROVIDER": "openbao1"})
bao = FakeBao()
bao.start()
run = bao.runner(module)
out = {"group": ps.cluster_group()}
out["first"] = ps.client_step(run=run)[0]
bao.approved[module] = {"database_engine": True, "groups": [ps.cluster_group()]}
out["approved"] = ps.client_step(run=run)[0]
mgr = ps.manager()
shared = ps.SHARED_SECRETS[0]
mgr.set_secret(shared, "robot-shared")
mgr.set_secret("PGADMIN_PASSWORD", "robot-own")
out["group_path"] = ("ns8-kv", f"groups/{out['group']}/{shared}") in bao.kv
out["module_path"] = ("ns8-kv", f"modules/{module}/PGADMIN_PASSWORD") in bao.kv
client = openbao_client.public_state()
out["database_mount"] = client["database_mount"]
ps.register_database_engine(mgr.vault, client["database_mount"], "robot", "127.0.0.1", 5432, "pw", ["*"],
                            provisioned=True)
out["engine_calls"] = len(bao.database.get(client["database_mount"], []))
mode = os.stat(openbao_client._dir()).st_mode & 0o777
out["dir_mode"] = oct(mode)
bao.revoke(module)
openbao_client.mark_reprovision()
out["revoked"] = ps.client_step(run=run)[0]
out["fallback_local"] = ps.manager().vault is None and ps.manager().get_secret("PGADMIN_PASSWORD") == "robot-own"
bao.stop()
json.dump(out, sys.stdout)
