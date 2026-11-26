# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Main aswfdocker command line implementation using click
"""

import os
import sys
import logging
import warnings

import click

from aswfdocker import (
    builder,
    buildorder,
    aswfinfo,
    groupinfo,
    constants,
    dockergen as aswf_dockergen,
    index,
    utils,
    releaser,
    settings as aswf_settings,
)
from aswfdocker import conandiff as conandiff_mod

# Avoid pylint giving us grief for complex command lines
# pylint: disable=too-many-arguments
# pylint: disable=too-many-locals
# pylint: disable=too-many-branches

logger = logging.getLogger("build-images")


pass_build_info = click.make_pass_decorator(aswfinfo.ASWFInfo)


@click.group()
@click.option(
    "--repo-root",
    "-r",
    envvar="ASWF_REPO_ROOT",
    default=".",
    help="Root of aswf-docker repository",
)
@click.option("--repo-uri", "-u", help="URL of current Git Repository")
@click.option("--source-branch", "-b", help="Current git branch name")
@click.option("--verbose", "-v", is_flag=True, help="Enables verbose mode.")
@click.version_option("1.0")
@click.pass_context
def cli(ctx, repo_root, repo_uri, source_branch, verbose):
    """aswfdocker is a command line interface to build ASWF Docker packages and CI images"""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    ctx.obj = aswfinfo.ASWFInfo(
        repo_uri=repo_uri,
        source_branch=source_branch,
        repo_root=os.path.abspath(repo_root),
    )


def validate_image_name(_, __, value):
    if value is None:
        return None
    try:
        return utils.get_image_spec(value)
    except RuntimeError as e:
        raise click.BadParameter(e.args[0])


def common_image_options(function):
    function = click.option(
        "--ci-image-type",
        "-t",
        required=False,
        help="Builds a Conan package or a container image.",
        type=click.Choice(constants.ImageType.__members__.keys(), case_sensitive=True),
    )(function)
    function = click.option(
        "--group",
        "-g",
        required=False,
        multiple=True,
        help='The name of the group of images to build, e.g. "base" or "vfx",'
        " can be specified multiple times.",
    )(function)
    function = click.option(
        "--version",
        "-v",
        required=False,
        multiple=True,
        help='The major version number to build, e.g. "2027", can be specified multiple times.'
        ' Can also be "all".',
    )(function)
    function = click.option(
        "--full-name",
        "-n",
        callback=validate_image_name,
        required=False,
        help="The full image name, e.g. aswftesting/ci-common:2027-clang22 or aswf/ci-package-openexr:2027",
    )(function)
    function = click.option(
        "--target",
        "-tg",
        required=False,
        multiple=True,
        help='An optional package or image name to build, e.g. "usd", can be specified multiple times.',
    )(function)
    return function


def get_group_info(build_info, ci_image_type, groups, versions, full_name, targets):
    idx = index.Index()
    if full_name:
        org, image_type, target, version = full_name
        versions = [version]
        targets = [target]
        try:
            groups = [idx.get_group_from_image(image_type, target)]
        except RuntimeError as e:
            raise click.BadOptionUsage(option_name="--full-name", message=e.args[0])
        build_info.set_org(org)
    else:
        if ci_image_type is None:
            image_type = constants.ImageType.IMAGE
        else:
            image_type = constants.ImageType[ci_image_type]
        if not groups and targets:
            groups = [idx.get_group_from_image(image_type, targets[0])]
    group_info = groupinfo.GroupInfo(
        type_=image_type,
        names=groups,
        versions=versions,
        targets=targets,
    )
    return group_info


@cli.command()
@common_image_options
@click.option(
    "--push",
    "-p",
    type=click.Choice(["YES", "NO", "AUTO"], case_sensitive=False),
    default="NO",
    help="Push built images to Docker Hub and Conan packages to Artifactory "
    "(GitHub Actions only).",
)
@click.option("--dry-run", "-d", is_flag=True, help="Just logs what would happen.")
@click.option(
    "--progress",
    "-pr",
    type=click.Choice(("auto", "tty", "plain"), case_sensitive=True),
    default="auto",
    help='Set type of progress output for "docker buildx bake" command.',
)
@click.option(
    "--use-conan",
    "-c",
    is_flag=True,
    expose_value=False,
    deprecated="This option is no longer relevant since all packages are built with Conan.",
)
@click.option(
    "--keep-source",
    "-ks",
    is_flag=True,
    expose_value=False,
    deprecated="This option is no longer relevant with Conan 2.",
)
@click.option(
    "--keep-build",
    "-kb",
    is_flag=True,
    expose_value=False,
    deprecated="This option is no longer relevant with Conan 2.",
)
@click.option(
    "--conan-login",
    "-cl",
    is_flag=True,
    expose_value=False,
    deprecated="This option is no longer relevant as authentication is now handled via buildx secrets.",
)
@click.option(
    "--build-missing",
    "-bm",
    is_flag=True,
    help="Instruct Conan to build missing binary packages from source.",
)
@click.option(
    "--conan-remote",
    "-cr",
    default=constants.CONAN_REMOTE_AUTO,
    show_default=True,
    help=f'Conan remote policy. "{constants.CONAN_REMOTE_AUTO}": only the local '
    "Conan cache, except on GitHub Actions where only the remote named after the "
    f'package org (aswf or aswftesting) is used; "{constants.CONAN_REMOTE_LOCAL}": '
    "only the local Conan cache; any other value: only that named remote.",
)
@click.option(
    "--target-to",
    "-tt",
    multiple=True,
    help="PACKAGE builds only: build this package and everything in the group it "
    "transitively requires. Can be specified multiple times.",
)
@click.option(
    "--target-from",
    "-tf",
    multiple=True,
    help="PACKAGE builds only: build this package and everything in the group that "
    "transitively requires it. Can be specified multiple times.",
)
@click.option(
    "--force-rebuild",
    "-fr",
    is_flag=True,
    help="PACKAGE builds only: rebuild every selected package even if Conan considers "
    "its binary up to date (e.g. after a system library change). Image builds always "
    "rebuild.",
)
@click.option(
    "--docker-org",
    "-do",
    default="",
    help="The Docker organization (and Conan user/remote) to build for, instead of "
    "the one derived from the repository URI and branch.",
)
@pass_build_info
def build(  # pylint: disable=too-many-arguments,too-many-locals
    build_info,
    ci_image_type,
    group,
    version,
    full_name,
    target,
    push,
    dry_run,
    progress,
    build_missing,
    conan_remote,
    target_to,
    target_from,
    force_rebuild,
    docker_org,
):
    """Builds a Conan package or ci-image Docker image.

    Conan packages are built in dependency order, skipping packages whose
    binary is already up to date unless --force-rebuild is given.
    """
    if docker_org:
        build_info.set_org(docker_org)
    if push == "YES":
        pushb = True
    elif push == "AUTO":
        pushb = utils.get_docker_push(build_info.repo_uri, build_info.source_branch)
    else:
        pushb = False

    is_package = ci_image_type == constants.ImageType.PACKAGE.name
    if (target_to or target_from) and not is_package and not full_name:
        raise click.BadOptionUsage(
            "--target-to/--target-from",
            "--target-to/--target-from require --ci-image-type PACKAGE",
        )
    if is_package and not group and (target or target_to or target_from):
        # Infer the group(s) from every named package, not just the first.
        idx = index.Index()
        try:
            group = sorted(
                {
                    idx.get_group_from_image(constants.ImageType.PACKAGE, t)
                    for t in (*target, *target_to, *target_from)
                }
            )
        except RuntimeError as e:
            raise click.BadOptionUsage("--target-to/--target-from", e.args[0])

    group_info = get_group_info(
        build_info, ci_image_type, group, version, full_name, target
    )
    if pushb and not utils.on_github_actions():
        raise click.BadOptionUsage(
            "--push",
            "Conan packages and Docker images can only be uploaded to Artifactory "
            "and Docker Hub from GitHub Actions.",
        )
    selection = None
    if group_info.type == constants.ImageType.PACKAGE:
        # Package builds compute the build order over the whole group, so the
        # targets narrow what gets built rather than which members exist.
        selection = builder.PackageSelection(
            targets=group_info.targets,
            targets_to=target_to,
            targets_from=target_from,
            force=force_rebuild,
        )
        group_info = groupinfo.GroupInfo(
            type_=group_info.type,
            names=group_info.names,
            versions=group_info.versions,
            targets=[],
        )
    elif target_to or target_from:
        raise click.BadOptionUsage(
            "--target-to/--target-from",
            "--target-to/--target-from require --ci-image-type PACKAGE",
        )

    b = builder.Builder(
        build_info=build_info,
        group_info=group_info,
        push=pushb,
        conan_remote=conan_remote,
    )
    try:
        b.build(
            dry_run=dry_run,
            progress=progress,
            build_missing=build_missing,
            selection=selection,
        )
    except buildorder.BuildOrderError as e:
        raise click.ClickException(str(e))


@cli.command(deprecated="No longer relevant since all packages are built with Conan.")
@click.option("--from", "-f", "from_org", default="aswftesting")
@click.option("--to", "-t", "to_org", default="aswf")
@click.option(
    "--package",
    "-p",
    help="Optional package name to migrate (all packages are migrated by default)",
)
@click.option(
    "--version",
    "-v",
    help="Version of the package to migrate (all versions are migrated by default)",
)
@click.option("--dry-run", "-d", is_flag=True)
def migrate(**_kwargs):
    pass


@cli.command()
@pass_build_info
def getdockerorg(build_info):
    """Prints the current Docker Hub organization to use according to the current repo uri and branch name"""
    click.echo(
        utils.get_docker_org(build_info.repo_uri, build_info.source_branch), nl=False
    )


@cli.command()
@pass_build_info
def getdockerpush(build_info):
    """Prints if the images should be pushed according to the current repo uri and branch name"""
    click.echo(
        (
            "true"
            if utils.get_docker_push(build_info.repo_uri, build_info.source_branch)
            else "false"
        ),
        nl=False,
    )


@cli.command(deprecated="No longer relevant since all packages are built with Conan.")
@click.option(
    "--docker-org",
    "-d",
    default="aswf",
    help="Docker organization",
)
@click.option(
    "--package",
    "-p",
    help="Package name to download",
)
@click.option(
    "--version",
    "-v",
    help="Package version to download",
)
@pass_build_info
def download(*_args, **_kwargs):
    pass


@cli.command()
def packages():
    """Lists all known CI packages in this format: PACKAGEGROUP/ci-package-PACKAGE:VERSION,
    one line for each VFX Platform year (or year and clang version) a package is built for.
    """
    for group, pkgs in index.Index().groups[constants.ImageType.PACKAGE].items():
        for package in pkgs:
            image_name = utils.get_image_name(constants.ImageType.PACKAGE, package)
            for version in index.Index().iter_versions(
                constants.ImageType.PACKAGE, package
            ):
                click.echo(f"{group}/{image_name}:{version}")


@cli.command()
def images():
    """Lists all known CI images in this format: IMAGEGROUP/ci-IMAGE:VERSION"""
    for group, imgs in index.Index().groups[constants.ImageType.IMAGE].items():
        for image in imgs:
            image_name = utils.get_image_name(constants.ImageType.IMAGE, image)
            for version in index.Index().iter_versions(
                constants.ImageType.IMAGE, image
            ):
                click.echo(f"{group}/{image_name}:{version}")


@cli.command()
@click.option(
    "--settings-path",
    "-p",
    default="~/.aswfdocker",
    help="User settings file path.",
)
@click.option(
    "--github-access-token",
    "-g",
    required=True,
    help="GitHub access token generated from https://github.com/settings/tokens",
)
def settings(settings_path, github_access_token):
    """Sets user settings"""
    if not github_access_token.strip():
        raise click.BadParameter(
            "must not be empty", param_hint="'--github-access-token' / '-g'"
        )
    s = aswf_settings.Settings(settings_path=settings_path)
    s.github_access_token = github_access_token
    s.save()


@cli.command()
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


@cli.command()
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
):
    """Publishes the draft GitHub release (creating its tag) of each released
    image (GitHub Actions only), with the workflow's GITHUB_TOKEN."""
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
    releaser.ReleasePublisher(
        build_info=build_info,
        group_info=group_info,
        sha=sha,
        message=message,
        token=os.environ["GITHUB_TOKEN"],
        repository=os.environ["GITHUB_REPOSITORY"],
    ).publish()


