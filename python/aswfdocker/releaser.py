# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
import logging
import typing

from github import Github, GithubException, Auth
from github.GitRelease import GitRelease

from aswfdocker import settings, constants, aswfinfo, groupinfo

logger = logging.getLogger(__name__)


class ImageRelease(typing.NamedTuple):
    """The GitHub release recording a released image."""

    name: str  # e.g. aswf/ci-base:2027.4
    message: str
    prerelease: bool

    @property
    def tag(self) -> str:
        """The release's git tag, e.g. aswf/ci-base/2027.4 (tags can't hold
        colons)."""
        return self.name.replace(":", "/")


def image_releases(
    build_info: aswfinfo.ASWFInfo, group_info: groupinfo.GroupInfo, message: str
) -> typing.List[ImageRelease]:
    """The GitHub releases recording the images of `group_info`."""
    prerelease = build_info.docker_org == constants.TESTING_DOCKER_ORG
    start_message = f"{message}\n\n" if message else ""
    return [
        ImageRelease(
            name=f"{build_info.docker_org}/{image}:{version}",
            message=f"{start_message}Inspect released Docker image here: "
            f"https://hub.docker.com/r/{build_info.docker_org}/{image}/tags?name={version}",
            prerelease=prerelease,
        )
        for image, version in group_info.iter_images_versions()
    ]


class GitHub:
    """The aswf-docker repository on GitHub (by default the main ASWF one, or
    the one of another `github_org`, or `repository`), accessed with
    `token`, by default the one saved by `aswfdocker settings`."""

    def __init__(self, github_org: str = "", token: str = "", repository: str = ""):
        if not token:
            token = settings.Settings().github_access_token
        if token:
            self.github = Github(auth=Auth.Token(token), per_page=100)
        else:
            self.github = Github(per_page=100)
        if not repository:
            if not github_org:
                github_org = constants.MAIN_GITHUB_ASWF_ORG
            repository = f"{github_org}/{constants.MAIN_GITHUB_REPO_NAME}"
        self.repo = self.github.get_repo(repository)

    def draft_releases(self) -> typing.Dict[str, GitRelease]:
        """The repository's draft releases, by tag. Only the release list
        has them: they can't be looked up by tag."""
        return {r.tag_name: r for r in self.repo.get_releases() if r.draft}

    def create_draft_release(
        self,
        sha: str,
        release: ImageRelease,
        drafts: typing.Dict[str, GitRelease],
    ) -> None:
        """Creates `release` as a draft on `sha`, replacing any earlier draft
        of it. Its tag only gets created when it's published."""
        old = drafts.get(release.tag)
        if old:
            old.delete_release()
            logger.warning("Draft release %s already existed, deleted!", release.name)
        self.repo.create_git_release(
            tag=release.tag,
            name=release.name,
            message=release.message,
            draft=True,
            prerelease=release.prerelease,
            target_commitish=sha,
        )
        logger.info("Draft release %s created.", release.name)

    def publish_release(
        self,
        sha: str,
        release: ImageRelease,
        drafts: typing.Dict[str, GitRelease],
    ) -> None:
        """Publishes the draft of `release` on `sha`, which creates its tag,
        replacing any earlier release (and tag) of it. Without a draft (e.g.
        when the release workflow was started from the GitHub Actions tab),
        the release is created directly."""
        draft = drafts.get(release.tag)
        if draft and draft.target_commitish != sha:
            draft = None
        try:
            old = self.repo.get_release(release.tag)
        except GithubException:
            old = None
        if old and not draft and old.target_commitish == sha:
            # e.g. the job publishing the releases was re-run
            logger.info("Release %s already published.", release.name)
            return
        if old:
            old.delete_release()
            logger.warning("Release %s already existed, deleted!", release.name)
        try:
            ref = self.repo.get_git_ref(ref="tags/" + release.tag)
        except GithubException:
            ref = None
        if ref:
            ref.delete()
            logger.warning("Tag %s already existed, deleted!", release.tag)
        if draft:
            draft.update_release(
                name=draft.title,
                message=draft.body,
                draft=False,
                prerelease=draft.prerelease,
                tag_name=release.tag,
                target_commitish=sha,
            )
            logger.info("Release %s published.", release.name)
        else:
            logger.warning("No draft release of %s on %s", release.name, sha)
            self.repo.create_git_release(
                tag=release.tag,
                name=release.name,
                message=release.message,
                draft=False,
                prerelease=release.prerelease,
                target_commitish=sha,
            )
            logger.info("Release %s created.", release.name)

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


