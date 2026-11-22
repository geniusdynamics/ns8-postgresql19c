#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Unit tests of the configuration rendering and the pure logic of pgha.

    python3 -m unittest discover -s tests/unit
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fakeagent"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "imageroot", "pypkg"))

import pgha  # noqa: E402

PORTS = pgha.parse_ports("20001,20002,20003,20004,20005,20006,20007,20008")
SECRETS = {k: f"secret-{k.lower()}" for k in pgha.SECRET_KEYS}


class PortsTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(PORTS, {"pg": 20001, "patroni": 20002, "etcd_client": 20003, "etcd_peer": 20004,
                                 "endpoint": 20005, "endpoint_ro": 20006, "pgadmin": 20007, "public": 20008})

    def test_missing_ports_are_zero(self):
        p = pgha.parse_ports("", "20010")
        self.assertEqual(p["pg"], 20010)
        self.assertEqual(p["pgadmin"], 0)


class PatroniRenderTest(unittest.TestCase):
    def render(self, host="10.5.4.1", mode="async"):
        return json.loads(pgha.render_patroni({
            "module_id": "postgresql19c2", "scope": "postgresql19c1", "host": host, "ports": PORTS,
            "secrets": SECRETS, "etcd_hosts": ["127.0.0.1:20003", "10.5.4.1:20103"],
            "max_connections": 250, "replication_mode": mode}))

    def test_identity_and_addresses(self):
        doc = self.render()
        self.assertEqual(doc["scope"], "postgresql19c1")
        self.assertEqual(doc["name"], "postgresql19c2")
        self.assertEqual(doc["postgresql"]["listen"], "127.0.0.1,10.5.4.1:20001")
        self.assertEqual(doc["postgresql"]["connect_address"], "10.5.4.1:20001")
        self.assertEqual(doc["restapi"]["listen"], "10.5.4.1:20002")
        self.assertEqual(doc["etcd3"]["protocol"], "https")
        self.assertEqual(doc["etcd3"]["hosts"], ["127.0.0.1:20003", "10.5.4.1:20103"])

    def test_loopback_only_without_vpn(self):
        doc = self.render(host="127.0.0.1")
        self.assertEqual(doc["postgresql"]["listen"], "127.0.0.1:20001")

    def test_scram_only(self):
        doc = self.render()
        hba = doc["postgresql"]["pg_hba"]
        for line in hba:
            if line.startswith("host"):
                self.assertTrue(line.endswith("scram-sha-256"), line)
            self.assertNotIn("md5", line)
            self.assertNotIn("trust", line)
        params = doc["bootstrap"]["dcs"]["postgresql"]["parameters"]
        self.assertEqual(params["password_encryption"], "scram-sha-256")

    def test_cluster_parameters(self):
        dcs = self.render()["bootstrap"]["dcs"]
        params = dcs["postgresql"]["parameters"]
        # PostgreSQL 19 default, equal on every member
        self.assertEqual(params["max_locks_per_transaction"], 128)
        self.assertEqual(params["max_connections"], 250)
        self.assertEqual(params["wal_sender_shutdown_timeout"], "30s")
        self.assertNotIn("io_workers", params)
        self.assertTrue(dcs["failsafe_mode"])
        self.assertTrue(dcs["postgresql"]["use_pg_rewind"])
        self.assertFalse(dcs["synchronous_mode"])

    def test_sync_mode(self):
        dcs = self.render(mode="sync")["bootstrap"]["dcs"]
        self.assertTrue(dcs["synchronous_mode"])
        self.assertFalse(dcs["synchronous_mode_strict"])

    def test_secrets_only_in_authentication(self):
        text = pgha.render_patroni({
            "module_id": "a1", "scope": "a1", "host": "10.5.4.1", "ports": PORTS, "secrets": SECRETS,
            "etcd_hosts": [], "max_connections": 100})
        doc = json.loads(text)
        self.assertEqual(doc["postgresql"]["authentication"]["replication"]["username"], pgha.REPLICATION_USER)
        self.assertEqual(doc["restapi"]["authentication"]["password"], SECRETS["PATRONI_REST_PASSWORD"])


