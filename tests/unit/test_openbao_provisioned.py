#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Provisioned mode (ns8-openbao client provisioning) of pgha_secrets
against an in-process fake OpenBao: the auth-provider seam, the cluster
group of the shared secrets, and the database engine mount
ns8-db-<module id>."""

import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakeagent"))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "imageroot", "pypkg"))

import openbao_client  # noqa: E402
import pgha  # noqa: E402
import pgha_secrets as ps  # noqa: E402
from fake_bao_api import FakeBao  # noqa: E402

MODULE = "postgresql19c1"
GROUP = "postgresql19c-pgc1-peers"


class ProvisionedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)
        self.bao = FakeBao()
        self.bao.start()
        self.addCleanup(self.bao.stop)
        env = mock.patch.dict(os.environ, {
            "AGENT_STATE_DIR": self.tmp.name, "MODULE_ID": MODULE, "PG_CLUSTER_ID": "pgc1",
            "PG_VAULT_ENABLED": "1", "PG_VAULT_AUTH": "provisioned", "PG_VAULT_PROVIDER": "openbao1"})
        env.start()
        self.addCleanup(env.stop)
        self.run_task = self.bao.runner(MODULE)

    def step(self):
        return ps.client_step(run=self.run_task)

    def test_group_name(self):
        self.assertEqual(ps.cluster_group(), GROUP)
        self.assertEqual(ps.cluster_group("PG_X.1"), "postgresql19c-pg-x-1-peers")
        self.assertTrue(ps.cluster_group("x" * 80).endswith("-peers"))
        self.assertLessEqual(len(ps.cluster_group("x" * 80)), 63)

    def test_pending_uses_local_secrets_and_requests_group_and_engine(self):
        status, warnings = self.step()
        self.assertEqual(status, "pending")
        self.assertEqual(warnings[0]["id"], "openbao_client_pending")
        self.assertEqual(self.bao.pending[MODULE]["database_engine"], True)
        mgr = ps.manager()
        self.assertIsNone(mgr.vault)
        mgr.set_secret("PGADMIN_PASSWORD", "local-only")
        self.assertEqual(mgr.get_secret("PGADMIN_PASSWORD"), "local-only")

    def test_shared_secrets_go_to_the_group_others_to_the_module(self):
        self.bao.approved[MODULE] = {"database_engine": True}
        status, _ = self.step()
        self.assertEqual(status, "provisioned")
        # Approved without the group: the group request is recorded
        self.assertEqual(self.bao.pending[MODULE]["groups"], [GROUP])
        vault = ps.manager().vault
        self.assertIsInstance(vault, ps.RoutingVault)
        self.assertIsNone(vault.group)
        with self.assertRaises(ps.OpenBaoUnavailable):
            vault.get(ps.SHARED_SECRETS[0])

        self.bao.approved[MODULE] = {"database_engine": True, "groups": [GROUP]}
        openbao_client.mark_reprovision()
        self.assertEqual(self.step()[0], "reprovisioned")
        mgr = ps.manager()
        shared = ps.SHARED_SECRETS[0]
        mgr.set_secret(shared, "replication-secret")
        mgr.set_secret("PGADMIN_PASSWORD", "pgadmin-secret")
        self.assertEqual(self.bao.kv[("ns8-kv", f"groups/{GROUP}/{shared}")], {"value": "replication-secret"})
        self.assertEqual(self.bao.kv[("ns8-kv", f"modules/{MODULE}/PGADMIN_PASSWORD")], {"value": "pgadmin-secret"})
        # The module token cannot read the group prefix, and the reverse
        module_token = openbao_client.read_token()
        self.assertFalse(self.bao._allowed(module_token, "ns8-kv", f"groups/{GROUP}/{shared}"))
        self.assertEqual(ps.manager().fetch_shared(names=(shared,)), {shared: "replication-secret"})

    def test_403_marks_reprovision(self):
        self.bao.approved[MODULE] = {"database_engine": True, "groups": [GROUP]}
        self.step()
        vault = ps.manager().vault
        self.bao.revoke(MODULE)
        with self.assertRaises(ps.OpenBaoHTTPError):
            vault.get("PGADMIN_PASSWORD")
        self.assertTrue(openbao_client.load_state().get("reprovision"))
        self.assertEqual(self.step()[0], "dropped")
        self.assertIsNone(ps.manager().vault)

    def test_database_engine_uses_the_provisioned_mount(self):
        self.bao.approved[MODULE] = {"database_engine": True, "groups": [GROUP]}
        self.step()
        client = openbao_client.public_state()
        self.assertEqual(client["database_mount"], f"ns8-db-{MODULE}")
        vault = ps.manager().vault
        ps.register_database_engine(vault, client["database_mount"], "pgc1", "10.5.4.1", 20005, "pw", ["*"],
                                    provisioned=True)
        calls = self.bao.database[f"ns8-db-{MODULE}"]
        self.assertEqual([(c[0], c[1], c[2]) for c in calls],
                         [("POST", "config", "pgc1"), ("POST", "rotate-root", "pgc1")])
        self.assertEqual(calls[0][3]["username"], ps.ENGINE_ROLE)
        self.assertNotIn(("GET", "sys/mounts/ns8-db-" + MODULE), self.bao.requests)
        # Another module's mount is refused
        with self.assertRaises(ps.OpenBaoHTTPError):
            ps.register_database_engine(vault, "ns8-db-other1", "pgc1", "h", 1, "pw", ["*"], provisioned=True)

    def test_turning_off_revokes_the_tokens(self):
        self.bao.approved[MODULE] = {"database_engine": True}
        self.step()
        token = openbao_client.read_token()
        status, _ = ps.client_step(dict(ps.vault_settings(), vault_enabled=False), run=self.run_task)
        self.assertEqual(status, "inactive")
        self.assertNotIn(token, self.bao.tokens)

    def test_pasted_token_mode_is_unchanged(self):
        with mock.patch.dict(os.environ, {"PG_VAULT_AUTH": "token"}):
            ps.write_openbao_env(self.bao.address, "root")
            vault = ps.make_vault()
            self.assertIsInstance(vault, ps.OpenBaoBackend)
            self.assertIsInstance(vault.auth, ps.StaticTokenAuth)


if __name__ == "__main__":
    unittest.main()
