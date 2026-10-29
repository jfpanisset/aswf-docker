# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
CI Image and Package Builder
"""

import logging
import subprocess
import os
import sys
import tempfile
import typing

from aswfdocker import bake, constants, aswfinfo, utils, groupinfo, index

logger = logging.getLogger(__name__)


class Builder:
    """Builder generates a "docker buildx bake" json file to drive the parallel builds of Docker images."""

    def __init__(
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        push: bool = False,
    ):
        self.push = push
        self.build_info = build_info
        self.group_info = group_info
        self.index = index.Index()

    def make_bake_dict(
        self,
        build_missing: bool,
        no_remote: bool,
    ) -> typing.Dict[str, dict]:
        # pylint: disable=too-many-locals
        root: typing.Dict[str, dict] = {}
        root["target"] = {}
        for image, version in self.group_info.iter_images_versions():
            versions_to_bake = set()
            major_version = utils.get_major_version(version)
            version_info = self.index.version_info(major_version)
            if self.group_info.type == constants.ImageType.PACKAGE:
                if version in versions_to_bake:
                    # Only one version per image needed
                    continue
                # if version_info.ci_common_version != major_version:
                # Only bake conan images in ci_common container!
                # version = version_info.ci_common_version
                # major_version = utils.get_major_version(version)
                versions_to_bake.add(version)
                # tags may not make much sense for Conan packages
                tags = list(
                    map(
                        lambda tag: f"{constants.DOCKER_REGISTRY}/{self.build_info.docker_org}"
                        + f"/ci-baseos-gl-conan:{tag}",
                        [version, major_version],
                    )
                )
                docker_file = constants.CONAN_PACKAGE_DOCKERFILE
            else:
                tags = version_info.get_tags(version, self.build_info.docker_org, image)
                docker_file = f"{image}/Dockerfile"

            channel = utils.conan_channel(
                version_info.ci_common_version,
                major_version,
                version_info.major_version,
            )
            args = utils.common_conan_bake_args(
                self.build_info.docker_org,
                self.build_info.package_org,
                version_info.ci_common_version,
                channel,
            )
            args["ASWF_VERSION"] = version
            if self.group_info.type == constants.ImageType.PACKAGE:
                # params as env var needed for conan build
                args.update(
                    {
                        "ASWF_PKG_NAME": image.replace("ci-package-", ""),
                        "ASWF_PKG_VERSION": version_info.package_versions.get(
                            utils.package_version_key(image.replace("ci-package-", ""))
                        ),
                        "ASWF_CONAN_BUILD_MISSING": (
                            "--build=missing" if build_missing else ""
                        ),
                        "ASWF_CONAN_NO_REMOTE": "--no-remote" if no_remote else "",
                        "ASWF_CONAN_PUSH": "TRUE" if self.push else "",
                    }
                )
            args.update(version_info.all_package_versions)
            target_dict = {
                "context": ".",
                "dockerfile": docker_file,
                "args": args,
                "labels": {
                    "org.opencontainers.image.created": self.build_info.build_date,
                    "org.opencontainers.image.revision": self.build_info.vcs_ref,
                },
                "tags": tags,
                "output": ["type=registry,push=true" if self.push else "type=docker"],
                "secret": [
                    "id=conan_login_username,env=CONAN_LOGIN_USERNAME",
                    "id=conan_password,env=CONAN_PASSWORD",
                ],
            }
            # Docker Desktop on Apple Silicon defaults to linux/arm64; pin amd64 to match CI.
            if sys.platform == "darwin":
                target_dict["platforms"] = ["linux/amd64"]
            root["target"][f"{image}-{major_version}"] = target_dict

        root["group"] = {"default": {"targets": list(root["target"].keys())}}
        return root

    def make_bake_jsonfile(
        self,
        build_missing: bool,
        no_remote: bool,
    ) -> typing.Optional[str]:
        d = self.make_bake_dict(build_missing, no_remote)
        if not d["group"]["default"]["targets"]:
            return None
        groups = "-".join(self.group_info.names)
        versions = "-".join(self.group_info.versions)
        path = os.path.join(
            tempfile.gettempdir(),
            f"docker-bake-{self.group_info.type.name}-{groups}-{versions}.json",
        )
        bake.write_bake_jsonfile(path, d)
        return path

    def _run(self, cmd: typing.List[str], dry_run: bool):
        if dry_run:
            logger.info("Would run: '%s'", " ".join(cmd))
        else:
            logger.info("Building: '%s'", " ".join(cmd))
            subprocess.run(cmd, check=True, cwd=self.build_info.repo_root)

    def _build_conan_package(
        self,
        image,
        version,
        dry_run,
        progress,
        bake_jsonfile,
    ):
        major_version = utils.get_major_version(version)

        # buildx bake --set allows us to override settings in the bake file and avoid having
        #   to rewrite it.
        # output=type=cacheonly : no container is produced, we only want the cache containing
        #   the output of conan builds
        # target=ci-conan-package-builder : see packages/common/Dockerfile for the Conan
        #   build container which runs:
        #
        # - conan create
        # - conan upload main version (conditional)
        #
        runcmd = bake.bake_command(
            bake_jsonfile,
            build_targets=[f"ci-package-{image}-{major_version}"],
            overrides={
                "output": "type=cacheonly",
                "target": "ci-conan-package-builder",
            },
            progress=progress,
        )

        self._run(
            runcmd,
            dry_run=dry_run,
        )

    def build(
        self,
        dry_run: bool = False,
        progress: str = "",
        build_missing=False,
        no_remote=False,
    ) -> None:
        images_and_versions = []
        for image, version in self.group_info.iter_images_versions(get_image=True):
            images_and_versions.append((image, version))

        if not images_and_versions:
            return

        path = self.make_bake_jsonfile(build_missing, no_remote)
        if path:
            if self.group_info.type == constants.ImageType.IMAGE:
                self._run(
                    bake.bake_command(
                        path, build_targets=[], overrides={}, progress=progress
                    ),
                    dry_run=dry_run,
                )
            else:
                conan_base = os.path.join(
                    utils.get_git_top_level(), "packages", "conan"
                )
                # As of Conan 2.18, parallel builds are still not supported and can result in
                # database lock failures, so we have to build sequentially.
                for image, version in images_and_versions:
                    recipe_path = os.path.join(conan_base, "recipes", image)
                    if not os.path.exists(recipe_path):
                        logger.warning("Recipe for %s not found: skipping!", image)
                        continue
                    self._build_conan_package(
                        image,
                        version,
                        dry_run,
                        progress,
                        path,
                    )
