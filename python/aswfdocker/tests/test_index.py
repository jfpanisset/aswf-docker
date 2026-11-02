# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for user settings
"""

import unittest

from aswfdocker import constants, index


class TestIndex(unittest.TestCase):
    def setUp(self):
        self.index = index.Index()

    def test_iter_images(self):
        packages = list(self.index.iter_images(constants.ImageType.PACKAGE))
        self.assertGreater(len(packages), 15)
        self.assertEqual(packages[0], "openssl")
        self.assertIn("clang", packages)

    def test_get_versions(self):
        images = list(self.index.iter_images(constants.ImageType.IMAGE))
        self.assertGreater(len(images), 1)
        self.assertEqual(images[0], "baseos-gl-conan")
        versions = list(self.index.iter_versions(constants.ImageType.IMAGE, images[0]))
        self.assertGreaterEqual(len(versions), 1)
        self.assertTrue(versions[0].startswith("2023."))

    def test_package_versions_follow_year_pins(self):
        # Built for every year that pins a version for it...
        self.assertEqual(
            list(self.index.iter_versions(constants.ImageType.PACKAGE, "cmake")),
            ["2023", "2024", "2025", "2026", "2027"],
        )
        # ...but not for a year that doesn't.
        del self.index.version_info("2023").all_package_versions["ASWF_CMAKE_VERSION"]
        self.assertEqual(
            list(self.index.iter_versions(constants.ImageType.PACKAGE, "cmake")),
            ["2024", "2025", "2026", "2027"],
        )

    def test_per_clang_package_versions(self):
        versions = list(self.index.iter_versions(constants.ImageType.PACKAGE, "clang"))
        self.assertIn("2027-clang21", versions)
        self.assertIn("2027-clang22", versions)
        self.assertNotIn("2027", versions)

    def test_version_info(self):
        vi = self.index.version_info("2027")
        self.assertTrue(vi)
        self.assertEqual(vi.version, "2027")
        self.assertTrue(vi.generate_profile)
        self.assertEqual(vi.conan_profile, "vfx2027")
        # Toolchain pins live in the year itself
        self.assertIn("ASWF_GCC_VERSION", vi.package_versions)
        # Distro entries aren't years and don't generate profiles
        self.assertFalse(self.index.version_info("rockylinux9").generate_profile)
        self.assertNotIn("rockylinux9", [y.version for y in self.index.iter_years()])

    def test_clang_versions(self):
        year = self.index.version_info("2027")
        majors = list(year.clang_versions)
        self.assertGreaterEqual(len(majors), 2)
        # The year itself pins its default clang version...
        default = year.all_package_versions["ASWF_CLANG_MAJOR_VERSION"]
        self.assertEqual(
            year.all_package_versions["ASWF_CLANG_VERSION"],
            year.clang_versions[default],
        )
        for major in majors:
            vi = self.index.version_info(f"2027-clang{major}")
            # ...each derived clang version overrides it, inheriting the rest
            self.assertEqual(vi.all_package_versions["ASWF_CLANG_MAJOR_VERSION"], major)
            self.assertEqual(
                vi.all_package_versions["ASWF_CLANG_VERSION"],
                year.clang_versions[major],
            )
            self.assertEqual(
                vi.all_package_versions["ASWF_GCC_VERSION"],
                year.all_package_versions["ASWF_GCC_VERSION"],
            )
            self.assertEqual(vi.major_version, "2027")
            self.assertEqual(vi.conan_profile, "vfx2027")
            # Only the newest clang version gets the bare year tag
            self.assertEqual(vi.use_major_version_as_tag, major == majors[-1])
            self.assertEqual(vi.tags, year.tags if major == majors[-1] else [])
        self.assertNotIn("2027-clang21", [y.version for y in self.index.iter_years()])

    def test_group_from_image(self):
        self.assertEqual(
            self.index.get_group_from_image(constants.ImageType.PACKAGE, "clang"),
            "common",
        )
