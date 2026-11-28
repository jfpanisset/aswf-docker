# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Click options and helpers shared by the aswfdocker commands
"""

import click

from aswfdocker import aswfinfo, constants, groupinfo, index, utils

pass_build_info = click.make_pass_decorator(aswfinfo.ASWFInfo)


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
