# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Docker Image Version information
"""

import typing
from aswfdocker import constants


class VersionInfo:
    """
    Docker image version information for use in builder
    """

    def __init__(  # noqa too many arguments
        self,
        version: str,
        major_version: str,
        tags: typing.List[str],
        parent_versions: typing.List[str],
        package_versions: typing.Dict[str, str],
        use_major_version_as_tag=True,
        legacy_common_version: typing.Optional[str] = None,
    ):
        self.version = version
        self.major_version = major_version
        self.legacy_common_version = legacy_common_version
        self.tags = tags
        self.parent_versions = parent_versions
        self.package_versions = package_versions
        self.use_major_version_as_tag = use_major_version_as_tag
        self.all_package_versions: typing.Dict[str, str] = {}
        self.conan_profile: typing.Optional[str] = None
        self.generate_profile: bool = False
        # A VFX Platform year's clang versions (major -> full version)...
        self.clang_versions: typing.Dict[str, str] = {}
        # ...or, for a "<year>-clang<major>" version derived from one of
        # them, that major version.
        self.clang_major_version: typing.Optional[str] = None

    def get_tags(
        self,
        aswf_version: str,
        docker_org: str,
        image_name: str,
        extra_suffix: typing.Optional[str] = None,
    ) -> typing.List[str]:
        if self.use_major_version_as_tag:
            tags = [self.major_version]
            if self.major_version != self.version:
                tags.append(self.version)
        else:
            tags = [self.version]
        tags.append(aswf_version)
        if (
            self.legacy_common_version
            and image_name in constants.LEGACY_COMMON_TAGGED_IMAGES
        ):
            # e.g. 2027-clang22.3 -> 7-clang22.3
            tags.extend(
                self.legacy_common_version + tag[len(self.major_version) :]
                for tag in list(tags)
                if tag.startswith(self.major_version)
            )
        tags.extend(self.tags)
        if extra_suffix:
            tags.append(self.major_version + "-" + extra_suffix)

        return list(
            map(
                lambda tag: f"{constants.DOCKER_REGISTRY}/{docker_org}/{image_name}:{tag}",
                tags,
            )
        )
