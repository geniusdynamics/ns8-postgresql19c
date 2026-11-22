#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""SecretManager: one way to read and write every secret of the module.

Backends:

- LocalBackend (always on): the 0600 file state/secrets.env. It is the
  operational copy: units and actions start from it, so PostgreSQL and
  Patroni start and rejoin after a full reboot while OpenBao is sealed or
  unreachable. It is in the backup (state-include.conf).
- OpenBaoBackend (opt-in): an OpenBao or Vault KV version 2 store, the
  pattern of ns8-kumomta. Secrets live under
  <mount>/data/<path>/<cluster_id>/<name>, field "value". It is the source
  of truth and the sync point of the members of one PostgreSQL cluster: a
  joining member reads the cluster secrets there instead of receiving them
  from a peer, and the reconcile timer pushes local values written while
  OpenBao was unavailable.

The OpenBao address and token are in the 0600 file state/openbao.env,
never in the module environment, Redis, argv or logs; get-configuration
returns vault_token_set only.

Secrets the module itself needs (the bootstrap secrets) are always cached
locally. A secret may be stored in OpenBao only (local=False, e.g. the
consumer credentials published for OpenBao-aware applications): those
are listed by list-openbao-only-secrets and copied back by
copy-openbao-to-local before OpenBao is turned off.
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

import pgha

OPENBAO_ENV = "openbao.env"
VAULT_ONLY_INDEX = "openbao-only.json"
CLIENTS_DIR = os.path.join("secrets", "clients")
FIELD = "value"
# Shared by every member of a PostgreSQL cluster
SHARED_SECRETS = pgha.CLUSTER_SECRET_KEYS + ("CA_CERT", "CA_KEY")
# Needed to start: always kept in the local cache
BOOTSTRAP_SECRETS = SHARED_SECRETS + ("PGADMIN_PASSWORD",)


class SecretError(RuntimeError):
    pass


class OpenBaoUnavailable(SecretError):
    """Sealed, unreachable or misconfigured: use the local cache"""


class OpenBaoHTTPError(SecretError):
    def __init__(self, status, path):
        super().__init__(f"OpenBao answered {status} for {path}")
        self.status = status


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class LocalBackend:
    """state/secrets.env (0600) and the CA files under state/tls"""

    name = "local"

    def __init__(self, state_dir=None):
        self.state_dir = state_dir or pgha.state_dir()

    def _path(self, *parts):
        return os.path.join(self.state_dir, *parts)

    def _read_env(self):
        return pgha.read_envfile(self._path(pgha.SECRETS_FILE))

    def get(self, name):
        if name == "CA_CERT":
            return self._read_file(pgha.TLS_DIR, "ca.crt")
        if name == "CA_KEY":
            return self._read_file(pgha.TLS_DIR, "ca.key")
        if name.startswith("client/"):
            return self._read_file(CLIENTS_DIR, name.split("/", 1)[1])
        return self._read_env().get(name)

    def _read_file(self, *parts):
        try:
            with open(self._path(*parts)) as fp:
                return fp.read()
        except FileNotFoundError:
            return None

    def set(self, name, value):
        if name in ("CA_CERT", "CA_KEY"):
            os.makedirs(self._path(pgha.TLS_DIR), mode=0o700, exist_ok=True)
            pgha.write_private(self._path(pgha.TLS_DIR, "ca.crt" if name == "CA_CERT" else "ca.key"), value)
            return
        if name.startswith("client/"):
            pgha.write_private(self._path(CLIENTS_DIR, name.split("/", 1)[1]), value)
            return
        values = self._read_env()
        values[name] = value
        text = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
        pgha.write_private(self._path(pgha.SECRETS_FILE), text)

    def delete(self, name):
        if name.startswith("client/"):
            try:
                os.unlink(self._path(CLIENTS_DIR, name.split("/", 1)[1]))
            except FileNotFoundError:
                pass
            return
        values = self._read_env()
        if values.pop(name, None) is not None:
            pgha.write_private(self._path(pgha.SECRETS_FILE),
                               "".join(f"{k}={v}\n" for k, v in sorted(values.items())))

    def list(self):
        names = set(self._read_env())
        for name, fname in (("CA_CERT", "ca.crt"), ("CA_KEY", "ca.key")):
            if os.path.exists(self._path(pgha.TLS_DIR, fname)):
                names.add(name)
        try:
            names |= {f"client/{f}" for f in os.listdir(self._path(CLIENTS_DIR))}
        except FileNotFoundError:
            pass
        return sorted(names)


