# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import logging
import typing
from datetime import datetime, timezone

from github import Github, InputGitAuthor, GithubException, Auth

from aswfdocker import settings, constants, aswfinfo, groupinfo

logger = logging.getLogger(__name__)


class GitHub:
    def __init__(self, github_org: str):
        s = settings.Settings()
        if s.github_access_token:
            auth = Auth.Token(s.github_access_token)
            self.github = Github(auth=auth)
        else:
            self.github = Github()
        if not github_org:
            github_org = constants.MAIN_GITHUB_ASWF_ORG
        self.repo = self.github.get_repo(
            f"{github_org}/{constants.MAIN_GITHUB_REPO_NAME}"
        )

    def create_release(self, sha, tag, release_message, prerelease):
        logger.debug("GitHub.create_release(tag=%s)", tag)
        real_tag = tag.replace(":", "/")
        try:
            release = self.repo.get_release(real_tag)
        except GithubException:
            release = None
        if release:
            release.delete_release()
            logger.warning("Release %s already existed, deleted!", real_tag)
        try:
            ref = self.repo.get_git_ref(ref="tags/" + real_tag)
        except GithubException:
            ref = None
        if ref:
            ref.delete()
            logger.warning("Tag %s already existed, deleted!", real_tag)
        release = self.repo.create_git_tag_and_release(
            tag=real_tag,
            tag_message=tag,
            release_name=tag,
            release_message=release_message,
            object=sha,
            type="commit",
            draft=False,
            prerelease=prerelease,
            tagger=InputGitAuthor(
                self.github.get_user().name,
                self.github.get_user().email,
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            ),
        )
        logger.info("Release %s created.", real_tag)

    def commit_exists(self, sha: str) -> bool:
        try:
            self.repo.get_commit(sha)
        except GithubException:
            return False
        return True

    def dispatch_workflow(
        self, workflow: str, ref: str, inputs: typing.Dict[str, str]
    ) -> None:
        logger.debug("GitHub.dispatch_workflow(%s, ref=%s, %s)", workflow, ref, inputs)
        if not self.repo.get_workflow(workflow).create_dispatch(ref=ref, inputs=inputs):
            raise RuntimeError(f"Failed to start the {workflow} workflow")
        logger.info(
            "Started the %s workflow: %s/actions/workflows/%s",
            workflow,
            self.repo.html_url,
            workflow,
        )


class Releaser:
    """Releaser creates GitHub releases for each Docker image."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        sha: str,
        github_org="",
        message="",
    ):
        self.github_org = github_org
        self.build_info = build_info
        self.group_info = group_info
        self.sha = sha
        self.message = message
        self.gh = GitHub(github_org)
        self.release_list: typing.List[typing.Tuple[str]] = []

    def gather(self):
        for image, version in self.group_info.iter_images_versions():
            tag = f"{self.build_info.docker_org}/{image}:{version}"
            self.release_list.append((image, version, tag))

    def release(self, dry_run=True):
        logger.debug("Releaser.release(dry_run=%s)", dry_run)
        prerelease = self.build_info.docker_org == constants.TESTING_DOCKER_ORG
        if self.message:
            start_message = f"{self.message}\n\n"
        else:
            start_message = self.message
        for image, version, tag in self.release_list:
            message = (
                f"{start_message}Inspect released Docker image here: "
                f"https://hub.docker.com/r/{self.build_info.docker_org}/{image}/tags?name={version}"
            )
            if dry_run:
                logger.info(
                    "Would create this GitHub release on current commit: %s", tag
                )
            else:
                self.gh.create_release(
                    self.sha, tag, release_message=message, prerelease=prerelease
                )


class PackageReleaser:
    """PackageReleaser has the release-packages.yml GitHub Actions workflow
    build Conan packages from a given commit and upload them to Artifactory,
    which only GitHub Actions has the credentials for. Unlike image releases,
    no GitHub release or tag is created."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        sha: str,
        github_org="",
        workflow_ref="",
        force_rebuild=False,
    ):
        self.build_info = build_info
        self.group_info = group_info
        self.sha = sha
        self.force_rebuild = force_rebuild
        self.gh = GitHub(github_org)
        # The workflow definition comes from this branch (by default the
        # repository's default branch), but always builds `sha`.
        self.workflow_ref = workflow_ref or self.gh.repo.default_branch
        self.release_list: typing.List[str] = []

    def gather(self):
        for image, version in self.group_info.iter_images_versions():
            self.release_list.append(f"{self.build_info.package_org}/{image}:{version}")

    def inputs(self) -> typing.Dict[str, str]:
        """The release-packages.yml workflow_dispatch inputs (all strings,
        as the GitHub API requires)."""
        return {
            "sha": self.sha,
            "package_org": self.build_info.package_org,
            "groups": " ".join(self.group_info.names),
            "versions": " ".join(self.group_info.versions),
            "targets": " ".join(self.group_info.targets),
            "force_rebuild": "true" if self.force_rebuild else "false",
        }

    def release(self, dry_run=True):
        inputs = self.inputs()
        if dry_run:
            logger.info(
                "Would start the %s workflow on %s with inputs: %s",
                constants.PACKAGE_RELEASE_WORKFLOW,
                self.workflow_ref,
                inputs,
            )
            return
        if not self.gh.commit_exists(self.sha):
            raise RuntimeError(
                f"Commit {self.sha} not found in {self.gh.repo.full_name}: "
                "push it to a branch there before releasing packages built from it"
            )
        self.gh.dispatch_workflow(
            constants.PACKAGE_RELEASE_WORKFLOW, self.workflow_ref, inputs
        )
