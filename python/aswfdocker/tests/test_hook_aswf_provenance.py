# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the hook_aswf_provenance Conan hook
"""

import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "packages",
    "conan",
    "settings",
    "extensions",
    "hooks",
    "hook_aswf_provenance.py",
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("hook_aswf_provenance", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


class _FakeOutput:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class _FakeConanFile:
    def __init__(self, package_metadata_folder):
        self.package_metadata_folder = package_metadata_folder
        self.output = _FakeOutput()


_ENV_VARS = ("ASWF_GIT_SHA", "ASWF_GIT_DIRTY", "ASWF_GIT_DIFF_HASH")


class TestPostPackage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        # Conan creates the metadata folder lazily; the hook must cope.
        self.metadata = os.path.join(tmp.name, "metadata")
        self.conanfile = _FakeConanFile(self.metadata)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in _ENV_VARS:
            os.environ.pop(name, None)

    def _provenance(self):
        with open(
            os.path.join(self.metadata, hook.PROVENANCE_FILE), encoding="utf-8"
        ) as f:
            return json.load(f)

    def test_nothing_written_outside_aswfdocker_builds(self):
        hook.post_package(self.conanfile)
        self.assertFalse(os.path.exists(self.metadata))
        self.assertEqual(self.conanfile.output.messages, [])

    def test_empty_sha_is_treated_as_unset(self):
        # The Dockerfile ARG exists even when aswfdocker doesn't set it.
        os.environ["ASWF_GIT_SHA"] = ""
        hook.post_package(self.conanfile)
        self.assertFalse(os.path.exists(self.metadata))

    def test_clean_build(self):
        os.environ.update(
            {
                "ASWF_GIT_SHA": "abc123",
                "ASWF_GIT_DIRTY": "false",
                "ASWF_GIT_DIFF_HASH": "",
            }
        )
        hook.post_package(self.conanfile)
        self.assertEqual(self._provenance(), {"git_sha": "abc123", "git_dirty": False})
        self.assertEqual(len(self.conanfile.output.messages), 1)

    def test_dirty_build(self):
        os.environ.update(
            {
                "ASWF_GIT_SHA": "abc123",
                "ASWF_GIT_DIRTY": "true",
                "ASWF_GIT_DIFF_HASH": "f00d",
            }
        )
        hook.post_package(self.conanfile)
        self.assertEqual(
            self._provenance(),
            {"git_sha": "abc123", "git_dirty": True, "git_diff_hash": "f00d"},
        )


if __name__ == "__main__":
    unittest.main()
