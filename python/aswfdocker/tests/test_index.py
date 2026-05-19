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
        self.assertEqual(packages[0], "clang")

    def test_get_versions(self):
        images = list(self.index.iter_images(constants.ImageType.IMAGE))
        self.assertGreater(len(images), 1)
        self.assertEqual(images[0], "baseos-gl-conan")
        versions = list(self.index.iter_versions(constants.ImageType.IMAGE, images[0]))
        self.assertGreaterEqual(len(versions), 1)
        self.assertTrue(versions[0].startswith("3"))

    def test_version_info(self):
        vi = self.index.version_info("2019")
        self.assertTrue(vi)
        self.assertEqual(vi.version, "2019")
        # Version 2022 pre-dates the generate_profile flag: must default to False
        vi = self.index.version_info("2022")
        self.assertFalse(vi.generate_profile)
        # Version 2023 onwards opts in to profile generation
        vi = self.index.version_info("2023")
        self.assertTrue(vi.generate_profile)
        # Version 2026 uses ci_common6 as its toolchain base
        vi = self.index.version_info("2026")
        self.assertEqual(vi.ci_common_version, "6")

    def test_group_from_image(self):
        self.assertEqual(
            self.index.get_group_from_image(constants.ImageType.PACKAGE, "clang"),
            "common-2",
        )
