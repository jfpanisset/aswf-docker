#!/usr/bin/env bash
# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

# Installs the Conan packages of an image, listed in
# ${ASWF_INSTALL_PREFIX}/conanfile.txt, into ${ASWF_INSTALL_PREFIX} with the
# aswf_deploy deployer (packages/conan/settings/extensions/deployers), which
# also writes the image's SBOM to ${ASWF_INSTALL_PREFIX}/aswf/sbom.cdx.json.
# Run by the RUN of ci-image-dockerfile.jinja2 installing them, with the
# image's build ARGs in its environment.
#
# Usage: install_conan_packages.sh <image name> <SBOM name>
#   <image name>: the image's own package (e.g. openexr for ci-openexr), not
#                 installed, as the image is for building it
#   <SBOM name>:  the SBOM's subject, e.g. ci-openexr/2027.4

set -euo pipefail

image_name=$1
sbom_name=$2

# e.g. "--remote aswf", or "--no-remote" for local builds (see
# aswfdocker's utils.conan_remote_args()).
read -ra remote_args <<< "${ASWF_CONAN_REMOTE_ARGS}"

# conan install doesn't download package metadata, which holds the build
# provenance aswf_deploy records in the image SBOM, so packages coming from
# a remote are first downloaded with it.
if [[ "${ASWF_CONAN_REMOTE_ARGS}" != "--no-remote" ]]; then
    graph=$(mktemp --suffix=.json)
    conan graph info "${ASWF_INSTALL_PREFIX}" \
        --profile:all "${ASWF_CONAN_PROFILE}" \
        "${remote_args[@]}" \
        --format=json > "${graph}"
    conan aswf-download-metadata "${graph}" \
        "${remote_args[@]}" \
        --metadata aswf_provenance.json
    rm "${graph}"
fi

conan install "${ASWF_INSTALL_PREFIX}" \
    --profile:all "${ASWF_CONAN_PROFILE}" \
    "${remote_args[@]}" \
    --deployer aswf_deploy \
    --deployer-folder "${ASWF_INSTALL_PREFIX}" \
    --conf user.aswf:exclude_package="${image_name}" \
    --conf user.aswf:sbom_name="${sbom_name}"