class OpenBaoBackend:
    """KV version 2 at <address>/v1/<mount>/data/<path>/<scope>/<name>"""

    name = "openbao"

    def __init__(self, address, token, mount="secret", path="postgresql19c", scope="",
                 tls_skip_verify=False, timeout=8):
        self.address = (address or "").rstrip("/")
        self.token = token or ""
        self.mount = (mount or "secret").strip("/")
        self.base = "/".join(p for p in ((path or "").strip("/"), scope) if p)
        self.tls_skip_verify = tls_skip_verify
        self.timeout = timeout

    def secret_path(self, name):
        return "/".join(p for p in (self.base, name) if p)

    def _request(self, method, api, body=None):
        return self.api(method, f"{self.mount}/{api}", body)

    def api(self, method, path, body=None):
        """Any OpenBao API call under /v1/ (path without the leading /v1/)"""
        if not self.address or not self.token:
            raise OpenBaoUnavailable("OpenBao address or token not set")
        url = f"{self.address}/v1/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Vault-Token": self.token, "Content-Type": "application/json"})
        ctx = None
        if self.address.startswith("https://") and self.tls_skip_verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as ex:
            # 503: sealed or standby without a leader
            if ex.code in (502, 503, 504):
                raise OpenBaoUnavailable(f"OpenBao is not available ({ex.code})") from None
            raise OpenBaoHTTPError(ex.code, path.split("/", 1)[-1]) from None
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise OpenBaoUnavailable(f"cannot reach OpenBao: {type(ex).__name__}") from None
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def get(self, name):
        try:
            payload = self._request("GET", "data/" + self.secret_path(name))
        except OpenBaoHTTPError as ex:
            if ex.status == 404:
                return None
            raise
        value = ((payload.get("data") or {}).get("data") or {}).get(FIELD)
        return value if isinstance(value, str) and value else None

    def set(self, name, value, create_only=False):
        body = {"data": {FIELD: value}}
        if create_only:
            body["options"] = {"cas": 0}
        self._request("POST", "data/" + self.secret_path(name), body)

    def delete(self, name):
        try:
            self._request("DELETE", "metadata/" + self.secret_path(name))
        except OpenBaoHTTPError as ex:
            if ex.status != 404:
                raise

    def list(self):
        try:
            payload = self._request("LIST", "metadata/" + self.base)
        except OpenBaoHTTPError as ex:
            if ex.status in (404, 405):
                return []
            raise
        return sorted((payload.get("data") or {}).get("keys") or [])


# ---------------------------------------------------------------------------
# SecretManager
# ---------------------------------------------------------------------------

