#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Client side of the ns8-openbao client provisioning (ns8-openbao 1.3.0,
docs/client-provisioning.md of geniusdynamics/ns8-openbao).

The module asks an OpenBao module of the cluster (role secretsclient,
authorization openbao@any:secretsclient) for its own token:

1. request-client-access records a pending request (database engine,
   shared groups); the OpenBao administrator approves it;
2. provision-client returns response-wrapped tokens; each one is checked
   with sys/wrapping/lookup (creation_path) and unwrapped at once;
3. the tokens go to 0600 files under state/openbao-client/, never to the
   module environment (Redis), argv, logs or an action output;
4. maintain() (timer) renews them every RENEW_INTERVAL, provisions again
   after REPROVISION_AGE or on a 403, and drops them when provisioning is
   disabled or the approval is revoked: the module then falls back to the
   pasted token, then to its local secrets.

The same file is shipped by ns8-kumomta and ns8-postgresql19c. Pure
Python, no external package: the agent package is imported lazily so unit
tests can run without it.
"""

import json
import os
import time
import urllib.error
import urllib.request

CLIENT_DIR = "openbao-client"          # 0700, under the state directory
STATE_FILE = "state.json"              # no secrets
TOKEN_FILE = "token"                   # module token, 0600
GROUP_TOKEN_SUFFIX = ".group-token"    # <group id>.group-token, 0600
WRAP_CREATION_PATH = "auth/token/create/ns8-client"
KV_MOUNT = "ns8-kv"
RENEW_INTERVAL = 8 * 3600              # renew-self every 8 hours (period 24 h)
REPROVISION_AGE = 25 * 86400           # explicit max TTL is 30 days
PENDING_RETRY = 3600                   # pending modules try to provision hourly
# provision-client errors after which the stored tokens are dropped
FALLBACK_ERRORS = ("client_provisioning_disabled", "client_not_approved")
TIMEOUT = 15


class ClientError(RuntimeError):
    """A provisioning failure: code is the ns8-openbao error code (e.g.
    client_not_approved, client_provisioning_disabled, openbao_unavailable)
    or a client-side one (wrapping_token_mismatch, wrapping_token_used,
    openbao_unreachable, task_failed)."""

    def __init__(self, code, value=""):
        super().__init__(f"{code} {value}".strip())
        self.code = code
        self.value = value


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _dir(state_dir=None):
    return os.path.join(state_dir or os.getcwd(), CLIENT_DIR)


def _write_private(path, text):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        fp.write(text)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path) as fp:
            return fp.read().strip()
    except FileNotFoundError:
        return ""


def load_state(state_dir=None):
    """The non-secret state of the provisioned client, or {}"""
    try:
        with open(os.path.join(_dir(state_dir), STATE_FILE)) as fp:
            return json.load(fp)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state, state_dir=None):
    _write_private(os.path.join(_dir(state_dir), STATE_FILE), json.dumps(state, indent=2, sort_keys=True) + "\n")


def read_token(state_dir=None):
    return _read(os.path.join(_dir(state_dir), TOKEN_FILE))


def read_group_token(group_id, state_dir=None):
    return _read(os.path.join(_dir(state_dir), group_id + GROUP_TOKEN_SUFFIX))


def active(state_dir=None):
    """True when a provisioned token is stored"""
    return bool(load_state(state_dir).get("address") and read_token(state_dir))


def granted_group(group_id, state_dir=None):
    """{group_id, kv_prefix} when the group token is stored, else None"""
    for group in load_state(state_dir).get("groups") or []:
        if group.get("group_id") == group_id and read_group_token(group_id, state_dir):
            return group
    return None


def _remove_tokens(state_dir=None, keep=()):
    try:
        names = os.listdir(_dir(state_dir))
    except FileNotFoundError:
        return
    for name in names:
        if (name == TOKEN_FILE or name.endswith(GROUP_TOKEN_SUFFIX)) and name not in keep:
            try:
                os.unlink(os.path.join(_dir(state_dir), name))
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# OpenBao HTTP API
# ---------------------------------------------------------------------------

def api(address, path, method="GET", token="", data=None, timeout=TIMEOUT):
    """(status, body) of an OpenBao API call. Raises ClientError
    openbao_unreachable when OpenBao cannot be reached. Never logs the
    request or response body: they may hold tokens."""
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(f"{address.rstrip('/')}/v1/{path}", data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as ex:
        return ex.code, {}
    except (urllib.error.URLError, OSError, ValueError) as ex:
        raise ClientError("openbao_unreachable", type(ex).__name__) from None
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, {}


def unwrap(address, wrapped_token, creation_path=WRAP_CREATION_PATH):
    """Check a response-wrapping token, then unwrap it. Return the token."""
    status, info = api(address, "sys/wrapping/lookup", "POST", data={"token": wrapped_token})
    if status != 200:
        # Unknown wrapper: already unwrapped (maybe by someone else) or expired
        raise ClientError("wrapping_token_used", str(status))
    if (info.get("data") or {}).get("creation_path") != creation_path:
        raise ClientError("wrapping_token_mismatch", (info.get("data") or {}).get("creation_path", ""))
    status, body = api(address, "sys/wrapping/unwrap", "POST", token=wrapped_token, data={})
    token = (body.get("auth") or {}).get("client_token") if status == 200 else ""
    if not token:
        # Already unwrapped by somebody else, or expired
        raise ClientError("wrapping_token_used", str(status))
    return token


# ---------------------------------------------------------------------------
# NS8 tasks of the OpenBao module
# ---------------------------------------------------------------------------

def _run(openbao_id, action, data, run=None):
    if run is None:
        import agent.tasks
        run = agent.tasks.run
    ret = run(agent_id=f"module/{openbao_id}", action=action, data=data,
              extra={"isNotificationHidden": True})
    if ret.get("exit_code") == 0:
        return ret.get("output") or {}
    output = ret.get("output")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            output = None
    if isinstance(output, list) and output and isinstance(output[0], dict) and output[0].get("error"):
        raise ClientError(output[0]["error"], str(output[0].get("value", "")))
    raise ClientError("task_failed", str(ret.get("exit_code")))


def get_status(openbao_id, run=None):
    """get-client-status of this module: status none/pending/approved/stale"""
    return _run(openbao_id, "get-client-status", {}, run)


def request_access(openbao_id, database_engine=False, groups=(), run=None):
    data = {"database_engine": bool(database_engine)}
    if groups:
        data["groups"] = sorted(set(groups))
    return _run(openbao_id, "request-client-access", data, run)


def provision(openbao_id, database_engine=False, state_dir=None, run=None, now=None):
    """provision-client, unwrap every token and store it (0600). The old
    tokens are revoked once the new ones are stored. Return the new state."""
    out = _run(openbao_id, "provision-client", {"database_engine": True} if database_engine else {}, run)
    address = out["address"].rstrip("/")
    creation_path = out.get("wrap_creation_path") or WRAP_CREATION_PATH
    if creation_path != WRAP_CREATION_PATH:
        raise ClientError("wrapping_token_mismatch", creation_path)
    tokens = {TOKEN_FILE: unwrap(address, out["wrapped_token"], creation_path)}
    groups = []
    for group in out.get("groups") or []:
        tokens[group["group_id"] + GROUP_TOKEN_SUFFIX] = unwrap(address, group["wrapped_token"], creation_path)
        groups.append({"group_id": group["group_id"], "kv_prefix": group["kv_prefix"]})
    old = load_state(state_dir)
    old_tokens = {}
    if old.get("address"):
        old_tokens[TOKEN_FILE] = read_token(state_dir)
        for group in old.get("groups") or []:
            old_tokens[group["group_id"] + GROUP_TOKEN_SUFFIX] = read_group_token(group["group_id"], state_dir)
    for name, token in tokens.items():
        _write_private(os.path.join(_dir(state_dir), name), token + "\n")
    _remove_tokens(state_dir, keep=tokens.keys())
    now = int(now if now is not None else time.time())
    state = {
        "openbao_module_id": openbao_id,
        "address": address,
        "kv_mount": out.get("kv_mount") or KV_MOUNT,
        "kv_prefix": out["kv_prefix"],
        "database_engine": bool(out.get("database_engine")),
        "database_mount": out.get("database_mount") or "",
        "groups": groups,
        "issued_at": now,
        "renewed_at": now,
        "reprovision": False,
    }
    save_state(state, state_dir)
    # Switch first, then stop using the old tokens
    for token in old_tokens.values():
        if token and token not in tokens.values():
            try:
                api(old["address"], "auth/token/revoke-self", "POST", token=token, data={})
            except ClientError:
                pass
    return state


def drop(state_dir=None, revoke=True):
    """Forget the provisioned tokens (fallback to the pasted token)"""
    state = load_state(state_dir)
    if revoke and state.get("address"):
        for token in [read_token(state_dir)] + [read_group_token(g["group_id"], state_dir)
                                                for g in state.get("groups") or []]:
            if token:
                try:
                    api(state["address"], "auth/token/revoke-self", "POST", token=token, data={}, timeout=5)
                except ClientError:
                    pass
    _remove_tokens(state_dir)
    try:
        os.unlink(os.path.join(_dir(state_dir), STATE_FILE))
    except FileNotFoundError:
        pass


def mark_reprovision(state_dir=None):
    """A request got 403: provision again at the next maintain()"""
    state = load_state(state_dir)
    if state and not state.get("reprovision"):
        state["reprovision"] = True
        save_state(state, state_dir)


def renew(state_dir=None, now=None):
    """renew-self every stored token. Return "renewed", "forbidden" (a 403:
    revoked or expired) or "short" (the lease no longer reaches a full
    period: the explicit max TTL is near)."""
    state = load_state(state_dir)
    result = "renewed"
    tokens = [read_token(state_dir)] + [read_group_token(g["group_id"], state_dir) for g in state.get("groups") or []]
    for token in tokens:
        if not token:
            return "forbidden"
        status, body = api(state["address"], "auth/token/renew-self", "POST", token=token, data={})
        if status in (401, 403):
            return "forbidden"
        if status != 200:
            raise ClientError("openbao_unavailable", str(status))
        lease = (body.get("auth") or {}).get("lease_duration")
        if isinstance(lease, int) and 0 < lease < RENEW_INTERVAL * 2:
            result = "short"
    state["renewed_at"] = int(now if now is not None else time.time())
    save_state(state, state_dir)
    return result


def maintain(openbao_id, database_engine=False, state_dir=None, run=None, now=None, wanted=True):
    """Timer step. Return one of:

    - "inactive": provisioning not wanted and nothing stored;
    - "provisioned": a first token was issued (the module was approved);
    - "pending": not approved yet (or provisioning not ready);
    - "renewed" / "fresh": the tokens are valid;
    - "reprovisioned": new tokens replace the old ones;
    - "dropped": the tokens were revoked or provisioning was disabled: the
      module falls back to the pasted token or its local secrets;
    - "retry": OpenBao cannot answer now; the stored tokens are kept.
    """
    now = int(now if now is not None else time.time())
    state = load_state(state_dir)
    if not wanted:
        if state:
            drop(state_dir)
        return "inactive"
    if not state.get("address") or not read_token(state_dir):
        try:
            provision(openbao_id, database_engine, state_dir, run, now)
            return "provisioned"
        except ClientError as ex:
            return "pending" if ex.code in FALLBACK_ERRORS or ex.code.startswith("client_") else "retry"
    renewal = None
    if state.get("reprovision") or now - int(state.get("renewed_at") or 0) >= RENEW_INTERVAL:
        try:
            renewal = renew(state_dir, now)
        except ClientError:
            return "retry"
    due = (renewal in ("forbidden", "short") or state.get("reprovision")
           or now - int(state.get("issued_at") or 0) >= REPROVISION_AGE
           or state.get("openbao_module_id") != openbao_id
           or bool(state.get("database_engine")) != bool(database_engine) and database_engine)
    if not due:
        return "renewed" if renewal else "fresh"
    try:
        provision(openbao_id, database_engine, state_dir, run, now)
        return "reprovisioned"
    except ClientError as ex:
        if ex.code in FALLBACK_ERRORS or renewal == "forbidden":
            drop(state_dir, revoke=renewal != "forbidden")
            return "dropped"
        return "retry"


def public_state(state_dir=None):
    """The state without secrets, for get-* actions and the UI"""
    state = load_state(state_dir)
    return {
        "active": active(state_dir),
        "openbao_module_id": state.get("openbao_module_id", ""),
        "address": state.get("address", ""),
        "kv_mount": state.get("kv_mount", ""),
        "kv_prefix": state.get("kv_prefix", ""),
        "database_engine": bool(state.get("database_engine")),
        "database_mount": state.get("database_mount", ""),
        "groups": [g["group_id"] for g in state.get("groups") or []],
        "issued_at": int(state.get("issued_at") or 0),
        "renewed_at": int(state.get("renewed_at") or 0),
    }