@cli.command()
@click.pass_context
@click.option(
    "--image-name", "-n", default="all", help='Image name to generate. E.g. "base"'
)
@click.option(
    "--check", "-c", is_flag=True, help="Checks that the current files are up to date."
)
@click.option("--verbose", "-v", is_flag=True, help="Enables verbose mode.")
def dockergen(context, image_name, check, verbose):
    """Generates a Docker file and readme from image data and template"""
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    if image_name == "all":
        imgs = []
        for gimages in index.Index().groups[constants.ImageType.IMAGE].values():
            imgs.extend(gimages)
    else:
        imgs = [image_name]
    if check:
        for image in imgs:
            path, ok = aswf_dockergen.DockerGen(image).check_dockerfile()
            if not ok:
                click.secho(f"{path} is not up to date!", fg="red")
                context.exit(1)
            else:
                click.secho(f"{path} is up to date", fg="green")
            path, ok = aswf_dockergen.DockerGen(image).check_readme()
            if not ok:
                click.secho(f"{path} is not up to date!", fg="red")
                context.exit(1)
            else:
                click.secho(f"{path} is up to date", fg="green")
        for version_key in aswf_dockergen.conan_profile_version_keys(index.Index()):
            path, ok = aswf_dockergen.ConanProfileGen(version_key).check()
            if not ok:
                click.secho(f"{path} is not up to date!", fg="red")
                context.exit(1)
            else:
                click.secho(f"{path} is up to date", fg="green")
    else:
        for image in imgs:
            path = aswf_dockergen.DockerGen(image).generate_dockerfile()
            click.echo(f"Generated {path}")
            path = aswf_dockergen.DockerGen(image).generate_readme()
            click.echo(f"Generated {path}")
        for version_key in aswf_dockergen.conan_profile_version_keys(index.Index()):
            path = aswf_dockergen.ConanProfileGen(version_key).generate()
            click.echo(f"Generated {path}")


