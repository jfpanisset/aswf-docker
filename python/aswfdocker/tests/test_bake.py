# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for bake.py
"""

import json
import os
import tempfile
import unittest

from aswfdocker import bake


class TestWriteBakeJsonfile(unittest.TestCase):
    def test_round_trips_content(self):
        bake_dict = {"target": {"foo": {"args": {"A": "1"}}}}
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bake.json")
            bake.write_bake_jsonfile(path, bake_dict)
            with open(path, "r", encoding="utf-8") as f:
                self.assertEqual(json.load(f), bake_dict)


class TestBakeCommand(unittest.TestCase):
    def test_no_targets_or_overrides(self):
        cmd = bake.bake_command(
            "/tmp/bake.json", build_targets=[], overrides={}, progress="auto"
        )
        self.assertEqual(
            cmd,
            ["docker", "buildx", "bake", "-f", "/tmp/bake.json", "--progress", "auto"],
        )

    def test_overrides_use_wildcard_scope_by_default(self):
        cmd = bake.bake_command(
            "/tmp/bake.json",
            build_targets=["ci-package-osl-2027"],
            overrides={
                "output": "type=cacheonly",
                "target": "ci-conan-package-builder",
            },
            progress="auto",
        )
        self.assertEqual(
            cmd,
            [
                "docker",
                "buildx",
                "bake",
                "-f",
                "/tmp/bake.json",
                "--set=*.output=type=cacheonly",
                "--set=*.target=ci-conan-package-builder",
                "--progress",
                "auto",
                "ci-package-osl-2027",
            ],
        )

    def test_overrides_use_named_scope_when_given(self):
        cmd = bake.bake_command(
            "/tmp/bake.json",
            build_targets=["ci-conan-graph"],
            overrides={
                "target": "ci-conan-graph-output",
                "output": "type=local,dest=/tmp/out",
            },
            progress="quiet",
            override_scope="ci-conan-graph",
        )
        self.assertEqual(
            cmd,
            [
                "docker",
                "buildx",
                "bake",
                "-f",
                "/tmp/bake.json",
                "--set=ci-conan-graph.target=ci-conan-graph-output",
                "--set=ci-conan-graph.output=type=local,dest=/tmp/out",
                "--progress",
                "quiet",
                "ci-conan-graph",
            ],
        )


if __name__ == "__main__":
    unittest.main()
