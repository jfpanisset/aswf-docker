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
import uuid

from aswfdocker import (
    bake,
    buildorder,
    constants,
    aswfinfo,
    utils,
    groupinfo,
    index,
)

logger = logging.getLogger(__name__)


class PackageSelection(typing.NamedTuple):
    """Which members of a package group to build; see buildorder.plan_build().
    Empty target lists select the whole group."""

    targets: typing.Sequence[str] = ()
    targets_to: typing.Sequence[str] = ()
    targets_from: typing.Sequence[str] = ()
    force: bool = False


class Builder:
    """Builder generates a "docker buildx bake" json file to drive the parallel builds of Docker images."""

    def __init__(
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        push: bool = False,
        conan_remote: str = constants.CONAN_REMOTE_AUTO,
    ):
        self.push = push
        self.build_info = build_info
        self.group_info = group_info
        self.index = index.Index()
        self.conan_remote = utils.resolve_conan_remote(
            conan_remote, build_info.package_org
        )
        # See ASWF_CONAN_NONCE in packages/common/Dockerfile: the build plan
        # has already decided every package bake runs must build, so BuildKit
        # must never replay one from its layer cache.
        self.conan_nonce = uuid.uuid4().hex
        self._git_provenance_cache: typing.Optional[typing.Dict[str, str]] = None

    def _git_provenance(self) -> typing.Dict[str, str]:
        if self._git_provenance_cache is None:
            self._git_provenance_cache = utils.git_provenance(utils.get_git_top_level())
        return self._git_provenance_cache

    def make_bake_dict(
        self,
        build_missing: bool,
    ) -> typing.Dict[str, dict]:
        root: typing.Dict[str, dict] = {}
        root["target"] = {}
        for image, version in self.group_info.iter_images_versions():
            major_version = utils.get_major_version(version)
            version_info = self.index.version_info(major_version)
            if self.group_info.type == constants.ImageType.PACKAGE:
                # tags may not make much sense for Conan packages
                tags = [
                    f"{constants.DOCKER_REGISTRY}/{self.build_info.docker_org}"
                    + f"/ci-baseos-gl-conan:{version_info.release}"
                ]
                docker_file = constants.CONAN_PACKAGE_DOCKERFILE
            else:
                tags = version_info.get_tags(version, self.build_info.docker_org, image)
                docker_file = f"{image}/Dockerfile"

            args = utils.common_conan_bake_args(
                self.build_info.docker_org,
                self.build_info.package_org,
                version_info,
            )
            args["ASWF_VERSION"] = version
            # Both package builds (conan create) and image builds (conan
            # install) resolve packages, so both get the remote policy.
            args["ASWF_CONAN_REMOTE_ARGS"] = " ".join(
                utils.conan_remote_args(self.conan_remote)
            )
            if self.group_info.type == constants.ImageType.PACKAGE:
                # params as env var needed for conan build
                args.update(
                    {
                        "ASWF_PKG_NAME": image.replace("ci-package-", ""),
                        "ASWF_PKG_VERSION": version_info.all_package_versions.get(
                            utils.package_version_key(image.replace("ci-package-", ""))
                        ),
                        "ASWF_CONAN_BUILD_MISSING": (
                            "--build=missing" if build_missing else ""
                        ),
                        "ASWF_CONAN_PUSH": "TRUE" if self.push else "",
                        "ASWF_CONAN_NONCE": self.conan_nonce,
                        # Recorded in each built package's Conan metadata by
                        # the hook_aswf_provenance hook, for image SBOMs.
                        **self._git_provenance(),
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
            }
            # Artifactory downloads are anonymous: only package uploads need
            # the credentials.
            if self.group_info.type == constants.ImageType.PACKAGE and self.push:
                target_dict["secret"] = [
                    "id=conan_login_username,env=CONAN_LOGIN_USERNAME",
                    "id=conan_password,env=CONAN_PASSWORD",
                ]
            # Docker Desktop on Apple Silicon defaults to linux/arm64; pin amd64 to match CI.
            if sys.platform == "darwin":
                target_dict["platforms"] = ["linux/amd64"]
            root["target"][f"{image}-{major_version}"] = target_dict

        root["group"] = {"default": {"targets": list(root["target"].keys())}}
        return root

    def make_bake_jsonfile(
        self,
        build_missing: bool,
    ) -> typing.Optional[str]:
        d = self.make_bake_dict(build_missing)
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
        selection: typing.Optional[PackageSelection] = None,
    ) -> None:
        images_and_versions = []
        for image, version in self.group_info.iter_images_versions(get_image=True):
            images_and_versions.append((image, version))

        if not images_and_versions:
            return

        path = self.make_bake_jsonfile(build_missing)
        if path:
            if self.group_info.type == constants.ImageType.IMAGE:
                self._run(
                    bake.bake_command(
                        path, build_targets=[], overrides={}, progress=progress
                    ),
                    dry_run=dry_run,
                )
            else:
                self._build_conan_packages(
                    images_and_versions,
                    path,
                    dry_run,
                    progress,
                    selection or PackageSelection(),
                )

    def _conan_build_passes(
        self, images_and_versions: typing.Sequence[typing.Tuple[str, str]]
    ) -> typing.Iterator[typing.Tuple[str, typing.List[typing.Tuple[str, str]]]]:
        """Splits the (package, version) pairs to build into build passes:
        one per year, or, when per-clang packages (see per_clang_packages in
        versions.yaml) are included, one per clang version of that year.
        Each pass holds the year's other packages too, so that its build
        order covers dependencies between them (e.g. clang tool-requiring
        cmake). Yields (pass version, its (package, version) pairs)."""
        by_year: typing.Dict[str, typing.List[typing.Tuple[str, str]]] = {}
        for image, version in images_and_versions:
            year = utils.get_major_version(version).split("-")[0]
            by_year.setdefault(year, []).append((image, version))
        for year, pairs in by_year.items():
            per_clang: typing.Dict[str, typing.List[typing.Tuple[str, str]]] = {}
            others = []
            for image, version in pairs:
                major_version = utils.get_major_version(version)
                if image in self.index.per_clang_packages:
                    per_clang.setdefault(major_version, []).append((image, version))
                else:
                    others.append((image, version))
            if not per_clang:
                yield year, others
            for clang_version, clang_pairs in per_clang.items():
                yield clang_version, others + clang_pairs

    def _build_conan_packages(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        images_and_versions: typing.Sequence[typing.Tuple[str, str]],
        bake_jsonfile: str,
        dry_run: bool,
        progress: str,
        selection: PackageSelection,
    ) -> None:
        """Builds the selected members of the group, pass by pass (see
        _conan_build_passes()), in the order Conan's dependency graph
        dictates, skipping packages whose binary is already up to date
        (unless forced) or that an earlier pass of the same year builds.
        The graph is computed even for a dry run: it's the only way to know
        what would be built."""
        repo_root = utils.get_git_top_level()
        with_recipes = []
        for image, version in images_and_versions:
            if not os.path.exists(
                os.path.join(repo_root, "packages", "conan", "recipes", image)
            ):
                logger.warning("Recipe for %s not found: skipping!", image)
                continue
            with_recipes.append((image, version))

        # Per year: packages an earlier pass builds (per-clang ones excluded,
        # as each pass builds a different version of them).
        done: typing.Dict[str, typing.Set[str]] = {}
        for pass_version, pairs in self._conan_build_passes(with_recipes):
            versions = dict(pairs)
            version_info = self.index.version_info(pass_version)
            year_done = done.setdefault(version_info.major_version, set())
            channel = version_info.conan_channel
            members = [
                (
                    image,
                    version_info.all_package_versions[utils.package_version_key(image)],
                )
                for image in versions
            ]

            logger.info(
                "Computing Conan build order for %s (%s)", pass_version, channel
            )
            order = buildorder.compute_build_order(
                repo_root,
                self.build_info.docker_org,
                self.build_info.package_org,
                version_info,
                members,
                conan_remote=self.conan_remote,
            )
            plan = buildorder.plan_build(
                order,
                list(versions),
                selection.targets,
                selection.targets_to,
                selection.targets_from,
                selection.force,
                done=year_done,
            )
            logger.info(
                "%s: building %s; up to date: %s",
                pass_version,
                ", ".join(plan.to_build) or "nothing",
                ", ".join(plan.up_to_date) or "nothing",
            )
            year_done.update(
                image
                for image in plan.to_build
                if image not in self.index.per_clang_packages
            )
            # As of Conan 2.18, parallel builds are still not supported and can result in
            # database lock failures, so we have to build sequentially.
            for image in plan.to_build:
                self._build_conan_package(
                    image,
                    versions[image],
                    dry_run,
                    progress,
                    bake_jsonfile,
                )
