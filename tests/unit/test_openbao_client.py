#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Unit tests of imageroot/pypkg/openbao_client.py (client side of the
ns8-openbao client provisioning) against an in-process fake OpenBao.

    python3 -m unittest discover -s tests/unit -p "test_*.py"
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "imageroot/pypkg"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import openbao_client as obc  # noqa: E402
from fake_bao_api import FakeBao  # noqa: E402

MODULE = "postgresql19c1"


class OpenBaoClientTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = temp.name
        self.bao = FakeBao()
        self.bao.start()
        self.addCleanup(self.bao.stop)
        self.run_task = self.bao.runner(MODULE)

    def provision(self, **kwargs):
        return obc.provision("openbao1", state_dir=self.state, run=self.run_task, **kwargs)

    def test_request_then_provision_after_approval(self):
        with self.assertRaises(obc.ClientError) as cm:
            self.provision()
        self.assertEqual(cm.exception.code, "client_not_approved")
        status = obc.request_access("openbao1", False, ["postgresql19c-x-peers"], run=self.run_task)
        self.assertEqual(status["status"], "pending")
        self.assertEqual(status["groups_requested"], ["postgresql19c-x-peers"])
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task), "pending")
        self.assertFalse(obc.active(self.state))

        self.bao.approved[MODULE] = {"groups": ["postgresql19c-x-peers"]}
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task), "provisioned")
        self.assertTrue(obc.active(self.state))
        token = obc.read_token(self.state)
        self.assertIn(token, self.bao.tokens)
        self.assertEqual(self.bao.wraps, {})  # every wrapper was unwrapped once
        # 0600 files in a 0700 directory, no token in the state file
        directory = os.path.join(self.state, obc.CLIENT_DIR)
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
        for name in os.listdir(directory):
            self.assertEqual(os.stat(os.path.join(directory, name)).st_mode & 0o777, 0o600)
        with open(os.path.join(directory, obc.STATE_FILE)) as fp:
            self.assertNotIn(token, fp.read())
        public = obc.public_state(self.state)
        self.assertEqual(public["kv_prefix"], f"modules/{MODULE}")
        self.assertEqual(public["groups"], ["postgresql19c-x-peers"])
        self.assertNotIn(token, repr(public))
        self.assertEqual(obc.granted_group("postgresql19c-x-peers", self.state)["kv_prefix"], "groups/postgresql19c-x-peers")
        self.assertIsNone(obc.granted_group("other-peers", self.state))

    def test_lookup_rejects_a_substituted_wrapping_token(self):
        self.bao.approved[MODULE] = {}
        self.bao.wrap_path = "auth/token/create-orphan"
        with self.assertRaises(obc.ClientError) as cm:
            self.provision()
        self.assertEqual(cm.exception.code, "wrapping_token_mismatch")
        self.assertFalse(obc.active(self.state))
        self.assertTrue(self.bao.wraps)  # never unwrapped

    def test_already_unwrapped_token_is_detected(self):
        self.bao.approved[MODULE] = {}
        run = self.run_task

        def steal(**kwargs):
            ret = run(**kwargs)
            if kwargs["action"] == "provision-client":
                wrapper = ret["output"]["wrapped_token"]
                self.bao.handle("POST", "sys/wrapping/unwrap", {"X-Vault-Token": wrapper}, {})
            return ret
        with self.assertRaises(obc.ClientError) as cm:
            obc.provision("openbao1", state_dir=self.state, run=steal)
        self.assertEqual(cm.exception.code, "wrapping_token_used")

    def test_renew_reprovision_and_revoke_old_token(self):
        self.bao.approved[MODULE] = {}
        state = self.provision(now=1000)
        first = obc.read_token(self.state)
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task, now=1000 + 60), "fresh")
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task,
                                      now=1000 + obc.RENEW_INTERVAL), "renewed")
        self.assertIn(("POST", "auth/token/renew-self"), self.bao.requests)
        self.assertEqual(obc.read_token(self.state), first)
        # Around day 25: new token, the old one is revoked after the switch
        now = state["issued_at"] + obc.REPROVISION_AGE
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task, now=now), "reprovisioned")
        self.assertNotEqual(obc.read_token(self.state), first)
        self.assertNotIn(first, self.bao.tokens)

    def test_short_lease_and_403_provision_again(self):
        self.bao.approved[MODULE] = {}
        self.provision(now=0)
        self.bao.lease = 3600   # explicit max TTL is near
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task,
                                      now=obc.RENEW_INTERVAL), "reprovisioned")
        self.bao.lease = 86400
        obc.mark_reprovision(self.state)
        token = obc.read_token(self.state)
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task,
                                      now=obc.RENEW_INTERVAL + 1), "reprovisioned")
        self.assertNotEqual(obc.read_token(self.state), token)

    def test_revocation_and_disable_fall_back(self):
        self.bao.approved[MODULE] = {}
        self.provision(now=0)
        self.bao.revoke(MODULE)   # administrator revoked: renew gets 403
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task,
                                      now=obc.RENEW_INTERVAL), "dropped")
        self.assertFalse(obc.active(self.state))
        self.assertEqual(obc.load_state(self.state), {})

        self.bao.approved[MODULE] = {}
        self.provision(now=0)
        self.bao.enabled = False   # provisioning disabled in ns8-openbao
        obc.mark_reprovision(self.state)
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task, now=10), "dropped")
        self.assertFalse(obc.active(self.state))

    def test_unavailable_openbao_keeps_the_tokens(self):
        self.bao.approved[MODULE] = {}
        self.provision(now=0)
        token = obc.read_token(self.state)
        self.bao.sealed = True
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task,
                                      now=obc.RENEW_INTERVAL), "retry")
        self.assertEqual(obc.read_token(self.state), token)

    def test_not_wanted_drops_and_revokes(self):
        self.bao.approved[MODULE] = {}
        self.provision()
        token = obc.read_token(self.state)
        self.assertEqual(obc.maintain("openbao1", state_dir=self.state, run=self.run_task, wanted=False), "inactive")
        self.assertFalse(obc.active(self.state))
        self.assertNotIn(token, self.bao.tokens)


if __name__ == "__main__":
    unittest.main()