class EtcdRenderTest(unittest.TestCase):
    def test_standalone(self):
        env = pgha.parse_envfile(pgha.render_etcd_env({"module_id": "pg1", "host": "10.5.4.1", "ports": PORTS}))
        self.assertEqual(env["ETCD_LISTEN_CLIENT_URLS"], "https://127.0.0.1:20003,https://10.5.4.1:20003")
        self.assertEqual(env["ETCD_LISTEN_PEER_URLS"], "https://10.5.4.1:20004")
        self.assertEqual(env["ETCD_INITIAL_CLUSTER"], "pg1=https://10.5.4.1:20004")
        self.assertEqual(env["ETCD_INITIAL_CLUSTER_STATE"], "new")
        self.assertEqual(env["ETCD_CLIENT_CERT_AUTH"], "true")
        self.assertEqual(env["ETCD_PEER_CLIENT_CERT_AUTH"], "true")
        self.assertNotIn("ETCDCTL_ENDPOINTS", env)

    def test_join(self):
        env = pgha.parse_envfile(pgha.render_etcd_env({
            "module_id": "pg2", "host": "10.5.4.2", "ports": PORTS,
            "initial_cluster": "pg1=https://10.5.4.1:20004,pg2=https://10.5.4.2:20004",
            "initial_state": "existing", "token": "pg1"}))
        self.assertEqual(env["ETCD_INITIAL_CLUSTER_STATE"], "existing")
        self.assertEqual(env["ETCD_INITIAL_CLUSTER_TOKEN"], "pg1")

    def test_loopback_only(self):
        env = pgha.parse_envfile(pgha.render_etcd_env({"module_id": "pg1", "host": "127.0.0.1", "ports": PORTS}))
        self.assertEqual(env["ETCD_LISTEN_CLIENT_URLS"], "https://127.0.0.1:20003")

    def test_initial_cluster_from_member_add(self):
        members = [
            {"ID": 1, "name": "pg1", "peerURLs": ["https://10.5.4.1:20004"]},
            {"ID": 2, "name": "", "peerURLs": ["https://10.5.4.2:20004"], "isLearner": True},
        ]
        self.assertEqual(pgha.etcd_initial_cluster(members, "pg2", "https://10.5.4.2:20004"),
                         "pg1=https://10.5.4.1:20004,pg2=https://10.5.4.2:20004")


class HaproxyRenderTest(unittest.TestCase):
    def test_servers_and_binds(self):
        text = pgha.render_haproxy({"host": "10.5.4.3", "ports": PORTS, "members": [
            {"module_id": "pg10", "host": "10.5.4.2", "pg_port": 20101, "patroni_port": 20102},
            {"module_id": "pg2", "host": "10.5.4.1", "pg_port": 20001, "patroni_port": 20002},
        ]})
        self.assertIn("bind 127.0.0.1:20005", text)
        self.assertIn("bind 10.5.4.3:20005", text)
        self.assertIn("bind 10.5.4.3:20006", text)
        self.assertIn("http-check send meth GET uri /primary", text)
        self.assertIn("uri /read-only?lag=16MB", text)
        self.assertIn("server pg10 10.5.4.2:20101 check port 20102", text)
        # Ordered by module sequence number: pg2 before pg10
        self.assertLess(text.index("server pg2 "), text.index("server pg10 "))
        self.assertIn("on-marked-down shutdown-sessions", text)

    def test_witness_without_members(self):
        text = pgha.render_haproxy({"host": "127.0.0.1", "ports": PORTS, "members": []})
        self.assertNotIn("    server ", text)
        self.assertEqual(text.count("bind 127.0.0.1"), 2)


class ScramTest(unittest.TestCase):
    def test_verifier(self):
        salt = b"0123456789abcdef"
        v = pgha.scram_sha256_verifier("pencil", salt=salt)
        algo, rest = v.split("$", 1)
        iters_salt, keys = rest.split("$")
        iters, salt_b64 = iters_salt.split(":")
        stored, server = keys.split(":")
        self.assertEqual(algo, "SCRAM-SHA-256")
        self.assertEqual(int(iters), 4096)
        self.assertEqual(base64.b64decode(salt_b64), salt)
        salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        self.assertEqual(base64.b64decode(stored), hashlib.sha256(client_key).digest())
        self.assertEqual(base64.b64decode(server), hmac.new(salted, b"Server Key", hashlib.sha256).digest())

    def test_random_salt(self):
        self.assertNotEqual(pgha.scram_sha256_verifier("x"), pgha.scram_sha256_verifier("x"))


class SqlTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(pgha.database_name_for("warpgate1"), "warpgate1")
        self.assertEqual(pgha.database_name_for("my-app.2"), "my_app_2")
        self.assertTrue(pgha.database_name_for("1abc").startswith("db_"))
        self.assertTrue(pgha.valid_database_name("warpgate1"))
        for bad in ("postgres", "replicator", "template1", "pg_x", "Bad", "a;drop", ""):
            self.assertFalse(pgha.valid_database_name(bad), bad)

    def test_grant_sql(self):
        sql = pgha.grant_sql("warpgate1", "warpgate2", "SCRAM-SHA-256$4096:abc$def:ghi", "warpgate")
        self.assertIn('CREATE ROLE "warpgate1_owner" WITH NOLOGIN', sql)
        self.assertIn('CREATE ROLE "warpgate2" WITH LOGIN NOSUPERUSER', sql)
        self.assertIn("PASSWORD 'SCRAM-SHA-256$4096:abc$def:ghi'", sql)
        self.assertIn('GRANT "warpgate1_owner" TO "warpgate2"', sql)
        self.assertIn('CREATE DATABASE "warpgate1" OWNER "warpgate1_owner"', sql)
        self.assertIn("COMMENT ON DATABASE \"warpgate1\" IS 'ns8-client=warpgate2;ns8-app=warpgate'", sql)
        self.assertIn("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC", sql)
        self.assertIn(f"GRANT CONNECT ON DATABASE postgres TO {pgha.REWIND_USER}", sql)
        # SET role after the database exists
        self.assertLess(sql.index("CREATE DATABASE"), sql.index("SET role"))
        self.assertNotIn(" SUPERUSER", sql.replace("NOSUPERUSER", ""))

    def test_ownership(self):
        free = {"database": None, "login": None, "owner_role": None}
        self.assertEqual(pgha.may_use_database(free, "warpgate1", "warpgate"), "")
        created = {"database": "ns8-client=warpgate1;ns8-app=warpgate", "login": None, "owner_role": "warpgate1_owner"}
        # Another instance of the same application shares the database
        self.assertEqual(pgha.may_use_database(created, "warpgate2", "warpgate"), "")
        self.assertEqual(pgha.may_use_database(created, "kumomta1", "kumomta"), "database_owned_by_other")
        self.assertEqual(pgha.may_use_database(created, "report1", "", admin_share=True), "")
        stolen = {"database": None, "login": "ns8-client=other1", "owner_role": None}
        self.assertEqual(pgha.may_use_database(stolen, "warpgate1", "warpgate"), "login_role_owned_by_other")
        system = {"database": None, "login": "", "owner_role": None}
        self.assertEqual(pgha.may_use_database(system, "app1", ""), "login_role_not_managed")
        self.assertEqual(pgha.may_use_database(free, "postgres", ""), "invalid_client_id")
        foreign = {"database": "", "login": None, "owner_role": None}
        self.assertEqual(pgha.may_use_database(foreign, "app1", ""), "database_not_managed")

    def test_comments(self):
        self.assertEqual(pgha.parse_comment("ns8-client=a1;ns8-app=x"), {"ns8-client": "a1", "ns8-app": "x"})
        self.assertEqual(pgha.parse_comment(None), {})

    def test_quote(self):
        self.assertEqual(pgha.quote_literal("a'b"), "'a''b'")
        self.assertEqual(pgha.quote_ident('a"b'), '"a""b"')

    def test_revoke(self):
        self.assertIn('ALTER ROLE "app1" WITH NOLOGIN', pgha.revoke_sql("app", "app1"))
        self.assertNotIn("DROP", pgha.revoke_sql("app", "app1"))
        sql = pgha.revoke_sql("app", "app1", drop=True)
        self.assertIn('DROP DATABASE IF EXISTS "app" WITH (FORCE)', sql)
        self.assertIn('DROP ROLE IF EXISTS "app_owner"', sql)

    def test_filter_dump(self):
        lines = ["CREATE ROLE postgres;\n", "ALTER ROLE postgres WITH SUPERUSER PASSWORD 'x';\n",
                 "CREATE ROLE replicator;\n", "ALTER ROLE rewind_user WITH LOGIN;\n",
                 "CREATE ROLE warpgate1;\n", "ALTER ROLE warpgate1 WITH LOGIN PASSWORD 'SCRAM';\n",
                 "CREATE ROLE postgresx;\n", 'CREATE ROLE "replicator";\n',
                 'ALTER ROLE "postgres" WITH SUPERUSER;\n']
        out = list(pgha.filter_dump(lines))
        self.assertEqual(out, ["CREATE ROLE warpgate1;\n", "ALTER ROLE warpgate1 WITH LOGIN PASSWORD 'SCRAM';\n",
                               "CREATE ROLE postgresx;\n"])


