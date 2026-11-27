# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0


import os
import subprocess
import tempfile
import unittest
from unittest import mock
import logging

import yaml
from click.testing import CliRunner

from aswfdocker import releaser, aswfinfo, index, constants, groupinfo, utils
from aswfdocker.cli import aswfdocker


def _release(draft, tag_name, target_commitish="abc123"):
    r = mock.MagicMock()
    r.draft = draft
    r.tag_name = tag_name
    r.target_commitish = target_commitish
    r.title = tag_name.replace("/2027", ":2027")
    r.body = "From the draft"
    r.prerelease = False
    return r


class TestGitHubReleases(unittest.TestCase):
    """GitHub's draft release handling, against a mocked repository."""

    RELEASE = releaser.ImageRelease(
        name="aswf/ci-base:2027.4", message="Hello", prerelease=False
    )

    def setUp(self):
        patcher = mock.patch.object(releaser, "Github")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.gh = releaser.GitHub(token="token", repository="me/aswf-docker")
        self.repo = self.gh.repo

    def test_tag_has_no_colon(self):
        self.assertEqual(self.RELEASE.tag, "aswf/ci-base/2027.4")

    def test_draft_releases_by_tag(self):
        draft = _release(True, "aswf/ci-base/2027.4")
        self.repo.get_releases.return_value = [
            draft,
            _release(False, "aswf/ci-base/2027.3"),
        ]
        self.assertEqual(self.gh.draft_releases(), {"aswf/ci-base/2027.4": draft})

    def test_create_draft_release_replaces_earlier_draft(self):
        old = _release(True, "aswf/ci-base/2027.4", "def456")
        self.gh.create_draft_release(
            "abc123", self.RELEASE, {"aswf/ci-base/2027.4": old}
        )
        old.delete_release.assert_called_once_with()
        self.repo.create_git_release.assert_called_once_with(
            tag="aswf/ci-base/2027.4",
            name="aswf/ci-base:2027.4",
            message="Hello",
            draft=True,
            prerelease=False,
            target_commitish="abc123",
        )
        # A published release of the image is left alone until its new
        # release is published.
        self.repo.get_release.assert_not_called()
        self.repo.get_git_ref.assert_not_called()

    def test_publish_release_publishes_draft_replacing_earlier_release(self):
        draft = _release(True, "aswf/ci-base/2027.4")
        old = _release(False, "aswf/ci-base/2027.4", "def456")
        self.repo.get_release.return_value = old
        self.gh.publish_release("abc123", self.RELEASE, {draft.tag_name: draft})
        old.delete_release.assert_called_once_with()
        self.repo.get_git_ref.return_value.delete.assert_called_once_with()
        draft.update_release.assert_called_once_with(
            name="aswf/ci-base:2027.4",
            message="From the draft",
            draft=False,
            prerelease=False,
            tag_name="aswf/ci-base/2027.4",
            target_commitish="abc123",
        )
        self.repo.create_git_release.assert_not_called()

    def test_publish_release_keeps_already_published_release(self):
        # e.g. the job publishing the releases was re-run: the release
        # attributed to the person releasing stays.
        published = _release(False, "aswf/ci-base/2027.4")
        self.repo.get_release.return_value = published
        self.gh.publish_release("abc123", self.RELEASE, {})
        published.delete_release.assert_not_called()
        self.repo.get_git_ref.assert_not_called()
        self.repo.create_git_release.assert_not_called()

    def test_publish_release_without_draft_creates_it(self):
        # e.g. the workflow was started from the GitHub Actions tab, or the
        # draft is on another commit: a draft is created, to attach the
        # SBOM to before publishing it.
        self.repo.get_release.side_effect = releaser.GithubException(404)
        self.repo.get_git_ref.side_effect = releaser.GithubException(404)
        for drafts in ({}, {"aswf/ci-base/2027.4": _release(True, "x", "def456")}):
            self.repo.create_git_release.reset_mock()
            self.gh.publish_release("abc123", self.RELEASE, drafts, "/sboms/x.json")
            self.repo.create_git_release.assert_called_once_with(
                tag="aswf/ci-base/2027.4",
                name="aswf/ci-base:2027.4",
                message="Hello",
                draft=True,
                prerelease=False,
                target_commitish="abc123",
            )
            draft = self.repo.create_git_release.return_value
            draft.upload_asset.assert_called_once_with(
                "/sboms/x.json",
                name="ci-base-2027.4.cdx.json",
                content_type="application/vnd.cyclonedx+json",
            )
            self.assertEqual(draft.update_release.call_args.kwargs["draft"], False)
            draft.reset_mock()

    def test_publish_release_attaches_sbom_before_replacing_release(self):
        draft = _release(True, "aswf/ci-base/2027.4")
        old = _release(False, "aswf/ci-base/2027.4", "def456")
        self.repo.get_release.return_value = old
        order = mock.MagicMock()
        order.attach_mock(draft.upload_asset, "attach_asset")
        order.attach_mock(old.delete_release, "delete")
        order.attach_mock(draft.update_release, "publish")
        draft.get_assets.return_value = []
        self.gh.publish_release(
            "abc123", self.RELEASE, {draft.tag_name: draft}, "/sboms/x.json"
        )
        self.assertEqual(
            [c[0] for c in order.mock_calls], ["attach_asset", "delete", "publish"]
        )

    def test_publish_release_replaces_sbom_of_draft(self):
        # e.g. the job publishing the releases was re-run after attaching
        # the SBOM, but before publishing.
        draft = _release(True, "aswf/ci-base/2027.4")
        stale, other = mock.MagicMock(), mock.MagicMock()
        stale.name = "ci-base-2027.4.cdx.json"
        other.name = "notes.txt"
        draft.get_assets.return_value = [stale, other]
        self.repo.get_release.side_effect = releaser.GithubException(404)
        self.repo.get_git_ref.side_effect = releaser.GithubException(404)
        self.gh.publish_release(
            "abc123", self.RELEASE, {draft.tag_name: draft}, "/sboms/x.json"
        )
        stale.delete_asset.assert_called_once_with()
        other.delete_asset.assert_not_called()
        draft.upload_asset.assert_called_once()
        self.repo.create_git_release.assert_not_called()

    def test_publish_release_without_sbom(self):
        draft = _release(True, "aswf/ci-base/2027.4")
        self.repo.get_release.side_effect = releaser.GithubException(404)
        self.repo.get_git_ref.side_effect = releaser.GithubException(404)
        self.gh.publish_release("abc123", self.RELEASE, {draft.tag_name: draft})
        draft.upload_asset.assert_not_called()
        draft.update_release.assert_called_once()


