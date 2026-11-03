# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Index of Docker images and versions.
"""

import logging
import typing
import yaml
import importlib_resources

from aswfdocker import constants, utils, versioninfo

logger = logging.getLogger(__name__)


class Index:
    """
    This is the index of all current package, images and versions.
    The data comes from the "versions.yaml" file at the root of the git repository.
    """

    def __init__(self):
        path = importlib_resources.files("aswfdocker.data").joinpath("versions.yaml")
        if not path.exists():
            raise RuntimeError(f"versions.yaml file does not exist at path {path}")
        logger.debug("version path: %s (%s)", path, type(path))
        with path.open() as f:
            self._versions = yaml.load(f, Loader=yaml.FullLoader)
        self.groups = {
            constants.ImageType.IMAGE: self._versions["groups"]["image"],
            constants.ImageType.PACKAGE: self._versions["groups"]["package"],
        }
        self.per_clang_packages = set(self._versions.get("per_clang_packages", []))
        self._version_infos: typing.Dict[str, versioninfo.VersionInfo] = {}
        for version, v in self._versions["versions"].items():
            if v.get("major_version") and not v.get("minor_version"):
                raise RuntimeError(f"Version {version} has no minor_version")
            vi = versioninfo.VersionInfo(
                version=version,
                major_version=v.get("major_version"),
                minor_version=v.get("minor_version"),
                tags=v.get("tags", []),
                legacy_common_version=v.get("legacy_common_version"),
                package_versions=dict(v.get("package_versions", {})),
                parent_versions=[],
                use_major_version_as_tag=False,
            )
            vi.conan_profile = v.get("conan_profile")
            vi.generate_profile = v.get("generate_profile", False)
            vi.clang_versions = v.get("clang_versions", {})
            self._version_infos[version] = vi
            self._add_clang_versions(vi)

        for vi in self._version_infos.values():
            for parent in vi.parent_versions:
                vi.all_package_versions.update(
                    self._version_infos[parent].package_versions
                )
            vi.all_package_versions.update(vi.package_versions)
            # OS-distro package_versions (bison/flex/..., and system
            # clang/OSMesa versions) live in a side entry keyed by
            # ASWF_BASEOS_DISTRO (e.g. "rockylinux8") rather than in a
            # year's own package_versions, since they're inherited by distro.
            # Fill in any that aren't already set, without overriding
            # anything more specific.
            baseos_distro = vi.all_package_versions.get("ASWF_BASEOS_DISTRO")
            if baseos_distro and baseos_distro in self._version_infos:
                for key, value in self._version_infos[
                    baseos_distro
                ].package_versions.items():
                    vi.all_package_versions.setdefault(key, value)

    def _add_clang_versions(self, year: versioninfo.VersionInfo) -> None:
        """Derives a "<year>-clang<major>" version from each of a year's
        clang_versions, and pins the year's own default clang version."""
        if not year.clang_versions:
            return
        default = year.package_versions.get("ASWF_CLANG_MAJOR_VERSION")
        if default not in year.clang_versions:
            raise RuntimeError(
                f"Version {year.version}: ASWF_CLANG_MAJOR_VERSION {default} "
                f"is not one of its clang_versions {list(year.clang_versions)}"
            )
        year.package_versions["ASWF_CLANG_VERSION"] = year.clang_versions[default]
        majors = list(year.clang_versions)
        for major in majors:
            # Only the newest clang version gets the bare year (and the
            # year's extra tags like "latest"), so that tag is unambiguous.
            newest = major == majors[-1]
            vi = versioninfo.VersionInfo(
                version=f"{year.version}-clang{major}",
                major_version=year.major_version,
                minor_version=year.minor_version,
                tags=year.tags if newest else [],
                legacy_common_version=year.legacy_common_version,
                package_versions={
                    "ASWF_CLANG_MAJOR_VERSION": major,
                    "ASWF_CLANG_VERSION": year.clang_versions[major],
                },
                parent_versions=[year.version],
                use_major_version_as_tag=newest,
            )
            vi.conan_profile = year.conan_profile
            vi.clang_major_version = major
            self._version_infos[vi.version] = vi

    def iter_images(self, image_type: constants.ImageType):
        """
        Iterates over all images (or Conan packages) by image type.
        """
        if image_type == constants.ImageType.PACKAGE:
            for packages in self.groups[image_type].values():
                yield from packages
        else:
            yield from self._versions["ci-images"]

    def iter_versions(self, image_type: constants.ImageType, name: str):
        """
        Iterates over all versions by image type and image name. An image
        is built for every year's current release (see ci-images in
        versions.yaml), e.g. "2027.4", or "2027-clang22.4" for each of the
        year's clang versions. A Conan package is built for every year that
        pins its version, or for each of those years' clang versions if it's
        one of per_clang_packages.
        """
        if image_type == constants.ImageType.IMAGE:
            image = self._versions["ci-images"][name] or {}
            excluded = image.get("exclude_versions", [])
            for year in self.iter_years():
                if year.version in excluded:
                    continue
                if image.get("per_clang"):
                    for major in year.clang_versions:
                        yield f"{year.version}-clang{major}.{year.minor_version}"
                else:
                    yield f"{year.version}.{year.minor_version}"
            return
        key = utils.package_version_key(name)
        for year in self.iter_years():
            if key not in year.all_package_versions:
                continue
            if name in self.per_clang_packages:
                for major in year.clang_versions:
                    yield f"{year.version}-clang{major}"
            else:
                yield year.version

    def iter_years(self):
        """VFX Platform year versions, e.g. "2027" but not "2027-clang22"."""
        for vi in self._version_infos.values():
            if vi.major_version and vi.clang_major_version is None:
                yield vi

    def iter_version_info(self):
        return self._version_infos.values()

    def version_info(self, version):
        for vi in self.iter_version_info():
            if version == vi.version:
                return vi
        raise ValueError(f"VersionInfo not found for version {version}")

    def package_data(self, package_name):
        return self._versions["package_data"].get(package_name, {})

    def get_group_from_image(self, image_type: constants.ImageType, image: str):
        for group, images in self.groups[image_type].items():
            for img in images:
                if img == image:
                    return group
        raise RuntimeError(f"Cannot find group for image {image}")