class QuorumTest(unittest.TestCase):
    def test_table(self):
        cases = {1: (1, 0), 2: (2, 0), 3: (2, 1), 4: (3, 1), 5: (3, 2)}
        for voters, (size, tolerance) in cases.items():
            s = pgha.quorum_summary(voters, voters, pg_members=voters)
            self.assertEqual(s["quorum_size"], size, voters)
            self.assertEqual(s["failure_tolerance"], tolerance, voters)
            self.assertTrue(s["quorum_ok"])

    def test_two_members_warn(self):
        s = pgha.quorum_summary(2, 2, pg_members=2)
        self.assertIn("below_quorum", s["warnings"])
        self.assertIn("even_voters", s["warnings"])

    def test_lost(self):
        s = pgha.quorum_summary(3, 1, pg_members=2)
        self.assertFalse(s["quorum_ok"])
        self.assertIn("quorum_lost", s["warnings"])

    def test_witness(self):
        s = pgha.quorum_summary(3, 3, pg_members=2)
        self.assertEqual(s["warnings"], [])
        self.assertIn("learner_pending", pgha.quorum_summary(3, 3, learners=1, pg_members=3)["warnings"])


class TlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AGENT_STATE_DIR": self.tmp.name, "MODULE_ID": "pg1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_node_cert(self):
        from cryptography import x509
        ca, ca_key = pgha.make_ca("test")
        cert, key = pgha.make_node_cert(ca, ca_key, "pg1", {"127.0.0.1", "10.5.4.1"})
        self.assertEqual(pgha.cert_ips(cert), {"127.0.0.1", "10.5.4.1"})
        self.assertTrue(pgha.cert_issued_by(cert, ca))
        c = x509.load_pem_x509_certificate(cert.encode())
        # Required by Python 3.13 strict verification (Patroni's etcd client)
        c.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
        c.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        other, _ = pgha.make_ca("other")
        self.assertFalse(pgha.cert_issued_by(cert, other))

    def test_ensure_node_cert_follows_address(self):
        self.assertTrue(pgha.ensure_node_cert("10.5.4.1"))
        self.assertFalse(pgha.ensure_node_cert("10.5.4.1"))
        self.assertTrue(pgha.ensure_node_cert("10.5.4.9"))
        self.assertEqual(os.stat(pgha.tls_path("node.key")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(pgha.tls_path("ca.key")).st_mode & 0o777, 0o600)

    def test_store_ca_drops_node_cert(self):
        pgha.ensure_node_cert("10.5.4.1")
        cert, key = pgha.make_ca("new")
        pgha.store_ca(cert, key)
        self.assertFalse(os.path.exists(pgha.tls_path("node.crt")))


class FakeRedis:
    def __init__(self, providers=None, hashes=None):
        self._providers = providers or {}
        self.hashes = hashes or {}

    def providers(self, service, transport):
        return list(self._providers.get(service, []))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


class FakeRedisDown:
    def providers(self, service, transport):
        raise ConnectionError("down")


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AGENT_STATE_DIR": self.tmp.name,
            "MODULE_ID": "pg2", "NODE_ID": "2", "PG_ROLE": "join", "PG_CLUSTER_ID": "pg1",
            "TCP_PORTS": "20001,20002,20003,20004,20005,20006,20007", "PG_HOST_OVERRIDE": "10.5.4.2"})
        self.env.start()
        self.rdb = FakeRedis(providers={
            pgha.CLUSTER_SERVICE: [
                {"module_id": "pg1", "host": "10.5.4.1", "node": "1", "cluster_id": "pg1", "role": "initial",
                 "pg_port": "20001", "patroni_port": "20002", "etcd_client_port": "20003",
                 "etcd_peer_port": "20004", "endpoint_port": "20005", "endpoint_ro_port": "20006"},
                {"module_id": "pg9", "host": "10.5.4.9", "node": "9", "cluster_id": "other", "role": "initial",
                 "etcd_client_port": "1", "etcd_peer_port": "2"},
                {"module_id": "pg3", "host": "10.5.4.3", "node": "3", "cluster_id": "pg1", "role": "witness",
                 "pg_port": "0", "patroni_port": "0", "etcd_client_port": "20003", "etcd_peer_port": "20004"},
            ],
            pgha.SERVICE: [
                {"module_id": "pg1", "host": "10.5.4.1", "node": "1", "cluster_id": "pg1", "port": "20005",
                 "ro_port": "20006"},
                {"module_id": "pg3", "host": "10.5.4.3", "node": "3", "cluster_id": "pg1", "port": "20005",
                 "ro_port": "20006"},
                {"module_id": "pg9", "host": "10.5.4.9", "node": "9", "cluster_id": "other", "port": "1"},
            ]})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_cluster_members(self):
        ms = pgha.cluster_members(self.rdb)
        self.assertEqual([m["module_id"] for m in ms], ["pg1", "pg2", "pg3"])
        self.assertEqual(pgha.etcd_hosts(ms), ["127.0.0.1:20003", "10.5.4.1:20003", "10.5.4.3:20003"])
        # Cached for starts without Redis
        self.assertEqual([m["module_id"] for m in pgha.cluster_members(FakeRedisDown())], ["pg1", "pg2", "pg3"])

    def test_candidates(self):
        with mock.patch.dict(os.environ, {"PG_ROLE": "standalone", "PG_CLUSTER_ID": "pg2"}):
            ids = [c["cluster_id"] for c in pgha.cluster_candidates(self.rdb)]
        self.assertEqual(ids, ["other", "pg1"])

    def test_endpoints_prefer_caller_node(self):
        eps = pgha.client_endpoints(self.rdb)
        self.assertEqual([e["module_id"] for e in eps], ["pg1", "pg2", "pg3"])
        self.assertEqual(pgha.pick_endpoint(eps, "3")["module_id"], "pg3")
        self.assertEqual(pgha.pick_endpoint(eps, "7")["module_id"], "pg2")

    def test_haproxy_members_skip_witness(self):
        ms = pgha.cluster_members(self.rdb)
        pg = [m for m in ms if pgha.runs_postgres(m["role"]) and m["pg_port"]]
        self.assertEqual([m["module_id"] for m in pg], ["pg1", "pg2"])

    def test_wanted_units(self):
        self.assertEqual(pgha.wanted_units("witness", pgadmin=False), ["etcd.service", "pgproxy.service"])
        self.assertEqual(pgha.wanted_units("join", pgadmin=True),
                         ["etcd.service", "patroni.service", "pgproxy.service", "pgadmin.service"])
        self.assertEqual(pgha.wanted_units("", pgadmin=True), [])

    def test_consumer_record_has_no_secret(self):
        rec = pgha.consumer_record()
        self.assertEqual(rec["ha_mode"], "cluster")
        for value in rec.values():
            self.assertNotIn("secret", value)


