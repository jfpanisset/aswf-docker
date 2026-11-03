# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the utility commands
"""

import unittest
import logging

from click.testing import CliRunner

from aswfdocker import constants, dockergen, index
from aswfdocker.cli import aswfdocker


class TestDockerGen(unittest.TestCase):
    def test_dockergen(self):
        dg = dockergen.DockerGen("base")
        self.assertTrue(dg.check_dockerfile())
        self.assertTrue(dg.check_readme())

    def test_images_from_ci_common_pin_its_release(self):
        # Never inadvertently built on top of a previous release's ci-common.
        for image in index.Index().iter_images(constants.ImageType.IMAGE):
            docker_from = dockergen.DockerGen(image).image_data["docker_from"]
            if "/ci-common:" in docker_from:
                self.assertTrue(
                    docker_from.endswith(".${ASWF_VFXPLATFORM_MINOR_VERSION}"),
                    msg=f"ci-{image}: {docker_from}",
                )


class TestUtilsCli(unittest.TestCase):
    def setUp(self):
        self._log_handlers = logging.getLogger("").handlers
        logging.getLogger("").handlers = []
        self.maxDiff = None

    def tearDown(self):
        logging.getLogger("").handlers = self._log_handlers

    def test_cli_dockergen(self):
        runner = CliRunner()
        result = runner.invoke(aswfdocker.cli, ["dockergen", "--check", "-n", "base"])
        self.assertEqual(result.exit_code, 0, msg=f"output: {result.output}")
        lines = result.output.split("\n")
        # First two lines are for the requested image; subsequent lines are
        # for Conan profiles (always checked regardless of --image-name).
        self.assertTrue(lines[0].endswith("ci-base/Dockerfile is up to date"))
        self.assertTrue(lines[1].endswith("ci-base/README.md is up to date"))
        self.assertTrue(
            any("profiles/vfx" in line for line in lines),
            "Expected Conan vfx profile check lines in output",
        )
