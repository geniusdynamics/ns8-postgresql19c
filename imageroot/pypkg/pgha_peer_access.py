#
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

"""Approvals of the instances allowed to join the cluster of this
instance (the set-peer-access pattern of ns8-kumomta).

The pgpeer role (authorization postgresql19c@any:pgpeer) is granted by NS8
to every ns8-postgresql19c instance. admit-member hands the cluster
secrets over, so it also requires an explicit approval of the caller by
the administrator, bound to the caller's module UUID: a new installation
that reuses a module ID needs a new approval. The approvals are not
backed up and not cloned.
"""

import contextlib
import fcntl
import json
import os

import agent
import pgha

ACCESS_DIR = "peer-access"
GRANTS = os.path.join(ACCESS_DIR, "grants.json")


@contextlib.contextmanager
def access_lock(mode):
    os.makedirs(pgha.state_path(ACCESS_DIR), mode=0o700, exist_ok=True)
    fd = os.open(pgha.state_path(ACCESS_DIR, "lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, mode)
        yield


def read_grants():
    try:
        with open(pgha.state_path(GRANTS)) as fp:
            return json.load(fp)
    except FileNotFoundError:
        return {}


def image_name_of(image_url):
    return (image_url or "").rsplit("/", 1)[-1].split(":", 1)[0].split("@", 1)[0]


def own_image_name():
    return image_name_of(os.environ.get("IMAGE_URL", "")) or pgha.IMAGE_NAME


def registered_uuid(peer, rdb=None):
    """UUID of an installed ns8-postgresql19c instance, None otherwise"""
    if peer == os.environ.get("MODULE_ID") or not pgha.MODULE_ID_RE.fullmatch(peer or ""):
        return None
    rdb = rdb or agent.redis_connect(use_replica=True)
    module_uuid = rdb.hget("cluster/module_uuid", peer)
    image_url = rdb.hget(f"module/{peer}/environment", "IMAGE_URL")
    return module_uuid if image_name_of(image_url) == own_image_name() else None


def set_peer_access(peer, allowed):
    with access_lock(fcntl.LOCK_EX):
        grants = read_grants()
        if allowed:
            module_uuid = registered_uuid(peer)
            if not module_uuid:
                pgha.validation_error("peer", peer, "not_a_postgresql19c_instance")
            grants[peer] = module_uuid
        else:
            grants.pop(peer, None)
        pgha.write_private(pgha.state_path(GRANTS), json.dumps(grants, sort_keys=True) + "\n")


def approved_peers(rdb=None):
    """Effective approvals (grants whose UUID still matches)"""
    grants = read_grants()
    if not grants:
        return set()
    rdb = rdb or agent.redis_connect(use_replica=True)
    uuids = rdb.hmget("cluster/module_uuid", list(grants))
    return {peer for peer, uid in zip(grants, uuids) if uid and uid == grants[peer]}


def peer_candidates(rdb=None):
    """Other ns8-postgresql19c instances of the NS8 cluster, with their
    approval state"""
    rdb = rdb or agent.redis_connect(use_replica=True)
    approved = approved_peers(rdb)
    result = []
    for module_id, node_id in (rdb.hgetall("cluster/module_node") or {}).items():
        if module_id == os.environ.get("MODULE_ID"):
            continue
        if image_name_of(rdb.hget(f"module/{module_id}/environment", "IMAGE_URL")) != own_image_name():
            continue
        result.append({"module_id": module_id, "node": str(node_id), "allowed": module_id in approved,
                       "ui_name": rdb.get(f"module/{module_id}/ui_name") or ""})
    return sorted(result, key=lambda c: pgha.member_sort_key(c["module_id"]))


@contextlib.contextmanager
def authorized_peer(module_id):
    """The authenticated caller must be the module it claims to be, and be
    approved on this instance"""
    user = os.environ.get("AGENT_TASK_USER", "")
    prefix, _, peer = user.partition("/")
    if prefix != "module" or peer != module_id or not pgha.MODULE_ID_RE.fullmatch(peer):
        pgha.validation_error("module_id", module_id, "peer_not_authorized")
    with access_lock(fcntl.LOCK_SH):
        expected = read_grants().get(peer)
        if not expected or expected != registered_uuid(peer):
            pgha.validation_error("module_id", module_id, "peer_not_approved")
        yield peer