class SecretManager:
    def __init__(self, local=None, vault=None, state_dir=None):
        self.local = local or LocalBackend(state_dir)
        self.vault = vault
        self.state_dir = state_dir or pgha.state_dir()

    # -- index of the secrets kept in OpenBao only
    def _index_path(self):
        return os.path.join(self.state_dir, VAULT_ONLY_INDEX)

    def _read_index(self):
        try:
            with open(self._index_path()) as fp:
                return json.load(fp)
        except (FileNotFoundError, ValueError):
            return {}

    def _write_index(self, index):
        pgha.write_private(self._index_path(), json.dumps(index, sort_keys=True) + "\n")

    # -- public API
    def get_secret(self, name, prefer_vault=False):
        """Local cache first (never blocks on OpenBao). With prefer_vault,
        OpenBao is asked first and the local cache refreshed; when OpenBao
        is unavailable the cached value is returned."""
        if self.vault and (prefer_vault or self.local.get(name) is None):
            try:
                value = self.vault.get(name)
                if value is not None:
                    if name in BOOTSTRAP_SECRETS and self.local.get(name) != value:
                        self.local.set(name, value)
                    return value
            except OpenBaoUnavailable as ex:
                pgha.log(f"OpenBao unavailable, using the local copy of {name}: {ex}")
        return self.local.get(name)

    def set_secret(self, name, value, local=True):
        """Write the local cache (unless local=False) and OpenBao when
        enabled. An OpenBao failure leaves the value pending: the reconcile
        timer pushes it. A secret kept only in OpenBao must reach it."""
        if local or name in BOOTSTRAP_SECRETS:
            self.local.set(name, value)
        index = self._read_index()
        if self.vault:
            try:
                self.vault.set(name, value)
                if not local and name not in BOOTSTRAP_SECRETS:
                    index[name] = self.vault.secret_path(name)
                    self._write_index(index)
                return True
            except OpenBaoUnavailable as ex:
                if not local and name not in BOOTSTRAP_SECRETS:
                    raise
                pgha.log(f"OpenBao unavailable, {name} is pushed later: {ex}")
                return False
        elif not local and name not in BOOTSTRAP_SECRETS:
            raise SecretError(f"{name} can be stored in OpenBao only, and OpenBao is off")
        return True

    def delete_secret(self, name):
        self.local.delete(name)
        index = self._read_index()
        if index.pop(name, None) is not None:
            self._write_index(index)
        if self.vault:
            try:
                self.vault.delete(name)
            except SecretError as ex:
                pgha.log(f"Cannot delete {name} from OpenBao: {ex}")

    def list(self):
        """[{name, local, openbao_only}] without values"""
        local = set(self.local.list())
        index = self._read_index()
        names = local | set(index)
        return [{"name": n, "local": n in local, "openbao_only": n in index and n not in local}
                for n in sorted(names)]

    def vault_only_items(self):
        """Secrets that exist in OpenBao only (names and paths, no values)"""
        local = set(self.local.list())
        return [{"name": n, "path": p} for n, p in sorted(self._read_index().items()) if n not in local]

    def copy_to_local(self, names=None):
        """Copy OpenBao-only secrets to the local 0600 files. Returns
        (copied, failed) name lists."""
        if not self.vault:
            raise SecretError("OpenBao is off")
        copied, failed = [], []
        for item in self.vault_only_items():
            if names is not None and item["name"] not in names:
                continue
            try:
                value = self.vault.get(item["name"])
                if value is None:
                    raise SecretError("missing")
                self.local.set(item["name"], value)
                copied.append(item["name"])
            except SecretError:
                failed.append(item["name"])
        return copied, failed

    def push_shared(self, names=SHARED_SECRETS, check_conflicts=True):
        """Copy the local shared secrets to OpenBao where missing. Returns
        (pushed, conflicts): a conflict is a value in OpenBao that differs
        from the local one; it is never overwritten."""
        if not self.vault:
            return [], []
        pushed, conflicts = [], []
        for name in names:
            value = self.local.get(name)
            if not value:
                continue
            remote = self.vault.get(name)
            if remote is None:
                self.vault.set(name, value)
                pushed.append(name)
            elif remote != value and check_conflicts:
                conflicts.append(name)
        return pushed, conflicts

    def fetch_shared(self, names=SHARED_SECRETS):
        """Read every shared secret from OpenBao; None if any is missing"""
        if not self.vault:
            return None
        values = {}
        for name in names:
            value = self.vault.get(name)
            if value is None:
                return None
            values[name] = value
        return values


# ---------------------------------------------------------------------------
# Settings and wiring
# ---------------------------------------------------------------------------

