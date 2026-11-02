# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Utility functions
"""

import hashlib
import os
import re
import subprocess
import datetime
import logging
import typing
import requests

from aswfdocker import constants

logger = logging.getLogger(__name__)


def get_current_branch() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], encoding="UTF-8"
    )[:-1]


def get_current_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], encoding="UTF-8")[:-1]


def get_git_top_level() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], encoding="UTF-8"
    )[:-1]


def git_provenance(repo_root: str) -> typing.Dict[str, str]:
    """The git state a package build comes from, as the build ARGs
    packages/common/Dockerfile passes to the hook_aswf_provenance Conan hook:
    the HEAD commit, whether the working tree differs from it (including
    untracked, non-ignored files), and if so a hash of those differences.

    Release builds run from a clean checkout, so the SHA alone identifies
    them. Local builds are often dirty; the hash then at least tells two
    local builds from identical trees apart from ones that differ.
    """

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=repo_root)

    status = git("status", "--porcelain", "--untracked-files=all")
    provenance = {
        "ASWF_GIT_SHA": git("rev-parse", "HEAD").decode().strip(),
        "ASWF_GIT_DIRTY": "true" if status.strip() else "false",
        "ASWF_GIT_DIFF_HASH": "",
    }
    if status.strip():
        digest = hashlib.sha256(git("diff", "HEAD", "--binary"))
        untracked = git("ls-files", "--others", "--exclude-standard", "-z")
        for path in sorted(p for p in untracked.split(b"\0") if p):
            digest.update(b"\0untracked\0" + path + b"\0")
            with open(os.path.join(repo_root, os.fsdecode(path)), "rb") as f:
                digest.update(f.read())
        provenance["ASWF_GIT_DIFF_HASH"] = digest.hexdigest()
    return provenance


def get_current_date() -> str:
    # 2020-01-19T08:04:51Z
    return datetime.datetime.now().isoformat(timespec="seconds") + "Z"


def get_docker_org(repo_uri: str, source_branch: str) -> str:
    if not source_branch and not repo_uri:
        return constants.TESTING_DOCKER_ORG
    if (
        source_branch == "refs/heads/main"
        and constants.MAIN_GITHUB_ASWF_DOCKER_URL.endswith(repo_uri)
    ):
        docker_org = constants.PUBLISH_DOCKER_ORG
    elif source_branch in ("refs/heads/testing", ""):
        docker_org = constants.TESTING_DOCKER_ORG
    else:
        docker_org = constants.FAKE_DOCKER_ORG
    return docker_org


def get_docker_push(repo_uri: str, source_branch: str) -> bool:
    if (
        source_branch == "refs/heads/main"
        and repo_uri == constants.MAIN_GITHUB_ASWF_DOCKER_URL
    ) or source_branch == "refs/heads/testing":
        return True
    return False


def get_major_version(version: str) -> str:
    return version.split(".")[0]


def get_image_name(image_type: constants.ImageType, image: str):
    if image_type == constants.ImageType.PACKAGE:
        return f"ci-package-{image}"
    return f"ci-{image}"


IMAGE_NAME_REGEXC = re.compile(constants.IMAGE_NAME_REGEX)


def get_image_spec(name: str):
    m = IMAGE_NAME_REGEXC.match(name)
    if not m:
        raise RuntimeError(
            f"Image name does not conform to expected format: {constants.IMAGE_NAME_REGEX}"
        )
    org = m.group("org")
    if m.group("package"):
        image_type = constants.ImageType.PACKAGE
    else:
        image_type = constants.ImageType.IMAGE
    image = m.group("image")
    version = m.group("version")
    logger.debug("get_image_spec found %s: %s/%s:%s", image_type, org, image, version)
    return org, image_type, image, version


def on_github_actions() -> bool:
    """Whether this runs on GitHub Actions (GITHUB_ACTIONS=true is set on
    every runner): the only place holding the Artifactory credentials, so
    the only place Conan packages may be uploaded from."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


def resolve_conan_remote(conan_remote: str, package_org: str) -> str:
    """Which Conan remote(s) package resolution may use: "local" (the local
    Conan cache only) or the name of the one remote to consult.

    "auto" picks by environment: local builds only ever use the local cache,
    while on GitHub Actions (GITHUB_ACTIONS=true, set on every runner) the
    one remote is `package_org` -- the same Artifactory remote packages get
    uploaded to, so later builds find earlier uploads. Any other value is
    an explicit choice and returned as-is.
    """
    if conan_remote != constants.CONAN_REMOTE_AUTO:
        return conan_remote
    if on_github_actions():
        return package_org
    return constants.CONAN_REMOTE_LOCAL


def conan_remote_args(conan_remote: str) -> typing.List[str]:
    """Conan command-line arguments enforcing an already-resolved remote
    policy (see resolve_conan_remote())."""
    if conan_remote == constants.CONAN_REMOTE_AUTO:
        raise ValueError("conan_remote must be resolved before use")
    if conan_remote == constants.CONAN_REMOTE_LOCAL:
        return ["--no-remote"]
    return ["--remote", conan_remote]


def package_version_key(package_name: str) -> str:
    """The versions.yaml key pinning a Conan package's version, e.g.
    "util-linux-libuuid" -> "ASWF_UTIL_LINUX_LIBUUID_VERSION"."""
    return f"ASWF_{package_name.upper().replace('-', '_')}_VERSION"


def common_conan_bake_args(
    docker_org: str, package_org: str, major_version: str, channel: str
) -> typing.Dict[str, str]:
    """The subset of packages/common/Dockerfile build ARGs needed by every
    bake target built from it, whether a real `conan create` package build
    (builder.py) or the read-only dependency-graph grapher (buildorder.py).
    `major_version` is the VFX Platform year, whose ci-baseos-gl-conan image
    packages are built in.
    """
    return {
        "ASWF_ORG": docker_org,
        "ASWF_PKG_ORG": package_org,
        "ASWF_VFXPLATFORM_VERSION": major_version,
        "ASWF_CONAN_CHANNEL": channel,
        "ASWF_CONAN_HOME": constants.ASWF_CONAN_HOME,
    }


def get_dockerhub_token(username, password):
    body = {"username": username, "password": password}
    response = requests.post(
        "https://hub.docker.com/v2/users/login", json=body, timeout=5
    )
    return response.json()["token"]
