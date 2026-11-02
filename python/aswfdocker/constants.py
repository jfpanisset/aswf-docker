# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Main configuration and constants for aswfdocker
"""

import enum


class ImageType(enum.Enum):
    IMAGE = "image"
    PACKAGE = "package"


PUBLISH_DOCKER_ORG = "aswf"
TESTING_DOCKER_ORG = "aswftesting"
# this org is not valid, but this ensures that the test will not accidently pull an existing image
FAKE_DOCKER_ORG = "aswflocaltesting"

DOCKER_REGISTRY = "docker.io"

DEV_BUILD_DATE = "dev"
DEV_VCS_REF = "dev"

MAIN_GITHUB_ASWF_ORG = "AcademySoftwareFoundation"
MAIN_GITHUB_REPO_NAME = "aswf-docker"
MAIN_GITHUB_ASWF_DOCKER_URL = (
    f"https://github.com/{MAIN_GITHUB_ASWF_ORG}/{MAIN_GITHUB_REPO_NAME}"
)

CCI_GITHUB_ORG = "conan-io"
CCI_GITHUB_REPO_NAME = "conan-center-index"
CCI_DEFAULT_BRANCH = "master"

# pylint: disable=line-too-long
IMAGE_NAME_REGEX = r"(refs/tags/)?(?P<org>[a-z]+)/ci-(?P<package>package\-)?(?P<image>[a-z0-9_-]+)[:/](?P<version>[0-9\.a-z\-]+)"

ALL = "all"

# Images also tagged with their year's legacy_common_version (see
# versions.yaml), e.g. ci-common:7-clang22 as well as ci-common:2027-clang22,
# from when they were versioned separately from the VFX Platform year.
LEGACY_COMMON_TAGGED_IMAGES = ("ci-baseos-gl-conan", "ci-common")

ASWF_CONAN_HOME = "/opt/conan_home"

# Conan remote policy values (see utils.resolve_conan_remote()): any other
# value names the single Conan remote to use.
CONAN_REMOTE_AUTO = "auto"
CONAN_REMOTE_LOCAL = "local"

# Dockerfile (relative to the repo root) every Conan package build and the
# Conan dependency-graph grapher are baked from.
CONAN_PACKAGE_DOCKERFILE = "packages/common/Dockerfile"
