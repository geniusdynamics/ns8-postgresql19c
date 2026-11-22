#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Unit tests of the SecretManager backends against a fake OpenBao,
including the sealed and unreachable paths that fall back to the local
cache."""

import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakeagent"))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "imageroot", "pypkg"))

import fake_openbao  # noqa: E402
import pgha  # noqa: E402
import pgha_secrets as ps  # noqa: E402

TOKEN = "test-token"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeOpenBao:
    def __init__(self):
        self.port = free_port()
        self.server = fake_openbao.serve(self.port, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.address = f"http://127.0.0.1:{self.port}"

    def seal(self, sealed=True):
        req = urllib.request.Request(f"{self.address}/fake/{'seal' if sealed else 'unseal'}", method="POST", data=b"")
        urllib.request.urlopen(req).read()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bao = FakeOpenBao()

    @classmethod
    def tearDownClass(cls):
        cls.bao.stop()

    def setUp(self):
        fake_openbao.STORE.clear()
        fake_openbao.STATE["sealed"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AGENT_STATE_DIR": self.tmp.name, "MODULE_ID": "pg1",
                                                "PG_CLUSTER_ID": "pg1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def vault(self, token=TOKEN, scope="pg1", address=None):
        return ps.OpenBaoBackend(address or self.bao.address, token, "secret", "postgresql19c", scope)

    def mode(self, name):
        return os.stat(os.path.join(self.tmp.name, name)).st_mode & 0o777


class LocalBackendTest(Base):
    def test_roundtrip_and_mode(self):
        b = ps.LocalBackend()
        self.assertIsNone(b.get("PG_SUPERUSER_PASSWORD"))
        b.set("PG_SUPERUSER_PASSWORD", "abc")
        b.set("PG_REPLICATION_PASSWORD", "def")
        self.assertEqual(b.get("PG_SUPERUSER_PASSWORD"), "abc")
        self.assertEqual(self.mode("secrets.env"), 0o600)
        self.assertEqual(b.list(), ["PG_REPLICATION_PASSWORD", "PG_SUPERUSER_PASSWORD"])
        b.delete("PG_REPLICATION_PASSWORD")
        self.assertEqual(b.list(), ["PG_SUPERUSER_PASSWORD"])

    def test_ca_and_clients(self):
        b = ps.LocalBackend()
        b.set("CA_CERT", "cert")
        b.set("CA_KEY", "key")
        b.set("client/app1", "{}")
        self.assertEqual(b.get("CA_KEY"), "key")
        self.assertEqual(self.mode(os.path.join("tls", "ca.key")), 0o600)
        self.assertEqual(self.mode(os.path.join("secrets", "clients", "app1")), 0o600)
        self.assertIn("client/app1", b.list())
        self.assertIn("CA_CERT", b.list())

    def test_manager_without_vault(self):
        mgr = ps.SecretManager()
        mgr.set_secret("PG_REWIND_PASSWORD", "x")
        self.assertEqual(mgr.get_secret("PG_REWIND_PASSWORD"), "x")
        with self.assertRaises(ps.SecretError):
            mgr.set_secret("client/app", "y", local=False)
        self.assertEqual([i["name"] for i in mgr.list()], ["PG_REWIND_PASSWORD"])


class OpenBaoBackendTest(Base):
    def test_kv2_roundtrip(self):
        v = self.vault()
        self.assertIsNone(v.get("PG_SUPERUSER_PASSWORD"))
        v.set("PG_SUPERUSER_PASSWORD", "s1")
        self.assertEqual(v.get("PG_SUPERUSER_PASSWORD"), "s1")
        self.assertIn("secret:postgresql19c/pg1/PG_SUPERUSER_PASSWORD", fake_openbao.STORE)
        self.assertEqual(v.list(), ["PG_SUPERUSER_PASSWORD"])
        v.delete("PG_SUPERUSER_PASSWORD")
        self.assertIsNone(v.get("PG_SUPERUSER_PASSWORD"))

    def test_create_only(self):
        v = self.vault()
        v.set("X", "1", create_only=True)
        with self.assertRaises(ps.OpenBaoHTTPError):
            v.set("X", "2", create_only=True)

    def test_denied(self):
        with self.assertRaises(ps.OpenBaoHTTPError) as ctx:
            self.vault(token="wrong").get("X")
        self.assertEqual(ctx.exception.status, 403)

    def test_sealed_is_unavailable(self):
        self.bao.seal()
        with self.assertRaises(ps.OpenBaoUnavailable):
            self.vault().get("X")

    def test_unreachable(self):
        with self.assertRaises(ps.OpenBaoUnavailable):
            self.vault(address=f"http://127.0.0.1:{free_port()}").get("X")

    def test_no_settings(self):
        with self.assertRaises(ps.OpenBaoUnavailable):
            ps.OpenBaoBackend("", "").get("X")


class ManagerWithVaultTest(Base):
    def test_set_writes_both(self):
        mgr = ps.SecretManager(vault=self.vault())
        self.assertTrue(mgr.set_secret("PG_SUPERUSER_PASSWORD", "p1"))
        self.assertEqual(mgr.local.get("PG_SUPERUSER_PASSWORD"), "p1")
        self.assertEqual(mgr.vault.get("PG_SUPERUSER_PASSWORD"), "p1")

    def test_sealed_falls_back_to_local_cache(self):
        mgr = ps.SecretManager(vault=self.vault())
        mgr.set_secret("PG_REPLICATION_PASSWORD", "r1")
        self.bao.seal()
        # Boot path: the local cache answers, OpenBao is never a dependency
        self.assertEqual(mgr.get_secret("PG_REPLICATION_PASSWORD", prefer_vault=True), "r1")
        # A write while sealed stays local and is pushed later
        self.assertFalse(mgr.set_secret("PG_REWIND_PASSWORD", "w1"))
        self.assertEqual(mgr.local.get("PG_REWIND_PASSWORD"), "w1")
        self.bao.seal(False)
        pushed, conflicts = mgr.push_shared()
        self.assertEqual(pushed, ["PG_REWIND_PASSWORD"])
        self.assertEqual(conflicts, [])
        self.assertEqual(mgr.vault.get("PG_REWIND_PASSWORD"), "w1")

    def test_prefer_vault_refreshes_cache(self):
        v = self.vault()
        v.set("PATRONI_REST_PASSWORD", "from-bao")
        mgr = ps.SecretManager(vault=v)
        self.assertEqual(mgr.get_secret("PATRONI_REST_PASSWORD", prefer_vault=True), "from-bao")
        self.assertEqual(mgr.local.get("PATRONI_REST_PASSWORD"), "from-bao")

    def test_conflicts_are_reported_not_overwritten(self):
        v = self.vault()
        v.set("PG_SUPERUSER_PASSWORD", "other")
        mgr = ps.SecretManager(vault=v)
        mgr.local.set("PG_SUPERUSER_PASSWORD", "mine")
        pushed, conflicts = mgr.push_shared(names=("PG_SUPERUSER_PASSWORD",))
        self.assertEqual((pushed, conflicts), ([], ["PG_SUPERUSER_PASSWORD"]))
        self.assertEqual(v.get("PG_SUPERUSER_PASSWORD"), "other")

    def test_fetch_shared_for_a_joining_member(self):
        writer = ps.SecretManager(vault=self.vault())
        for name in ps.SHARED_SECRETS:
            writer.set_secret(name, f"v-{name}")
        joiner_dir = tempfile.TemporaryDirectory()
        try:
            joiner = ps.SecretManager(vault=self.vault(), state_dir=joiner_dir.name)
            shared = joiner.fetch_shared()
            self.assertEqual(shared["PG_REPLICATION_PASSWORD"], "v-PG_REPLICATION_PASSWORD")
            self.assertEqual(shared["CA_KEY"], "v-CA_KEY")
            # Another cluster path holds nothing
            self.assertIsNone(ps.SecretManager(vault=self.vault(scope="pg9")).fetch_shared())
        finally:
            joiner_dir.cleanup()

    def test_vault_only_items_and_copy_to_local(self):
        mgr = ps.SecretManager(vault=self.vault())
        mgr.set_secret("client/app1", '{"password": "x"}', local=False)
        self.assertIsNone(mgr.local.get("client/app1"))
        items = mgr.vault_only_items()
        self.assertEqual(items, [{"name": "client/app1", "path": "postgresql19c/pg1/client/app1"}])
        self.assertEqual(mgr.list(), [{"name": "client/app1", "local": False, "openbao_only": True}])
        copied, failed = mgr.copy_to_local()
        self.assertEqual((copied, failed), (["client/app1"], []))
        self.assertEqual(mgr.vault_only_items(), [])
        self.assertEqual(self.mode(os.path.join("secrets", "clients", "app1")), 0o600)

    def test_vault_only_write_needs_openbao(self):
        mgr = ps.SecretManager(vault=self.vault())
        self.bao.seal()
        with self.assertRaises(ps.OpenBaoUnavailable):
            mgr.set_secret("client/app2", "{}", local=False)

    def test_copy_failure_is_reported(self):
        mgr = ps.SecretManager(vault=self.vault())
        mgr.set_secret("client/app3", "{}", local=False)
        self.bao.seal()
        copied, failed = mgr.copy_to_local()
        self.assertEqual((copied, failed), ([], ["client/app3"]))

    def test_bootstrap_secrets_always_local(self):
        mgr = ps.SecretManager(vault=self.vault())
        mgr.set_secret("PG_SUPERUSER_PASSWORD", "boot", local=False)
        self.assertEqual(mgr.local.get("PG_SUPERUSER_PASSWORD"), "boot")


class SettingsTest(Base):
    def test_token_file(self):
        ps.write_openbao_env("http://bao:8200", "tok")
        self.assertEqual(self.mode("openbao.env"), 0o600)
        with mock.patch.dict(os.environ, {"PG_VAULT_ENABLED": "1"}):
            s = ps.vault_settings()
        self.assertTrue(s["vault_token_set"])
        self.assertNotIn("vault_token", s)
        self.assertEqual(s["vault_path"], "postgresql19c")
        ps.remove_openbao_env()
        self.assertFalse(ps.vault_settings()["vault_token_set"])

    def test_sync_shared(self):
        ps.write_openbao_env(self.bao.address, TOKEN)
        with mock.patch.dict(os.environ, {"PG_VAULT_ENABLED": "1", "PG_ROLE": "initial"}):
            ps.LocalBackend().set("PG_SUPERUSER_PASSWORD", "s")
            self.assertTrue(ps.sync_shared())
            self.assertFalse(ps.sync_shared())
            self.bao.seal()
            self.assertFalse(ps.sync_shared())


if __name__ == "__main__":
    unittest.main()
