# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


import unittest
from unittest import mock
import logging

from click.testing import CliRunner

from aswfdocker import releaser, aswfinfo, index, constants, groupinfo, utils
from aswfdocker.cli import aswfdocker


class TestReleaser(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.build_info = aswfinfo.ASWFInfo(
            repo_uri="notauri", source_branch="testing", aswf_version="2027.123"
        )

    def test_image_base_2027_dict(self):
        r = releaser.Releaser(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=["base"],
            ),
            sha=utils.get_current_sha(),
        )
        r.gh.repo.create_git_tag_and_release = mock.MagicMock()

        class U:
            name = "testuser"
            email = "testuser@test.user"

        r.gh.github.get_user = mock.MagicMock(return_value=U())

        r.gather()
        r.release(dry_run=False)
        base_version = next(
            v
            for v in index.Index().iter_versions(constants.ImageType.IMAGE, "base")
            if v.startswith("2027.")
        )
        r.gh.repo.create_git_tag_and_release.assert_called_once_with(
            tag=f"aswflocaltesting/ci-base/{base_version}",
            draft=False,
            object=utils.get_current_sha(),
            prerelease=False,
            release_message="Inspect released Docker image here: "
            f"https://hub.docker.com/r/aswflocaltesting/ci-base/tags?name={base_version}",
            release_name=f"aswflocaltesting/ci-base:{base_version}",
            tag_message=f"aswflocaltesting/ci-base:{base_version}",
            type="commit",
            tagger=mock.ANY,
        )


class TestReleaserCli(unittest.TestCase):
    def setUp(self):
        self._log_handlers = logging.getLogger("").handlers
        logging.getLogger("").handlers = []
        self.maxDiff = None

    def tearDown(self):
        logging.getLogger("").handlers = self._log_handlers

    def test_release_cli(self):
        current_version = list(
            index.Index().iter_versions(constants.ImageType.IMAGE, "base")
        )[1]
        runner = CliRunner()
        result = runner.invoke(
            aswfdocker.cli,
            [
                "release",
                "-n",
                f"aswf/ci-base:{current_version}",
                "--sha",
                utils.get_current_sha(),
                "--dry-run",
            ],
            input="y\n",
        )
        self.assertFalse(result.exception)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            result.output,
            # pylint: disable=line-too-long
            f"""Are you sure you want to create the following 1 release on sha={utils.get_current_sha()}?
aswf/ci-base:{current_version}
 [y/N]: y
INFO:aswfdocker.releaser:Would create this GitHub release on current commit: aswf/ci-base:{current_version}
Release done.
""",
        )

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_package(self, mock_github):
        # A package release starts the package release workflow, which builds
        # and uploads the packages from GitHub Actions, without creating any
        # GitHub release.
        mock_github.return_value.repo.default_branch = "main"
        sha = utils.get_current_sha()
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-n", "aswf/ci-package-boost:2027", "--sha", sha],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn("aswf/ci-package-boost:2027", result.output)
        gh = mock_github.return_value
        gh.create_release.assert_not_called()
        gh.dispatch_workflow.assert_called_once_with(
            constants.PACKAGE_RELEASE_WORKFLOW,
            "main",
            {
                "sha": sha,
                "package_org": "aswf",
                "groups": "base1",
                "versions": "2027",
                "targets": "boost",
                "force_rebuild": "false",
            },
        )

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_package_group(self, mock_github):
        result = CliRunner().invoke(
            aswfdocker.cli,
            [
                "release",
                "-t",
                "PACKAGE",
                "-g",
                "common",
                "-v",
                "2027",
                "--docker-org",
                "aswf",
                "--sha",
                "abc123",
                "--force-rebuild",
                "--workflow-ref",
                "my-branch",
            ],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Every clang version of the year is listed
        self.assertIn("aswf/ci-package-clang:2027-clang22", result.output)
        self.assertIn("aswf/ci-package-cmake:2027", result.output)
        mock_github.return_value.dispatch_workflow.assert_called_once_with(
            constants.PACKAGE_RELEASE_WORKFLOW,
            "my-branch",
            {
                "sha": "abc123",
                "package_org": "aswf",
                "groups": "common",
                "versions": "2027",
                "targets": "",
                "force_rebuild": "true",
            },
        )

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_package_dry_run(self, mock_github):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-n", "aswf/ci-package-boost:2027", "--sha", "abc123", "-d"],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn(
            f"Would start the {constants.PACKAGE_RELEASE_WORKFLOW} workflow",
            result.output,
        )
        mock_github.return_value.commit_exists.assert_not_called()
        mock_github.return_value.dispatch_workflow.assert_not_called()

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_package_unknown_commit(self, mock_github):
        # e.g. a local commit that was never pushed
        mock_github.return_value.commit_exists.return_value = False
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-n", "aswf/ci-package-boost:2027", "--sha", "abc123"],
            input="y\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Commit abc123 not found", result.output)
        mock_github.return_value.dispatch_workflow.assert_not_called()

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_image_refuses_package_options(self, mock_github):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-n", "aswf/ci-base:2027.4", "--sha", "abc", "--force-rebuild"],
            input="y\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("only apply to Conan package releases", result.output)
        mock_github.return_value.create_release.assert_not_called()