class TestImageReleaseSbom(unittest.TestCase):
    RELEASE = releaser.ImageRelease(
        name="aswftesting/ci-common:2027-clang22.4", message="", prerelease=True
    )

    def test_names(self):
        self.assertEqual(
            self.RELEASE.image, "docker.io/aswftesting/ci-common:2027-clang22.4"
        )
        self.assertEqual(self.RELEASE.sbom_file, "ci-common-2027-clang22.4.cdx.json")

    @mock.patch.object(releaser.subprocess, "run")
    def test_export_sboms(self, mock_run):
        mock_run.return_value.stdout = "cid123\n"
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sboms")
            releaser.export_sboms([self.RELEASE], out)
            self.assertTrue(os.path.isdir(out))
            self.assertEqual(
                [c.args[0] for c in mock_run.call_args_list],
                [
                    [
                        "docker",
                        "create",
                        "--pull",
                        "never",
                        "docker.io/aswftesting/ci-common:2027-clang22.4",
                        "true",
                    ],
                    [
                        "docker",
                        "cp",
                        "cid123:/usr/local/aswf/sbom.cdx.json",
                        os.path.join(out, "ci-common-2027-clang22.4.cdx.json"),
                    ],
                    ["docker", "rm", "cid123"],
                ],
            )

    @mock.patch.object(releaser.subprocess, "run")
    def test_export_sboms_removes_container_on_failure(self, mock_run):
        created = mock.MagicMock(stdout="cid123\n")
        mock_run.side_effect = [
            created,
            subprocess.CalledProcessError(1, "docker cp"),
            mock.MagicMock(),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(subprocess.CalledProcessError):
                releaser.export_sboms([self.RELEASE], tmp)
        self.assertEqual(
            mock_run.call_args_list[-1].args[0], ["docker", "rm", "cid123"]
        )


class TestReleaser(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None
        self.build_info = aswfinfo.ASWFInfo(
            repo_uri="notauri", source_branch="testing", aswf_version="2027.123"
        )

    @mock.patch.object(releaser, "GitHub")
    def test_image_base_2027_dict(self, mock_github):
        # Run by the release-images.yml workflow, with its own token, once
        # the images are pushed.
        publisher = releaser.ReleasePublisher(
            self.build_info,
            groupinfo.GroupInfo(
                names=["base"],
                versions=["2027"],
                type_=constants.ImageType.IMAGE,
                targets=["base"],
            ),
            sha="abc123",
            token="gha-token",
            repository="me/aswf-docker",
        )
        mock_github.assert_called_once_with(
            token="gha-token", repository="me/aswf-docker"
        )
        publisher.publish()
        base_version = next(
            v
            for v in index.Index().iter_versions(constants.ImageType.IMAGE, "base")
            if v.startswith("2027.")
        )
        gh = mock_github.return_value
        gh.publish_release.assert_called_once_with(
            "abc123",
            releaser.ImageRelease(
                name=f"aswflocaltesting/ci-base:{base_version}",
                message="Inspect released Docker image here: "
                f"https://hub.docker.com/r/aswflocaltesting/ci-base/tags?name={base_version}",
                prerelease=False,
            ),
            gh.draft_releases.return_value,
            "",
        )

    def test_full_release_image_groups_match_workflow(self):
        # release-all.yml releases the images FullReleaser creates drafts of
        path = os.path.join(
            utils.get_git_top_level(), ".github", "workflows", "release-all.yml"
        )
        with open(path, encoding="utf-8") as f:
            jobs = yaml.safe_load(f)["jobs"]
        groups = [
            job["with"]["groups"]
            for job in jobs.values()
            if job["uses"].endswith("/release-images.yml")
        ]
        self.assertEqual(sorted(groups), sorted(constants.FULL_RELEASE_IMAGE_GROUPS))


class TestReleaserCli(unittest.TestCase):
    def setUp(self):
        self._log_handlers = logging.getLogger("").handlers
        logging.getLogger("").handlers = []
        self.maxDiff = None

    def tearDown(self):
        logging.getLogger("").handlers = self._log_handlers

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli(self, mock_github):
        # An image release starts the image release workflow, which builds,
        # pushes and creates the GitHub releases from GitHub Actions.
        mock_github.return_value.repo.default_branch = "main"
        current_version = list(
            index.Index().iter_versions(constants.ImageType.IMAGE, "base")
        )[1]
        sha = utils.get_current_sha()
        result = CliRunner().invoke(
            aswfdocker.cli,
            [
                "release",
                "-n",
                f"aswf/ci-base:{current_version}",
                "--sha",
                sha,
                "-m",
                "Hello",
            ],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn(f"aswf/ci-base:{current_version}", result.output)
        gh = mock_github.return_value
        # The draft release, attributed to the person releasing, is created
        # before the workflow is started.
        self.assertEqual(
            [c[0] for c in gh.mock_calls if c[0] != "repo.default_branch"][-3:],
            ["draft_releases", "create_draft_release", "dispatch_workflow"],
        )
        draft = gh.create_draft_release.call_args.args[1]
        self.assertEqual(draft.name, f"aswf/ci-base:{current_version}")
        self.assertTrue(draft.message.startswith("Hello\n\n"))
        self.assertFalse(draft.prerelease)
        gh.dispatch_workflow.assert_called_once_with(
            constants.IMAGE_RELEASE_WORKFLOW,
            "main",
            {
                "sha": sha,
                "docker_org": "aswf",
                "groups": "base",
                "versions": current_version.rsplit(".", 1)[0],
                "targets": "base",
                "message": "Hello",
            },
        )

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_image_group_dry_run(self, mock_github):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-g", "vfx", "-v", "2027", "--sha", "abc123", "-d"],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        # Per-clang images are listed for every clang version of the year
        self.assertIn("aswftesting/ci-osl:2027-clang22", result.output)
        self.assertIn(
            f"Would start the {constants.IMAGE_RELEASE_WORKFLOW} workflow",
            result.output,
        )
        self.assertIn(
            "Would create draft GitHub release aswftesting/ci-osl:2027-clang22",
            result.output,
        )
        mock_github.return_value.create_draft_release.assert_not_called()
        mock_github.return_value.dispatch_workflow.assert_not_called()

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_refuses_other_orgs(self, mock_github):
        # The release workflows only release to aswf or aswftesting
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "-n", "aswflocaltesting/ci-base:2027.4", "--sha", "abc"],
            input="y\n",
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not aswflocaltesting", result.output)
        mock_github.return_value.dispatch_workflow.assert_not_called()

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_full_release(self, mock_github):
        mock_github.return_value.repo.default_branch = "main"
        result = CliRunner().invoke(
            aswfdocker.cli,
            [
                "release",
                "--full-release",
                "-v",
                "2027",
                "-v",
                "2026",
                "--docker-org",
                "aswf",
                "--sha",
                "abc123",
                "-m",
                "VFX Platform 2027",
            ],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mock_github.return_value.dispatch_workflow.assert_called_once_with(
            constants.FULL_RELEASE_WORKFLOW,
            "main",
            {
                "sha": "abc123",
                "org": "aswf",
                "versions": "2026 2027",
                "force_rebuild": "false",
                "message": "VFX Platform 2027",
            },
        )
        # Drafts of every image of every year
        drafts = [
            c.args[1].name
            for c in mock_github.return_value.create_draft_release.call_args_list
        ]
        for name in (
            "aswf/ci-baseos-gl-conan:2026",
            "aswf/ci-common:2027-clang22",
            "aswf/ci-base:2026",
            "aswf/ci-vfxall:2027-clang22",
        ):
            self.assertTrue(any(d.startswith(name + ".") for d in drafts), name)
        self.assertFalse(any("ci-openrv" in d for d in drafts))

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_full_release_all_years(self, mock_github):
        mock_github.return_value.repo.default_branch = "main"
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["release", "--full-release", "-v", "all", "--sha", "abc123"],
            input="y\n",
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        years = sorted(vi.version for vi in index.Index().iter_years())
        inputs = mock_github.return_value.dispatch_workflow.call_args.args[2]
        self.assertEqual(inputs["versions"], " ".join(years))
        self.assertEqual(inputs["org"], "aswftesting")

    @mock.patch.object(releaser, "GitHub")
    def test_release_cli_full_release_refuses_bad_options(self, mock_github):
        for args, error in (
            (["-g", "vfx", "-v", "2027"], "can't be combined"),
            ([], "needs years"),
            (["-v", "2027-clang22"], "needs years"),
        ):
            result = CliRunner().invoke(
                aswfdocker.cli,
                ["release", "--full-release", "--sha", "abc123", *args],
                input="y\n",
            )
            self.assertNotEqual(result.exit_code, 0, msg=args)
            self.assertIn(error, result.output)
        mock_github.return_value.dispatch_workflow.assert_not_called()

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
        gh.create_draft_release.assert_not_called()
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
        self.assertIn("only applies to Conan package releases", result.output)
        mock_github.return_value.dispatch_workflow.assert_not_called()


class TestPublishReleasesCli(unittest.TestCase):
    @mock.patch.object(releaser, "GitHub")
    def test_refused_outside_github_actions(self, mock_github):
        with mock.patch.dict(os.environ):
            os.environ.pop("GITHUB_ACTIONS", None)
            result = CliRunner().invoke(
                aswfdocker.cli,
                [
                    "publishreleases",
                    "-g",
                    "base",
                    "-v",
                    "2027",
                    "-s",
                    "abc",
                    "-do",
                    "aswf",
                ],
            )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("only be published from GitHub Actions", result.output)
        mock_github.assert_not_called()

    @mock.patch.object(releaser, "GitHub")
    def test_publishes_releases_with_workflow_token(self, mock_github):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "gha-token",
            "GITHUB_REPOSITORY": "AcademySoftwareFoundation/aswf-docker",
        }
        with mock.patch.dict(os.environ, env):
            result = CliRunner().invoke(
                aswfdocker.cli,
                [
                    "publishreleases",
                    "-g",
                    "base",
                    "-v",
                    "2027",
                    "-s",
                    "abc",
                    "-do",
                    "aswf",
                    "-m",
                    "Hello",
                ],
            )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        mock_github.assert_called_once_with(
            token="gha-token", repository="AcademySoftwareFoundation/aswf-docker"
        )
        calls = mock_github.return_value.publish_release.call_args_list
        self.assertEqual(
            sorted(c.args[1].name.split(":")[0] for c in calls),
            ["aswf/ci-base", "aswf/ci-baseqt"],
        )
        for c in calls:
            self.assertEqual(c.args[0], "abc")
            self.assertTrue(c.args[1].message.startswith("Hello\n\n"))
            self.assertFalse(c.args[1].prerelease)

    @mock.patch.object(releaser, "GitHub")
    def test_attaches_sboms(self, mock_github):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "gha-token",
            "GITHUB_REPOSITORY": "AcademySoftwareFoundation/aswf-docker",
        }
        args = ["publishreleases", "-g", "base", "-v", "2027", "-s", "abc"]
        args += ["-do", "aswf", "-tg", "base"]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            # Refused, before publishing anything, when an SBOM is missing.
            result = CliRunner().invoke(aswfdocker.cli, args + ["--sbom-dir", tmp])
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("Missing image SBOMs", result.output)
            mock_github.return_value.publish_release.assert_not_called()

            release = mock_github.return_value.publish_release
            releases = releaser.image_releases(
                aswfinfo.ASWFInfo(repo_uri="x", source_branch="y"),
                groupinfo.GroupInfo(
                    names=["base"],
                    versions=["2027"],
                    type_=constants.ImageType.IMAGE,
                    targets=["base"],
                ),
                "",
            )
            for r in releases:
                with open(os.path.join(tmp, r.sbom_file), "w", encoding="utf-8") as f:
                    f.write("{}")
            result = CliRunner().invoke(aswfdocker.cli, args + ["--sbom-dir", tmp])
            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertEqual(len(release.call_args_list), len(releases))
            for c in release.call_args_list:
                self.assertEqual(c.args[3], os.path.join(tmp, c.args[1].sbom_file))


class TestExportSbomsCli(unittest.TestCase):
    @mock.patch.object(releaser, "export_sboms")
    def test_exports_image_sboms(self, mock_export):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["exportsboms", "-g", "common", "-v", "2027", "-do", "aswf", "-o", "out"],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        releases, output_dir = mock_export.call_args.args
        self.assertEqual(output_dir, "out")
        names = sorted(r.name.split(":")[1] for r in releases)
        self.assertTrue(names)
        self.assertTrue(
            all(r.name.startswith("aswf/ci-common:2027-clang") for r in releases)
        )

    @mock.patch.object(releaser, "export_sboms")
    def test_refuses_packages(self, mock_export):
        result = CliRunner().invoke(
            aswfdocker.cli,
            ["exportsboms", "-t", "PACKAGE", "-g", "common", "-v", "2027", "-o", "out"],
        )
        self.assertNotEqual(result.exit_code, 0)
        mock_export.assert_not_called()
