# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
aswfdocker release commands: starting the release workflows, and the
GitHub Actions-only steps they run
"""

import os
import sys
import logging
import warnings

import click

from aswfdocker import constants, index, releaser, utils
from aswfdocker.cli.options import (
    common_image_options,
    get_group_info,
    pass_build_info,
)

# Avoid pylint giving us grief for complex command lines
# pylint: disable=too-many-arguments
# pylint: disable=too-many-locals
# pylint: disable=too-many-branches

logger = logging.getLogger("build-images")


@click.command()
@common_image_options
@click.option(
    "--sha",
    "-s",
    help="The sha to create the release tag on, defaults to current sha.",
)
@click.option(
    "--github-org",
    "-o",
    default=constants.MAIN_GITHUB_ASWF_ORG,
    help="The GitHub organization/username to create the release on,"
    f" defaults to {constants.MAIN_GITHUB_ASWF_ORG}.",
)
@click.option(
    "--docker-org",
    "-do",
    default=constants.TESTING_DOCKER_ORG,
    help="The Docker organization/username to upload the Docker image to,"
    f" defaults to {constants.TESTING_DOCKER_ORG}.",
)
@click.option(
    "--message",
    "-m",
    help="The release message of each image's GitHub release (images only).",
)
@click.option(
    "--force-rebuild",
    is_flag=True,
    help="Conan packages only: rebuild and upload the selected packages even if "
    "Artifactory already has an up-to-date binary.",
)
@click.option(
    "--workflow-ref",
    default="",
    help="The branch to run the release workflow definition from, defaults to the "
    "repository's default branch. Releases are always built from --sha.",
)
@click.option(
    "--full-release",
    is_flag=True,
    help="Release every Conan package and image of the given years (-v), in "
    "dependency order.",
)
@click.option("--dry-run", "-d", is_flag=True, help="Just logs what would happen.")
@pass_build_info
def release(  # pylint: disable=too-many-arguments,too-many-locals
    build_info,
    ci_image_type,
    group,
    version,
    full_name,
    target,
    sha,
    github_org,
    docker_org,
    message,
    force_rebuild,
    workflow_ref,
    full_release,
    dry_run,
):
    """Releases ci-image Docker images and/or Conan packages.

    Starts a GitHub Actions workflow building them from the commit, the only
    place holding the Docker Hub and Artifactory credentials: images are
    pushed to Docker Hub and each recorded as a GitHub release (and tag) on
    the commit, Conan packages are uploaded to Artifactory. With
    --full-release, every package and image of the given years is released,
    in dependency order.
    """

    # Disable SSL unclosed ResourceWarning coming from GitHub
    warnings.filterwarnings(
        action="ignore", message="unclosed", category=ResourceWarning
    )

    if not sha:
        if utils.get_current_branch() != "main":
            click.secho(
                "Cannot release from non-main branch! Specify --sha to create a release on a given commit.",
                fg="red",
            )
            sys.exit(1)
        sha = utils.get_current_sha()
    if docker_org:
        build_info.set_org(docker_org)

    workflow_releaser: releaser.WorkflowReleaser
    if full_release:
        if ci_image_type or group or full_name or target:
            raise click.BadOptionUsage(
                "--full-release",
                "--full-release releases every group: it can't be combined with "
                "-t, -g, -n or --target",
            )
        years = sorted(vi.version for vi in index.Index().iter_years())
        if list(version) == [constants.ALL]:
            version = years
        if not version or set(version) - set(years):
            raise click.BadOptionUsage(
                "--version",
                f"--full-release needs years to release (-v), from: {', '.join(years)}",
            )
        full_releaser = releaser.FullReleaser(
            build_info=build_info,
            versions=sorted(version),
            sha=sha,
            github_org=github_org,
            workflow_ref=workflow_ref,
            force_rebuild=force_rebuild,
            message=message,
        )
        full_releaser.gather()
        workflow_releaser = full_releaser
        prompt = (
            f"Are you sure you want GitHub Actions to release every Conan package "
            f"({'forcing a rebuild' if force_rebuild else 'unless up to date'}) of "
            f"{' '.join(sorted(version))} to {build_info.docker_org} from sha={sha}, "
            f"and these {len(full_releaser.releases)} images (as draft GitHub "
            "releases until they're pushed)?\n"
            + "\n".join(r.name for r in full_releaser.releases)
            + "\n"
        )
    else:
        group_info = get_group_info(
            build_info, ci_image_type, group, version, full_name, target
        )
        if group_info.type == constants.ImageType.PACKAGE:
            package_releaser = releaser.PackageReleaser(
                build_info=build_info,
                group_info=group_info,
                sha=sha,
                github_org=github_org,
                workflow_ref=workflow_ref,
                force_rebuild=force_rebuild,
            )
            package_releaser.gather()
            workflow_releaser = package_releaser
            prompt = (
                "Are you sure you want GitHub Actions to build and upload these {} "
                "Conan packages from sha={} ({})?\n{}\n".format(
                    len(package_releaser.release_list),
                    sha,
                    "forcing a rebuild" if force_rebuild else "unless up to date",
                    "\n".join(package_releaser.release_list),
                )
            )
        else:
            if force_rebuild:
                raise click.BadOptionUsage(
                    "--force-rebuild",
                    "--force-rebuild only applies to Conan package releases",
                )
            image_releaser = releaser.ImageReleaser(
                build_info=build_info,
                group_info=group_info,
                sha=sha,
                github_org=github_org,
                workflow_ref=workflow_ref,
                message=message,
            )
            image_releaser.gather()
            workflow_releaser = image_releaser
            prompt = (
                "Are you sure you want GitHub Actions to build and push these {} "
                "images from sha={} (as draft GitHub releases until they're "
                "pushed)?\n{}\n".format(
                    len(image_releaser.releases),
                    sha,
                    "\n".join(r.name for r in image_releaser.releases),
                )
            )
    if build_info.docker_org not in (
        constants.PUBLISH_DOCKER_ORG,
        constants.TESTING_DOCKER_ORG,
    ):
        raise click.BadOptionUsage(
            "--docker-org",
            f"Releases go to {constants.PUBLISH_DOCKER_ORG} or "
            f"{constants.TESTING_DOCKER_ORG}, not {build_info.docker_org}",
        )
    if not click.confirm(prompt):
        click.echo("Release cancelled.")
        return
    try:
        workflow_releaser.release(dry_run=dry_run)
    except RuntimeError as e:
        raise click.ClickException(e.args[0])
    click.echo("Dry run: no release started." if dry_run else "Release started.")


@click.command()
@common_image_options
@click.option(
    "--sha",
    "-s",
    required=True,
    help="The sha the releases are on.",
)
@click.option(
    "--docker-org",
    "-do",
    required=True,
    help="The Docker organization the images were pushed to.",
)
@click.option(
    "--message",
    "-m",
    default="",
    help="The release message, for releases without a draft.",
)
@click.option(
    "--sbom-dir",
    default="",
    help="The directory holding the image SBOMs (from exportsboms) to attach "
    "to the releases.",
)
@pass_build_info
def publishreleases(
    build_info,
    ci_image_type,
    group,
    version,
    full_name,
    target,
    sha,
    docker_org,
    message,
    sbom_dir,
):
    """Publishes the draft GitHub release (creating its tag) of each released
    image (GitHub Actions only), with the workflow's GITHUB_TOKEN, with the
    image's SBOM attached."""
    if not utils.on_github_actions():
        raise click.UsageError(
            "GitHub releases of images can only be published from GitHub Actions."
        )
    build_info.set_org(docker_org)
    group_info = get_group_info(
        build_info, ci_image_type, group, version, full_name, target
    )
    if group_info.type != constants.ImageType.IMAGE:
        raise click.BadOptionUsage("--ci-image-type", "Only images get GitHub releases")
    publisher = releaser.ReleasePublisher(
        build_info=build_info,
        group_info=group_info,
        sha=sha,
        message=message,
        token=os.environ["GITHUB_TOKEN"],
        repository=os.environ["GITHUB_REPOSITORY"],
        sbom_dir=sbom_dir,
    )
    try:
        publisher.publish()
    except FileNotFoundError as e:
        raise click.ClickException(e.args[0])


@click.command()
@common_image_options
@click.option(
    "--output-dir",
    "-o",
    required=True,
    help="The directory to export the SBOMs to.",
)
@click.option(
    "--docker-org",
    "-do",
    default="",
    help="The Docker organization of the images, instead of the one derived "
    "from the repository URI and branch.",
)
@pass_build_info
def exportsboms(
    build_info,
    ci_image_type,
    group,
    version,
    full_name,
    target,
    output_dir,
    docker_org,
):
    """Exports the SBOM of each image, as built in the local Docker image
    store, for publishreleases to attach to its GitHub release."""
    if docker_org:
        build_info.set_org(docker_org)
    group_info = get_group_info(
        build_info, ci_image_type, group, version, full_name, target
    )
    if group_info.type != constants.ImageType.IMAGE:
        raise click.BadOptionUsage("--ci-image-type", "Only images have SBOMs")
    releaser.export_sboms(
        releaser.image_releases(build_info, group_info, message=""), output_dir
    )