@cli.command()
@common_image_options
@click.option(
    "--username",
    "-u",
    help="Docker Hub username.",
)
@click.option(
    "--password",
    "-p",
    envvar="DOCKERHUB_PASSWORD",
    help="Docker Hub password (default: the DOCKERHUB_PASSWORD environment variable).",
)
@click.option(
    "--docker-org",
    "-do",
    default="",
    help="The Docker organization to push the descriptions to, instead of the "
    "one derived from the repository URI and branch.",
)
@pass_build_info
def pushoverview(
    build_info,
    ci_image_type,
    group,
    version,
    full_name,
    target,
    username,
    password,
    docker_org,
):
    """Pushes the Docker Image README file to Docker Hub"""
    if docker_org:
        build_info.set_org(docker_org)
    group_info = get_group_info(
        build_info, ci_image_type, group, version, full_name, target
    )
    if group_info.type == constants.ImageType.PACKAGE:
        click.echo("Package readme do not exist yet.")
        return
    if not utils.on_github_actions():
        raise click.UsageError(
            "Docker Hub descriptions can only be updated from GitHub Actions."
        )
    token = utils.get_dockerhub_token(username, password)
    for image, _ in group_info.iter_images_versions():
        dg = aswf_dockergen.DockerGen(image.replace("ci-", ""))
        if dg.push_overview(build_info.docker_org, token):
            click.secho(
                f"Successfully pushed description to image {build_info.docker_org}/{image}",
                fg="green",
            )
        else:
            click.secho(
                f"Failed to push description to image {build_info.docker_org}/{image}",
                fg="red",
            )
            click.get_current_context().exit(1)


