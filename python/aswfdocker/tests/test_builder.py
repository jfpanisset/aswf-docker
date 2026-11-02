# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the build command
"""

import os
import unittest
import logging
import tempfile
from unittest.mock import patch

from click.testing import CliRunner

from aswfdocker import builder, buildorder, aswfinfo, index, constants, groupinfo
from aswfdocker.cli import aswfdocker


class TestBuilder(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.build_info = aswfinfo.ASWFInfo(
            repo_uri="notauri", source_branch="testing", aswf_version="2024.123"
        )

    @patch.object(builder.sys, "platform", "darwin")
    def test_bake_dict_sets_linux_amd64_platform_on_macos(self):
        """Match CI arch on Docker Desktop (Apple Silicon defaults to linux/arm64)."""
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        baked = b.make_bake_dict(False)
        self.assertEqual(
            baked["target"]["ci-base-2027"]["platforms"],
            ["linux/amd64"],
        )

    def test_bake_dict_omits_platforms_when_not_darwin(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        for plat in ("linux", "win32"):
            with patch.object(builder.sys, "platform", plat):
                baked = b.make_bake_dict(False)
            self.assertNotIn(
                "platforms",
                baked["target"]["ci-base-2027"],
                msg=f"host sys.platform={plat!r}",
            )

    def test_package_opentimelineio_2027_dict(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["vfx"],
                versions=["2027"],
                type_=constants.ImageType.PACKAGE,
                targets=[],
            ),
        )
        opentimelineio_version = _version_for(
            constants.ImageType.PACKAGE, "opentimelineio", "2027"
        )
        baked = b.make_bake_dict(False)
        target = baked["target"]["ci-package-opentimelineio-2027"]
        self.assertIn("ASWF_OPENTIMELINEIO_VERSION", target["args"])
        # tags may not make much sense for Conan packages
        self.assertEqual(
            target["tags"],
            [f"{constants.DOCKER_REGISTRY}/aswflocaltesting/ci-baseos-gl-conan:2027"],
        )
        self.assertEqual(opentimelineio_version, "2027")
        self.assertEqual(target["args"]["ASWF_VERSION"], "2027")
        self.assertEqual(target["args"]["ASWF_VFXPLATFORM_VERSION"], "2027")
        self.assertEqual(target["args"]["ASWF_CONAN_CHANNEL"], "vfx2027")
        self.assertEqual(target["dockerfile"], "packages/common/Dockerfile")

    def test_package_remote_args_follow_environment(self):
        group_info = groupinfo.GroupInfo(
            names=["vfx"],
            versions=["2027"],
            type_=constants.ImageType.PACKAGE,
            targets=["opentimelineio"],
        )
        for env, expected in (
            ({}, "--no-remote"),
            # package_org for this test's build_info is aswftesting
            ({"GITHUB_ACTIONS": "true"}, "--remote aswftesting"),
        ):
            with patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("GITHUB_ACTIONS", None)
                baked = builder.Builder(self.build_info, group_info).make_bake_dict(
                    False
                )
            args = baked["target"]["ci-package-opentimelineio-2027"]["args"]
            self.assertEqual(args["ASWF_CONAN_REMOTE_ARGS"], expected)
            self.assertNotIn("ASWF_CONAN_NO_REMOTE", args)

    def test_package_builds_get_a_fresh_nonce_per_run(self):
        # Without it BuildKit replays a `conan create` the plan decided must
        # run (e.g. --force-rebuild with unchanged recipes/settings).
        group_info = groupinfo.GroupInfo(
            names=["vfx"],
            versions=["2027"],
            type_=constants.ImageType.PACKAGE,
            targets=["opentimelineio"],
        )
        nonces = [
            builder.Builder(self.build_info, group_info).make_bake_dict(False)[
                "target"
            ]["ci-package-opentimelineio-2027"]["args"]["ASWF_CONAN_NONCE"]
            for _ in range(2)
        ]
        self.assertTrue(all(nonces))
        self.assertNotEqual(nonces[0], nonces[1])

    def test_image_builds_get_no_nonce(self):
        # Replaying cached image layers is fine, and keeps image builds fast.
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        self.assertNotIn(
            "ASWF_CONAN_NONCE",
            b.make_bake_dict(False)["target"]["ci-base-2027"]["args"],
        )

    @patch.object(builder.utils, "git_provenance")
    def test_package_builds_get_git_provenance(self, mock_provenance):
        provenance = {
            "ASWF_GIT_SHA": "abc123",
            "ASWF_GIT_DIRTY": "true",
            "ASWF_GIT_DIFF_HASH": "f00d",
        }
        mock_provenance.return_value = provenance
        group_info = groupinfo.GroupInfo(
            names=["vfx"],
            versions=["2027"],
            type_=constants.ImageType.PACKAGE,
            targets=["opentimelineio", "openexr"],
        )
        targets = builder.Builder(self.build_info, group_info).make_bake_dict(False)[
            "target"
        ]
        for target in ("ci-package-opentimelineio-2027", "ci-package-openexr-2027"):
            self.assertLessEqual(provenance.items(), targets[target]["args"].items())
        # Computed once per run, not per target.
        mock_provenance.assert_called_once()

    @patch.object(builder.utils, "git_provenance")
    def test_image_builds_get_no_git_provenance(self, mock_provenance):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        args = b.make_bake_dict(False)["target"]["ci-base-2027"]["args"]
        self.assertNotIn("ASWF_GIT_SHA", args)
        mock_provenance.assert_not_called()

    def test_image_base_2027_dict(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        base_version = _version_for(constants.ImageType.IMAGE, "base", "2027")
        baked = b.make_bake_dict(False)
        self.assertEqual(
            baked["target"]["ci-base-2027"]["tags"],
            _tags("ci-base", ["2027", base_version], "2027"),
        )
        self.assertEqual(
            baked["target"]["ci-base-2027"]["args"]["ASWF_VERSION"], base_version
        )

    @patch.object(builder.sys, "platform", "linux")
    def test_image_openvdb_clang_dict(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=[
                    index.Index().get_group_from_image(
                        constants.ImageType.IMAGE, "openvdb"
                    )
                ],
                versions=["2027-clang22"],
                type_=constants.ImageType.IMAGE,
                targets=["openvdb"],
            ),
            # Explicit, so the expected args don't depend on GITHUB_ACTIONS.
            conan_remote=constants.CONAN_REMOTE_LOCAL,
        )
        openvdb_version = _version_for(
            constants.ImageType.IMAGE, "openvdb", "2027-clang22"
        )
        self.assertEqual(
            b.make_bake_dict(False),
            {
                "group": {"default": {"targets": ["ci-openvdb-2027-clang22"]}},
                "target": {
                    "ci-openvdb-2027-clang22": _expected_image_target(
                        "openvdb",
                        "2027-clang22",
                        openvdb_version,
                        # a clang variant is also tagged with its VFX year
                        _tags(
                            "ci-openvdb",
                            ["2027", "2027-clang22", openvdb_version],
                            "2027-clang22",
                        ),
                    ),
                },
            },
        )

    @patch.object(builder.sys, "platform", "linux")
    def test_image_base_2026_2027_dict(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2026", "2027"],
                type_=constants.ImageType.IMAGE,
                targets=["base"],
            ),
            # Explicit, so the expected args don't depend on GITHUB_ACTIONS.
            conan_remote=constants.CONAN_REMOTE_LOCAL,
        )
        expected_targets = {}
        for year in ("2026", "2027"):
            base_version = _version_for(constants.ImageType.IMAGE, "base", year)
            expected_targets[f"ci-base-{year}"] = _expected_image_target(
                "base", year, base_version, _tags("ci-base", [year, base_version], year)
            )
        self.assertEqual(
            b.make_bake_dict(False),
            {
                "group": {"default": {"targets": ["ci-base-2026", "ci-base-2027"]}},
                "target": expected_targets,
            },
        )

    def test_image_common_year_selects_every_clang_version(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["common"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        targets = b.make_bake_dict(False)["target"]
        majors = list(index.Index().version_info("2027").clang_versions)
        self.assertEqual(
            sorted(targets), sorted(f"ci-common-2027-clang{m}" for m in majors)
        )
        for major in majors:
            args = targets[f"ci-common-2027-clang{major}"]["args"]
            self.assertEqual(args["ASWF_CLANG_MAJOR_VERSION"], major)
            self.assertEqual(args["ASWF_VFXPLATFORM_VERSION"], "2027")
            self.assertEqual(args["ASWF_CONAN_CHANNEL"], "vfx2027")

    def test_image_common_legacy_tags(self):
        # ci-common keeps its pre-2028 ci_common version tags alongside the
        # VFX Platform year ones, only the newest clang getting the bare one.
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["common"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=[],
            ),
        )
        targets = b.make_bake_dict(False)["target"]
        majors = list(index.Index().version_info("2027").clang_versions)
        oldest, newest = majors[0], majors[-1]
        for major, tags in (
            (oldest, [f"2027-clang{oldest}"]),
            (newest, ["2027", f"2027-clang{newest}"]),
        ):
            version = _version_for(
                constants.ImageType.IMAGE, "common", f"2027-clang{major}"
            )
            legacy = [f"7{tag[4:]}" for tag in [*tags, version]]
            self.assertEqual(
                targets[f"ci-common-2027-clang{major}"]["tags"],
                _tags("ci-common", [*tags, version, *legacy], f"2027-clang{major}"),
            )

    def test_image_base_has_no_legacy_tags(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=["base"],
            ),
        )
        for tag in b.make_bake_dict(False)["target"]["ci-base-2027"]["tags"]:
            self.assertNotIn(":7", tag)

    def test_package_clang_built_per_clang_version(self):
        b = builder.Builder(
            self.build_info,
            groupinfo.GroupInfo(
                names=["common"],
                versions=["2027"],
                type_=constants.ImageType.PACKAGE,
                targets=[],
            ),
        )
        targets = b.make_bake_dict(False)["target"]
        year = index.Index().version_info("2027")
        self.assertEqual(
            sorted(t for t in targets if t.startswith("ci-package-clang-")),
            sorted(f"ci-package-clang-2027-clang{m}" for m in year.clang_versions),
        )
        # The rest of the group is built once for the year
        self.assertIn("ci-package-cmake-2027", targets)
        for major, full in year.clang_versions.items():
            args = targets[f"ci-package-clang-2027-clang{major}"]["args"]
            self.assertEqual(args["ASWF_PKG_VERSION"], full)
            self.assertEqual(args["ASWF_CONAN_CHANNEL"], "vfx2027")


def _version_for(image_type, name, major_version):
    """The full version of `name` built for `major_version`, from versions.yaml."""
    return next(
        v
        for v in index.Index().iter_versions(image_type, name)
        if v.split(".")[0] == major_version
    )


def _tags(image, tags, major_version):
    """Expected image tags: `tags` plus the version's own extra tags (e.g.
    "latest"/"draft"), which change from year to year in versions.yaml."""
    extra = index.Index().version_info(major_version).tags
    return [
        f"{constants.DOCKER_REGISTRY}/aswflocaltesting/{image}:{tag}"
        for tag in [*tags, *extra]
    ]


def _expected_image_target(image, major_version, image_version, tags):
    """The bake target make_bake_dict() should produce for an IMAGE build:
    its fixed args, overlaid by every package version the version pins
    (versions.yaml's all_package_versions)."""
    version_info = index.Index().version_info(major_version)
    args = {
        "ASWF_ORG": "aswflocaltesting",
        "ASWF_PKG_ORG": "aswftesting",
        "ASWF_VFXPLATFORM_VERSION": version_info.major_version,
        "ASWF_CONAN_CHANNEL": version_info.conan_profile,
        "ASWF_CONAN_HOME": constants.ASWF_CONAN_HOME,
        "ASWF_VERSION": image_version,
        "ASWF_CONAN_REMOTE_ARGS": "--no-remote",
        **version_info.all_package_versions,
    }
    return {
        "context": ".",
        "dockerfile": f"ci-{image}/Dockerfile",
        "args": args,
        "labels": {
            "org.opencontainers.image.created": constants.DEV_BUILD_DATE,
            "org.opencontainers.image.revision": constants.DEV_BUILD_DATE,
        },
        "tags": tags,
        "output": ["type=docker"],
        "secret": [
            "id=conan_login_username,env=CONAN_LOGIN_USERNAME",
            "id=conan_password,env=CONAN_PASSWORD",
        ],
    }


def _all_members_need_build(*args, **_kwargs):
    """Stands in for buildorder.compute_build_order() (which runs Docker):
    a flat graph in which every member needs building."""
    members = args[5]
    order = buildorder.BuildOrder([[name for name, _ in members]])
    order.need_build = {name for name, _ in members}
    return order


@patch.object(
    builder.buildorder, "compute_build_order", side_effect=_all_members_need_build
)
class TestBuilderCli(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self._log_handlers = logging.getLogger("").handlers
        logging.getLogger("").handlers = []
        self._i = 0

    def tearDown(self):
        logging.getLogger("").handlers = self._log_handlers

    @staticmethod
    def _would_run(output):
        return [line for line in output.strip().splitlines() if "Would run:" in line]

    def test_builder_cli(self, mock_compute):
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            [
                "build",
                "--ci-image-type",
                "PACKAGE",
                "--version",
                "2027",
                "--target",
                "openexr",
                "--dry-run",
            ],
        )
        self.assertFalse(result.exception, msg=result.output)
        bake_path = os.path.join(
            tempfile.gettempdir(), "docker-bake-PACKAGE-vfx-2027.json"
        )
        # The build order is computed over the whole group, not just the target
        mock_compute.assert_called_once()
        self.assertIn("openexr", [name for name, _ in mock_compute.call_args.args[5]])
        self.assertGreater(len(mock_compute.call_args.args[5]), 1)
        self.assertIn(
            "INFO:aswfdocker.builder:2027: building openexr; up to date: nothing",
            result.output,
        )
        # 1 - docker buildx bake for the conan packages (no login or upload)
        self.assertEqual(
            self._would_run(result.output),
            [
                f"INFO:aswfdocker.builder:Would run: 'docker buildx bake -f {bake_path} "
                + "--set=*.output=type=cacheonly --set=*.target=ci-conan-package-builder "
                + "--progress auto ci-package-openexr-2027'"
            ],
        )
        self.assertEqual(result.exit_code, 0)

    def test_builder_cli_fromtag(self, _mock_compute):
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            ["build", "--full-name", "aswftesting/ci-common:2027-clang22", "--dry-run"],
        )
        self.assertFalse(result.exception)
        bake_path = os.path.join(
            tempfile.gettempdir(), "docker-bake-IMAGE-common-2027-clang22.json"
        )
        cmd = f"docker buildx bake -f {bake_path} --progress auto"
        self.assertEqual(
            result.output,
            f"INFO:aswfdocker.builder:Would run: '{cmd}'\n",
        )
        self.assertEqual(result.exit_code, 0)

    def _assertEndsWith(self, cmds, expected):
        self.assertTrue(cmds[self._i].endswith(expected), "got: " + cmds[self._i])
        self._i += 1

    # Uploading packages is only allowed on GitHub Actions.
    @patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
    def test_builderlist_cli(self, mock_compute):
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            [
                "build",
                "--ci-image-type",
                "PACKAGE",
                "--version",
                "2025",
                "--version",
                "2026",
                "--target",
                "openexr",
                "--dry-run",
                "--build-missing",
                "--push",
                "YES",
            ],
        )
        self.assertFalse(result.exception, msg=result.output)
        bake_path = os.path.join(
            tempfile.gettempdir(), "docker-bake-PACKAGE-vfx-2025-2026.json"
        )
        # One build order per version
        self.assertEqual(mock_compute.call_count, 2)
        cmds = self._would_run(result.output)
        # We expect 2 steps:
        #   docker buildx to build and upload (2x for each openexr package)
        self.assertEqual(len(cmds), 2)
        self.assertEqual(
            cmds[self._i],
            f"INFO:aswfdocker.builder:Would run: 'docker buildx bake -f {bake_path} "
            + "--set=*.output=type=cacheonly --set=*.target=ci-conan-package-builder "
            + "--progress auto ci-package-openexr-2025'",
        )
        self._i += 1
        self.assertEqual(
            cmds[self._i],
            f"INFO:aswfdocker.builder:Would run: 'docker buildx bake -f {bake_path} "
            + "--set=*.output=type=cacheonly --set=*.target=ci-conan-package-builder "
            + "--progress auto ci-package-openexr-2026'",
        )
        self._i += 1
        self.assertEqual(result.exit_code, 0)

    def test_builder_cli_allversions(self, _mock_compute):
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            ["build", "--group", "common", "--version", "all", "--dry-run"],
        )
        self.assertFalse(result.exception)
        bake_path = os.path.join(
            tempfile.gettempdir(),
            "docker-bake-IMAGE-common-2023-clang14-2023-clang15-2024-clang16"
            "-2024-clang17-2025-clang18-2025-clang19-2026-clang19-2026-clang20"
            "-2027-clang21-2027-clang22.json",
        )
        cmd = f"docker buildx bake -f {bake_path} --progress auto"
        self.assertEqual(
            result.output,
            f"INFO:aswfdocker.builder:Would run: '{cmd}'\n",
        )
        self.assertEqual(result.exit_code, 0)

    @staticmethod
    def _package_build(*args):
        return CliRunner().invoke(
            aswfdocker.cli,
            [
                "build",
                "--ci-image-type",
                "PACKAGE",
                "--version",
                "2027",
                "--dry-run",
                *args,
            ],
        )

    def test_target_to_infers_group(self, mock_compute):
        result = self._package_build("--target-to", "openexr")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        bake_path = os.path.join(
            tempfile.gettempdir(), "docker-bake-PACKAGE-vfx-2027.json"
        )
        mock_compute.assert_called_once()
        self.assertEqual(
            self._would_run(result.output),
            [
                f"INFO:aswfdocker.builder:Would run: 'docker buildx bake -f {bake_path} "
                + "--set=*.output=type=cacheonly --set=*.target=ci-conan-package-builder "
                + "--progress auto ci-package-openexr-2027'"
            ],
        )

    def test_package_push_refused_outside_github_actions(self, mock_compute):
        with patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            result = self._package_build("--target", "openexr", "--push", "YES")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("can only be uploaded", result.output)
        mock_compute.assert_not_called()

    def test_image_push_refused_outside_github_actions(self, _mock_compute):
        with patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            result = CliRunner().invoke(
                aswfdocker.cli,
                ["build", "-g", "base", "-v", "2027", "--push", "YES", "--dry-run"],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("can only be uploaded", result.output)

    def test_image_auto_push_refused_outside_github_actions(self, _mock_compute):
        with patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            result = CliRunner().invoke(
                aswfdocker.cli,
                [
                    "--repo-uri",
                    constants.MAIN_GITHUB_ASWF_DOCKER_URL,
                    "--source-branch",
                    "refs/heads/main",
                    "build",
                    "-g",
                    "base",
                    "-v",
                    "2027",
                    "--push",
                    "AUTO",
                    "--dry-run",
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("can only be uploaded", result.output)

    @patch.dict(os.environ, {"GITHUB_ACTIONS": "true"})
    def test_image_push_allowed_on_github_actions(self, _mock_compute):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["build", "-g", "base", "-v", "2027", "--push", "YES", "--dry-run"],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)

    @staticmethod
    def _clang_needs_cmake(*args, **_kwargs):
        """A graph where clang tool-requires cmake, everything needing a build."""
        names = [name for name, _ in args[5]]
        order = buildorder.BuildOrder([names])
        if "clang" in names and "cmake" in names:
            order.add_edge("clang", "cmake")
        order.need_build = set(names)
        return order

    def _bake_targets(self, output):
        return [cmd.rsplit(" ", 1)[-1].rstrip("'") for cmd in self._would_run(output)]

    def test_common_builds_every_clang_version_once_each(self, mock_compute):
        mock_compute.side_effect = self._clang_needs_cmake
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["build", "-t", "PACKAGE", "-g", "common", "-v", "2027", "--dry-run"],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        majors = list(index.Index().version_info("2027").clang_versions)
        # One build order per clang version, each covering the whole group
        self.assertEqual(mock_compute.call_count, len(majors))
        clang_versions = index.Index().version_info("2027").clang_versions
        for call, major in zip(mock_compute.call_args_list, majors):
            self.assertIn(("clang", clang_versions[major]), call.args[5])
            self.assertIn("cmake", [name for name, _ in call.args[5]])
        # ...but the other packages are only built by the first one
        self.assertEqual(
            self._bake_targets(result.output),
            [
                "ci-package-openssl-2027",
                "ci-package-gnutls-2027",
                "ci-package-cmake-2027",
                "ci-package-ninja-2027",
                *(f"ci-package-clang-2027-clang{m}" for m in majors),
            ],
        )

    def test_target_to_clang_builds_its_dependencies_once(self, mock_compute):
        mock_compute.side_effect = self._clang_needs_cmake
        result = self._package_build("--target-to", "clang")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        majors = list(index.Index().version_info("2027").clang_versions)
        self.assertEqual(
            self._bake_targets(result.output),
            [
                "ci-package-cmake-2027",
                *(f"ci-package-clang-2027-clang{m}" for m in majors),
            ],
        )

    def test_target_clang_refuses_unbuilt_cmake(self, mock_compute):
        mock_compute.side_effect = self._clang_needs_cmake
        result = self._package_build("--target", "clang")
        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertEqual(self._would_run(result.output), [])

    def test_single_clang_version(self, mock_compute):
        result = CliRunner().invoke(
            aswfdocker.cli,
            [
                "build",
                "-t",
                "PACKAGE",
                "-g",
                "common",
                "-v",
                "2027-clang22",
                "--dry-run",
            ],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mock_compute.assert_called_once()
        self.assertEqual(
            self._bake_targets(result.output), ["ci-package-clang-2027-clang22"]
        )

    def test_target_to_rejected_for_images(self, _mock_compute):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["build", "--version", "2027", "--target-to", "openexr", "--dry-run"],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("require --ci-image-type PACKAGE", result.output)

    @staticmethod
    def _nothing_needs_build(*args, **kwargs):
        order = _all_members_need_build(*args, **kwargs)
        order.need_build = set()
        return order

    def test_up_to_date_packages_are_skipped(self, mock_compute):
        mock_compute.side_effect = self._nothing_needs_build
        result = self._package_build("--target", "openexr")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(self._would_run(result.output), [])
        self.assertIn("2027: building nothing; up to date: openexr", result.output)

    def test_force_rebuild_builds_up_to_date_packages(self, mock_compute):
        mock_compute.side_effect = self._nothing_needs_build
        result = self._package_build("--target", "openexr", "--force-rebuild")
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(self._would_run(result.output)), 1)
        self.assertIn("2027: building openexr; up to date: nothing", result.output)

    def test_unbuilt_dependency_refuses_with_list(self, mock_compute):
        def imath_needs_build(*args, **_kwargs):
            names = [name for name, _ in args[5]]
            order = buildorder.BuildOrder([names])
            order.add_edge("openexr", "imath")
            order.need_build = {"imath", "openexr"}
            order.refs = {name: {f"{name}/x@aswftesting/vfx2027"} for name in names}
            return order

        mock_compute.side_effect = imath_needs_build
        result = self._package_build("--target", "openexr")
        self.assertEqual(result.exit_code, 1, msg=result.output)
        self.assertIn("imath/x@aswftesting/vfx2027", result.output)
        self.assertEqual(self._would_run(result.output), [])