def read_openbao_env(state_dir=None):
    return pgha.read_envfile(os.path.join(state_dir or pgha.state_dir(), OPENBAO_ENV))


def write_openbao_env(address, token, state_dir=None):
    pgha.write_private(os.path.join(state_dir or pgha.state_dir(), OPENBAO_ENV),
                       f"VAULT_ADDR={address}\nVAULT_TOKEN={token}\n")


def remove_openbao_env(state_dir=None):
    try:
        os.unlink(os.path.join(state_dir or pgha.state_dir(), OPENBAO_ENV))
    except FileNotFoundError:
        pass


def vault_settings():
    env = read_openbao_env()
    return {
        "vault_enabled": pgha.env("PG_VAULT_ENABLED") == "1",
        "vault_address": env.get("VAULT_ADDR", ""),
        "vault_token_set": bool(env.get("VAULT_TOKEN")),
        "vault_mount": pgha.env("PG_VAULT_MOUNT", "secret") or "secret",
        "vault_path": pgha.env("PG_VAULT_PATH", "postgresql19c") or "postgresql19c",
        "vault_tls_skip_verify": pgha.env("PG_VAULT_TLS_SKIP_VERIFY") == "1",
    }


def make_vault(settings=None, scope=None, address=None, token=None):
    settings = settings or vault_settings()
    env = read_openbao_env()
    return OpenBaoBackend(address if address is not None else env.get("VAULT_ADDR", ""),
                          token if token is not None else env.get("VAULT_TOKEN", ""),
                          settings["vault_mount"], settings["vault_path"],
                          scope if scope is not None else pgha.cluster_id(),
                          settings.get("vault_tls_skip_verify", False))


def manager(scope=None):
    """The SecretManager of this instance, with OpenBao when enabled"""
    settings = vault_settings()
    vault = make_vault(settings, scope) if settings["vault_enabled"] else None
    return SecretManager(vault=vault)


def sync_shared():
    """Reconcile step: push the shared secrets written while OpenBao was
    unavailable. Returns True when something was pushed."""
    settings = vault_settings()
    if not settings["vault_enabled"] or not pgha.role():
        return False
    mgr = manager()
    try:
        pushed, conflicts = mgr.push_shared()
    except OpenBaoUnavailable as ex:
        pgha.log(f"OpenBao unavailable, the shared secrets are synchronized later: {ex}")
        return False
    if conflicts:
        pgha.warning("The OpenBao copy of " + ", ".join(conflicts) +
                     " differs from the local one: the local copy is used (see docs/openbao.md)")
    return bool(pushed)


# ---------------------------------------------------------------------------
# Database secrets engine
# ---------------------------------------------------------------------------

ENGINE_ROLE = "ns8_openbao"


def database_engine_config(host, port, username, password, allowed_roles):
    """Body of <mount>/config/<name> for the PostgreSQL plugin. OpenBao
    substitutes {{username}} and {{password}} itself."""
    return {
        "plugin_name": "postgresql-database-plugin",
        "connection_url": f"postgresql://{{{{username}}}}:{{{{password}}}}@{host}:{port}/postgres?sslmode=disable",
        "username": username,
        "password": password,
        "password_authentication": "scram-sha-256",
        "allowed_roles": allowed_roles,
        "verify_connection": True,
    }


def register_database_engine(vault, mount, name, host, port, password, allowed_roles, rotate=True):
    """Enable the database secrets engine at `mount` if needed, configure
    the connection `name`, then rotate its root credential so that only
    OpenBao knows it"""
    try:
        vault.api("GET", f"sys/mounts/{mount}")
    except OpenBaoHTTPError as ex:
        if ex.status not in (400, 404):
            raise
        vault.api("POST", f"sys/mounts/{mount}", {"type": "database"})
    vault.api("POST", f"{mount}/config/{name}",
              database_engine_config(host, port, ENGINE_ROLE, password, allowed_roles))
    if rotate:
        vault.api("POST", f"{mount}/rotate-root/{name}", {})