@cli.command()
@click.option(
    "--group",
    "-g",
    required=False,
    multiple=True,
    help='The name of the group of recipes to check, e.g. "common",'
    " can be specified multiple times.",
)
@click.option(
    "--target",
    "-tg",
    required=False,
    multiple=True,
    help='An optional recipe name to check, e.g. "usd", can be specified multiple times.',
)
@click.option(
    "--branch",
    default=constants.CCI_DEFAULT_BRANCH,
    help=f"GitHub branch to check against (default: {constants.CCI_DEFAULT_BRANCH})",
)
@click.option(
    "--merge",
    "do_merge",
    is_flag=True,
    default=False,
    help="Attempt a 3-way merge of upstream changes into locally-modified recipe files.",
)
@click.option(
    "--update-manifest",
    "update_manifest",
    is_flag=True,
    default=False,
    help="Record the latest upstream SHA in the manifest for the selected recipes.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="With --merge, proceed even if the recipe directory has uncommitted changes.",
)
@click.option(
    "--dry-run",
    "-d",
    is_flag=True,
    default=False,
    help="With --merge, print the upstream file diff (tracked SHA vs current "
    "upstream) without merging.",
)
@pass_build_info
def conandiff(  # pylint: disable=too-many-statements
    build_info,
    group,
    target,
    branch,
    do_merge,
    update_manifest,
    force,
    dry_run,
):
    """Check for outdated vendored Conan recipes against Conan Center Index."""
    if do_merge and dry_run:
        preview = conandiff_mod.run_conanmerge_preview(
            build_info.repo_root, groups=group, targets=target, branch=branch
        )
        if not preview.diffs:
            click.secho(
                "No upstream changes found for the selected recipes.", fg="green"
            )
        for file_diff in preview.diffs:
            click.secho(
                f"\n{file_diff.recipe}: {file_diff.path} ({file_diff.status})",
                fg="yellow",
            )
            if file_diff.diff:
                click.echo(file_diff.diff)
        return

    merge_report = None
    if do_merge:
        merge_report = conandiff_mod.run_conanmerge(
            build_info.repo_root,
            groups=group,
            targets=target,
            branch=branch,
            force=force,
        )
        for result in merge_report.results:
            if result.skipped_reason:
                click.secho(
                    f"{result.recipe}: skipped ({result.skipped_reason})", fg="yellow"
                )
                continue
            if result.conflicts:
                click.secho(f"{result.recipe}: conflicts", fg="red")
                for c in result.conflicts:
                    click.echo(f"  {c.path}: {c.reason}")
            else:
                click.secho(f"{result.recipe}: clean merge", fg="green")
            for path in result.added:
                click.echo(f"  added: {path}")
            for path in result.removed:
                click.echo(f"  removed: {path}")
            for path in result.merged:
                click.echo(f"  merged: {path}")

    if update_manifest:
        updated = conandiff_mod.run_update_manifest(
            build_info.repo_root,
            groups=group,
            targets=target,
            branch=branch,
            merge_report=merge_report,
        )
        for name in updated:
            click.secho(f"{name}: manifest sha updated", fg="green")

    if do_merge or update_manifest:
        return

    report = conandiff_mod.run_conandiff(
        build_info.repo_root, groups=group, targets=target, branch=branch
    )
    found_outdated = False
    for outdated in report.outdated:
        found_outdated = True
        click.secho("\nFound outdated recipe:", fg="yellow")
        click.echo(f"  Recipe: {outdated.recipe}")
        click.echo(f"  Folder: {outdated.folder}")
        click.echo(f"  Current SHA: {outdated.tracked_sha}")
        click.echo(f"  Found {len(outdated.newer_commits)} newer commits:")
        for commit in outdated.newer_commits:
            click.echo(f"    Commit: {commit.sha}")
            click.echo(
                f"    Diff URL: https://github.com/{constants.CCI_GITHUB_ORG}/"
                f"{constants.CCI_GITHUB_REPO_NAME}/commit/{commit.sha}"
            )
            click.echo(f"    Timestamp: {commit.commit.author.date}")
            if "\n" in commit.commit.message:
                click.echo("    Message:")
                for line in commit.commit.message.split("\n"):
                    click.echo(f"      {line}")
            else:
                click.echo(f"    Message: {commit.commit.message}")
            click.echo()
        click.echo()
    for removed in report.removed:
        found_outdated = True
        click.secho(
            f"Removed upstream: {removed.recipe}/{removed.folder} ({removed.reason})",
            fg="red",
        )
    for error in report.errors:
        click.secho(
            f"Error checking {error.recipe}/{error.folder}: {error.message}", fg="red"
        )

    if not found_outdated:
        click.secho("\nAll recipes are up to date!", fg="green")