class ReleasePublisher:
    """ReleasePublisher publishes the draft GitHub releases recording the
    images released by the release-images.yml GitHub Actions workflow, which
    runs it once the images are pushed, with its own GitHub token. The
    drafts were created by `aswfdocker release`, with the token of the
    person releasing, who the releases are attributed to."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        sha: str,
        message="",
        token="",
        repository="",
    ):
        self.sha = sha
        self.releases = image_releases(build_info, group_info, message)
        self.gh = GitHub(token=token, repository=repository)

    def publish(self):
        drafts = self.gh.draft_releases()
        for release in self.releases:
            self.gh.publish_release(self.sha, release, drafts)


class WorkflowReleaser:
    """WorkflowReleaser has a release GitHub Actions workflow build and
    publish from a given commit, as only GitHub Actions holds the Artifactory
    and Docker Hub credentials (and, in future, the signing keys)."""

    workflow = ""

    def __init__(self, sha: str, github_org="", workflow_ref=""):
        self.sha = sha
        self.gh = GitHub(github_org)
        # The workflow definition comes from this branch (by default the
        # repository's default branch), but always builds `sha`.
        self.workflow_ref = workflow_ref or self.gh.repo.default_branch
        # The image releases to create as drafts (see gather()).
        self.releases: typing.List[ImageRelease] = []

    def inputs(self) -> typing.Dict[str, str]:
        """The workflow_dispatch inputs (all strings, as the GitHub API
        requires)."""
        raise NotImplementedError

    def release(self, dry_run=True):
        inputs = self.inputs()
        if dry_run:
            for release in self.releases:
                logger.info("Would create draft GitHub release %s", release.name)
            logger.info(
                "Would start the %s workflow on %s with inputs: %s",
                self.workflow,
                self.workflow_ref,
                inputs,
            )
            return
        if not self.gh.commit_exists(self.sha):
            raise RuntimeError(
                f"Commit {self.sha} not found in {self.gh.repo.full_name}: "
                "push it to a branch there before releasing from it"
            )
        self.create_draft_releases()
        self.gh.dispatch_workflow(self.workflow, self.workflow_ref, inputs)

    def create_draft_releases(self):
        """Creates the draft GitHub releases of the images to release, which
        the workflow publishes once they're pushed: being created with the
        token of the person releasing, they're attributed to them."""
        if not self.releases:
            return
        drafts = self.gh.draft_releases()
        for release in self.releases:
            self.gh.create_draft_release(self.sha, release, drafts)


class PackageReleaser(WorkflowReleaser):
    """PackageReleaser has the release-packages.yml GitHub Actions workflow
    build Conan packages from a given commit and upload them to Artifactory.
    Unlike image releases, no GitHub release or tag is created."""

    workflow = constants.PACKAGE_RELEASE_WORKFLOW

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        sha: str,
        github_org="",
        workflow_ref="",
        force_rebuild=False,
    ):
        super().__init__(sha, github_org, workflow_ref)
        self.build_info = build_info
        self.group_info = group_info
        self.force_rebuild = force_rebuild
        self.release_list: typing.List[str] = []

    def gather(self):
        for image, version in self.group_info.iter_images_versions():
            self.release_list.append(f"{self.build_info.package_org}/{image}:{version}")

    def inputs(self) -> typing.Dict[str, str]:
        return {
            "sha": self.sha,
            "package_org": self.build_info.package_org,
            "groups": " ".join(self.group_info.names),
            "versions": " ".join(self.group_info.versions),
            "targets": " ".join(self.group_info.targets),
            "force_rebuild": "true" if self.force_rebuild else "false",
        }


class ImageReleaser(WorkflowReleaser):
    """ImageReleaser has the release-images.yml GitHub Actions workflow build
    Docker images from a given commit, push them to Docker Hub and publish
    the draft GitHub release recording each of them (see ReleasePublisher)."""

    workflow = constants.IMAGE_RELEASE_WORKFLOW

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        group_info: groupinfo.GroupInfo,
        sha: str,
        github_org="",
        workflow_ref="",
        message="",
    ):
        super().__init__(sha, github_org, workflow_ref)
        self.build_info = build_info
        self.group_info = group_info
        self.message = message

    def gather(self):
        self.releases = image_releases(self.build_info, self.group_info, self.message)

    def inputs(self) -> typing.Dict[str, str]:
        return {
            "sha": self.sha,
            "docker_org": self.build_info.docker_org,
            "groups": " ".join(self.group_info.names),
            "versions": " ".join(self.group_info.versions),
            "targets": " ".join(self.group_info.targets),
            "message": self.message or "",
        }


class FullReleaser(WorkflowReleaser):
    """FullReleaser has the release-all.yml GitHub Actions workflow release
    every Conan package and image of the given years from a given commit, in
    dependency order, publishing the draft GitHub release recording each
    image (see ReleasePublisher)."""

    workflow = constants.FULL_RELEASE_WORKFLOW

    def __init__(  # pylint: disable=too-many-arguments
        self,
        build_info: aswfinfo.ASWFInfo,
        versions: typing.Sequence[str],
        sha: str,
        github_org="",
        workflow_ref="",
        force_rebuild=False,
        message="",
    ):
        super().__init__(sha, github_org, workflow_ref)
        self.build_info = build_info
        self.versions = list(versions)
        self.force_rebuild = force_rebuild
        self.message = message

    def gather(self):
        self.releases = image_releases(
            self.build_info,
            groupinfo.GroupInfo(
                type_=constants.ImageType.IMAGE,
                names=list(constants.FULL_RELEASE_IMAGE_GROUPS),
                versions=self.versions,
                targets=[],
            ),
            self.message,
        )

    def inputs(self) -> typing.Dict[str, str]:
        return {
            "sha": self.sha,
            "org": self.build_info.docker_org,
            "versions": " ".join(self.versions),
            "force_rebuild": "true" if self.force_rebuild else "false",
            "message": self.message or "",
        }