class ClientIdTest(unittest.TestCase):
    def test_module_caller_acts_for_itself(self):
        with mock.patch.dict(os.environ, {"AGENT_TASK_USER": "module/warpgate1"}):
            self.assertEqual(pgha.resolve_client_id({}), "warpgate1")
            with mock.patch("sys.stdout"), self.assertRaises(SystemExit):
                pgha.resolve_client_id({"client_id": "other1"})

    def test_admin_needs_client_id(self):
        with mock.patch.dict(os.environ, {"AGENT_TASK_USER": "admin"}):
            self.assertEqual(pgha.resolve_client_id({"client_id": "app"}), "app")
            with mock.patch("sys.stdout"), self.assertRaises(SystemExit):
                pgha.resolve_client_id({})


if __name__ == "__main__":
    unittest.main()


class PublicAccessTest(unittest.TestCase):
    def render(self, enabled=True, allowlist=None):
        return pgha.render_haproxy({
            "host": "10.5.4.1", "ports": pgha.parse_ports("1,2,3,4,5,6,7,20008"),
            "members": [{"module_id": "pg1", "host": "10.5.4.1", "pg_port": 1, "patroni_port": 2}],
            "public": {"enabled": enabled, "allowlist": allowlist or []}})

    def test_off_by_default(self):
        self.assertNotIn("listen public", self.render(enabled=False))
        self.assertNotIn("listen public", pgha.render_haproxy({
            "host": "10.5.4.1", "ports": PORTS, "members": []}))

    def test_tls_only_any_source(self):
        text = self.render()
        public = text.split("listen public")[1]
        self.assertIn("bind 0.0.0.0:20008", public)
        self.assertNotIn("allowed_src", public)
        self.assertIn("req.payload(0,8) -m bin 0000000804d2162f", public)
        self.assertIn("tcp-request content reject", public)
        self.assertIn("uri /primary", public)
        # The internal listeners keep their loopback and VPN binds
        self.assertIn("bind 127.0.0.1:5", text)

    def test_allowlist(self):
        public = self.render(allowlist=["203.0.113.0/24", "198.51.100.7/32"]).split("listen public")[1]
        self.assertIn("acl allowed_src src 203.0.113.0/24 198.51.100.7/32", public)
        self.assertIn("tcp-request connection reject if !allowed_src", public)

    def test_allowlist_normalization(self):
        self.assertEqual(pgha.public_allowlist("203.0.113.9, 10.0.0.0/8"), ["203.0.113.9/32", "10.0.0.0/8"])
        self.assertEqual(pgha.public_allowlist(["192.0.2.5/24"]), ["192.0.2.0/24"])
        self.assertEqual(pgha.public_allowlist(""), [])
        with self.assertRaises(ValueError):
            pgha.normalize_network("not-an-ip")

    def test_firewall_rules(self):
        self.assertEqual(pgha.firewall_rules(20008, ["203.0.113.0/24"]), [
            'rule family="ipv4" source address="203.0.113.0/24" port port="20008" protocol="tcp" accept'])

    def test_member_tls(self):
        doc = json.loads(pgha.render_patroni({
            "module_id": "a1", "scope": "a1", "host": "10.5.4.1", "ports": PORTS, "secrets": SECRETS,
            "etcd_hosts": [], "max_connections": 100}))
        params = doc["postgresql"]["parameters"]
        self.assertEqual(params["ssl"], "on")
        self.assertEqual(params["ssl_cert_file"], "/run/patroni/node.crt")


class FirewallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AGENT_STATE_DIR": self.tmp.name, "MODULE_ID": "pg1",
                                                "TCP_PORTS": "1,2,3,4,5,6,7,20008"})
        self.env.start()
        import agent
        self.calls = []
        for name in ("add_rich_rules", "remove_rich_rules", "add_public_service", "remove_public_service"):
            setattr(agent, name, (lambda n: lambda *a, **k: self.calls.append((n, a)) or True)(name))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_transitions(self):
        self.assertTrue(pgha.apply_firewall(enabled=True, allowlist=[]))
        self.assertEqual(self.calls, [("add_public_service", ("pg1-public", ["20008/tcp"]))])
        self.calls.clear()
        self.assertFalse(pgha.apply_firewall(enabled=True, allowlist=[]))
        self.assertEqual(self.calls, [])
        # Allowlist: rich rules instead of the open service
        self.assertTrue(pgha.apply_firewall(enabled=True, allowlist=["203.0.113.0/24"]))
        self.assertEqual([c[0] for c in self.calls], ["add_rich_rules", "remove_public_service"])
        self.calls.clear()
        self.assertTrue(pgha.apply_firewall(enabled=False))
        self.assertEqual([c[0] for c in self.calls], ["remove_rich_rules"])
        self.assertEqual(pgha.read_firewall_state(), {"service": False, "rules": []})
