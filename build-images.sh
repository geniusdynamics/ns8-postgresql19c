#!/bin/bash

#
# Copyright (C) 2023 Nethesis S.r.l.
# Copyright (C) 2026 Genius Dynamics
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Terminate on error
set -e

# Prepare variables for later use
images=()
# The image will be pushed to GitHub container registry
repobase="${REPOBASE:-ghcr.io/geniusdynamics}"
# Configure the image name
reponame="postgresql19c"
# Node.js 25+ no longer ships Corepack, install it from npm
# renovate: datasource=npm packageName=corepack
corepack_version=0.36.0
# Client endpoint (HAProxy LTS) and optional pgAdmin, pulled at install
# renovate: datasource=docker depName=docker.io/library/haproxy
haproxy_image="docker.io/library/haproxy:3.2.27-alpine"
# renovate: datasource=docker depName=docker.io/dpage/pgadmin4
pgadmin_image="docker.io/dpage/pgadmin4:9.19.0"

# Runtime image: PostgreSQL 19 + Patroni + etcd (server/Containerfile pins
# every version). Published next to the module image with the same tag.
echo "Building the PostgreSQL runtime image..."
buildah build --layers --force-rm \
    --file server/Containerfile \
    --tag "${repobase}/${reponame}-server" \
    server
images+=("${repobase}/${reponame}-server")

# Create a new empty container image
container=$(buildah from scratch)

# Reuse existing nodebuilder-postgresql19c container, to speed up builds
if ! buildah containers --format "{{.ContainerName}}" | grep -q nodebuilder-postgresql19c; then
    echo "Pulling NodeJS runtime..."
    buildah from --name nodebuilder-postgresql19c -v "${PWD}:/usr/src:Z" docker.io/library/node:26.13.0-slim
    buildah run nodebuilder-postgresql19c sh -c "npm install -g corepack@${corepack_version} && corepack enable"
fi

echo "Build static UI files with node..."
buildah run \
    --workingdir=/usr/src/ui \
    --env="NODE_OPTIONS=--openssl-legacy-provider" \
    nodebuilder-postgresql19c \
    sh -c "yarn install && yarn build"

# Add imageroot directory to the container image
buildah add "${container}" imageroot /imageroot
buildah add "${container}" ui/dist /ui
# - routeadm on the node Traefik: the optional pgAdmin route
# - postgresql19c@any:pgpeer: the other instances of this module may run
#   the guarded peer actions (admit-member, also gated by set-peer-access,
#   and reconcile-cluster). Consumers request postgresql19c@any:pgclient.
# - node:fwadm: the optional direct access from outside the cluster opens
#   the public endpoint port in the node firewall (to everyone, or to an
#   allowlist with rich rules). Nothing is opened by default.
# - eight TCP ports: PostgreSQL, Patroni REST, etcd client, etcd peer,
#   client endpoint (read-write), client endpoint (read-only), pgAdmin,
#   public endpoint (TLS only). The others listen on loopback and/or the
#   node cluster VPN address, which is in the trusted firewall zone.
# - openbao@any:secretsclient: client provisioning of ns8-openbao (the
#   module's own token, the database engine ns8-db-<module id>, the group
#   postgresql19c-<cluster id>-peers of the shared secrets)
# - rootless module; core 3.20.1+ as the module template
buildah config --entrypoint=/ \
    --label="org.nethserver.authorizations=traefik@node:routeadm node:fwadm postgresql19c@any:pgpeer openbao@any:secretsclient" \
    --label="org.nethserver.tcp-ports-demand=8" \
    --label="org.nethserver.rootfull=0" \
    --label="org.nethserver.min-core=3.20.1" \
    --label="org.nethserver.volumes=pgdata" \
    --label="org.nethserver.images=${repobase}/${reponame}-server:${IMAGETAG:-latest} ${haproxy_image} ${pgadmin_image}" \
    "${container}"
# Commit the image
buildah commit "${container}" "${repobase}/${reponame}"

# Append the image URL to the images array
images+=("${repobase}/${reponame}")

#
# Setup CI when pushing to Github.
# Warning! docker::// protocol expects lowercase letters (,,)
if [[ -n "${CI}" ]]; then
    # Set output value for Github Actions
    printf "images=%s\n" "${images[*],,}" >> "${GITHUB_OUTPUT}"
else
    # Just print info for manual push
    printf "Publish the images with:\n\n"
    for image in "${images[@],,}"; do printf "  buildah push %s docker://%s:%s\n" "${image}" "${image}" "${IMAGETAG:-latest}" ; done
    printf "\n"
fi
