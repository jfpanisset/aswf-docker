# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the utility commands
"""

import os
import subprocess
import tempfile
import unittest
import logging
from unittest.mock import patch

from click.testing import CliRunner

from aswfdocker import utils, index, constants
from aswfdocker.cli import aswfdocker


class TestConanRemotePolicy(unittest.TestCase):
    def test_auto_is_local_outside_github_actions(self):
        with patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            self.assertEqual(
                utils.resolve_conan_remote(constants.CONAN_REMOTE_AUTO, "aswf"),
                constants.CONAN_REMOTE_LOCAL,
            )

    def test_auto_is_package_org_on_github_actions(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(
                utils.resolve_conan_remote(constants.CONAN_REMOTE_AUTO, "aswf"),
                "aswf",
            )

    def test_explicit_policy_ignores_environment(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(
                utils.resolve_conan_remote(constants.CONAN_REMOTE_LOCAL, "aswf"),
                constants.CONAN_REMOTE_LOCAL,
            )
            self.assertEqual(
                utils.resolve_conan_remote("aswftesting", "aswf"), "aswftesting"
            )

    def test_remote_args(self):
        self.assertEqual(
            utils.conan_remote_args(constants.CONAN_REMOTE_LOCAL), ["--no-remote"]
        )
        self.assertEqual(utils.conan_remote_args("aswf"), ["--remote", "aswf"])
        with self.assertRaises(ValueError):
            utils.conan_remote_args(constants.CONAN_REMOTE_AUTO)


class TestGitProvenance(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        self._git("init", "-q")
        self._write(".gitignore", "ignored.txt\n")
        self._write("tracked.txt", "one\n")
        self._git("add", ".")
        self._git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "initial",
        )
        self.head = self._git("rev-parse", "HEAD").strip()

    def _git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, encoding="UTF-8")

    def _write(self, name, content):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_clean_tree(self):
        self.assertEqual(
            utils.git_provenance(self.repo),
            {
                "ASWF_GIT_SHA": self.head,
                "ASWF_GIT_DIRTY": "false",
                "ASWF_GIT_DIFF_HASH": "",
            },
        )

    def test_ignored_files_keep_tree_clean(self):
        self._write("ignored.txt", "noise\n")
        self.assertEqual(utils.git_provenance(self.repo)["ASWF_GIT_DIRTY"], "false")

    def test_modified_tracked_file(self):
        self._write("tracked.txt", "two\n")
        first = utils.git_provenance(self.repo)
        self.assertEqual(first["ASWF_GIT_SHA"], self.head)
        self.assertEqual(first["ASWF_GIT_DIRTY"], "true")
        self.assertEqual(len(first["ASWF_GIT_DIFF_HASH"]), 64)
        # Same tree, same hash; a different change, a different hash.
        self.assertEqual(utils.git_provenance(self.repo), first)
        self._write("tracked.txt", "three\n")
        self.assertNotEqual(
            utils.git_provenance(self.repo)["ASWF_GIT_DIFF_HASH"],
            first["ASWF_GIT_DIFF_HASH"],
        )

    def test_untracked_file_contents_are_hashed(self):
        self._write("new.txt", "a\n")
        first = utils.git_provenance(self.repo)
        self.assertEqual(first["ASWF_GIT_DIRTY"], "true")
        self._write("new.txt", "b\n")
        self.assertNotEqual(
            utils.git_provenance(self.repo)["ASWF_GIT_DIFF_HASH"],
            first["ASWF_GIT_DIFF_HASH"],
        )


class TestUtils(unittest.TestCase):
    def test_get_docker_org(self):
        self.assertEqual(utils.get_docker_org("", ""), "aswftesting")
        self.assertEqual(
            utils.get_docker_org(
                constants.MAIN_GITHUB_ASWF_DOCKER_URL,
                "refs/heads/main",
            ),
            "aswf",
        )
        self.assertEqual(
            utils.get_docker_org(
                constants.MAIN_GITHUB_ASWF_DOCKER_URL,
                "refs/heads/testing",
            ),
            "aswftesting",
        )
        self.assertEqual(
            utils.get_docker_org(
                "https://github.com/randomfork/aswf-docker", "refs/heads/main"
            ),
            "aswflocaltesting",
        )
        self.assertEqual(
            utils.get_docker_org(
                "https://github.com/randomfork/aswf-docker", "refs/heads/randombranch"
            ),
            "aswflocaltesting",
        )

    def test_get_docker_push(self):
        self.assertFalse(utils.get_docker_push("", ""))
        self.assertTrue(
            utils.get_docker_push(
                constants.MAIN_GITHUB_ASWF_DOCKER_URL,
                "refs/heads/main",
            )
        )
        self.assertTrue(
            utils.get_docker_push(
                constants.MAIN_GITHUB_ASWF_DOCKER_URL,
                "refs/heads/testing",
            )
        )
        self.assertFalse(
            utils.get_docker_push(
                "https://github.com/randomfork/aswf-docker", "refs/heads/main"
            )
        )
        self.assertFalse(
            utils.get_docker_push(
                "https://github.com/randomfork/aswf-docker", "refs/heads/randombranch"
            )
        )

    def test_image_def_from_name(self):
        with self.assertRaises(RuntimeError):
            utils.get_image_spec("aswf/ci-package-openvdb_2027")
        self.assertEqual(
            utils.get_image_spec("aswftesting/ci-common:7"),
            ("aswftesting", constants.ImageType.IMAGE, "common", "7"),
        )
        self.assertEqual(
            utils.get_image_spec("aswf/ci-package-openvdb:2027"),
            ("aswf", constants.ImageType.PACKAGE, "openvdb", "2027"),
        )
        self.assertEqual(
            utils.get_image_spec("refs/tags/aswf/ci-package-openvdb/2027"),
            ("aswf", constants.ImageType.PACKAGE, "openvdb", "2027"),
        )
        self.assertEqual(
            utils.get_image_spec("aswf/ci-package-clang:7-clang22"),
            ("aswf", constants.ImageType.PACKAGE, "clang", "7-clang22"),
        )


class TestUtilsCli(unittest.TestCase):
    def setUp(self):
        self._log_handlers = logging.getLogger("").handlers
        logging.getLogger("").handlers = []
        self.maxDiff = None

    def tearDown(self):
        logging.getLogger("").handlers = self._log_handlers

    def test_cli_getdockerorg(self):
        runner = CliRunner()
        result = runner.invoke(aswfdocker.cli, ["getdockerorg"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "aswftesting")

    def test_cli_getdockerorgforced(self):
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            [
                "--repo-uri",
                "https://github.com/AcademySoftwareFoundation/aswf-docker",
                "--source-branch",
                "refs/heads/main",
                "getdockerorg",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "aswf")

    def test_cli_getdockerpush(self):
        runner = CliRunner()
        result = runner.invoke(aswfdocker.cli, ["getdockerpush"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "false")

    def test_cli_packages(self):
        runner = CliRunner()
        result = runner.invoke(aswfdocker.cli, ["packages"], catch_exceptions=False)
        self.assertEqual(result.exit_code, 0)
        pkgs = result.output.split("\n")
        self.assertGreater(len(pkgs), 20)
        clang_version = list(
            index.Index().iter_versions(constants.ImageType.PACKAGE, "openssl")
        )[0]
        self.assertEqual(
            pkgs[0],
            f"common/ci-package-openssl:{clang_version}",
        )

    def test_cli_images(self):
        runner = CliRunner()
        result = runner.invoke(aswfdocker.cli, ["images"], catch_exceptions=False)
        self.assertEqual(result.exit_code, 0)
        imgs = result.output.split("\n")
        self.assertGreater(len(imgs), 15)
        baseos_gl_conan_version = list(
            index.Index().iter_versions(constants.ImageType.IMAGE, "baseos-gl-conan")
        )[0]
        self.assertEqual(
            imgs[0],
            f"baseos-gl-conan/ci-baseos-gl-conan:{baseos_gl_conan_version}",
        )
