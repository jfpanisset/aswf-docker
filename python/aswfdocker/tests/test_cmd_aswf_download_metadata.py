# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the `conan aswf-download-metadata` custom command
(packages/conan/settings/extensions/commands/cmd_aswf_download_metadata.py).

Like the deployer, that module lives outside the aswfdocker package and is
loaded by Conan from $CONAN_HOME/extensions/commands, so it's loaded here
by file path.
"""

import importlib.util
import os
import unittest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_MODULE_PATH = os.path.join(
    _REPO_ROOT,
    "packages",
    "conan",
    "settings",
    "extensions",
    "commands",
    "cmd_aswf_download_metadata.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "cmd_aswf_download_metadata", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _node(ref, binary, binary_remote, package_id="pid", prev="prev1"):
    return {
        "ref": ref,
        "recipe": "Downloaded",
        "rrev_timestamp": 1700000000.0,
        "package_id": package_id,
        "prev": prev,
        "prev_timestamp": 1700000001.0,
        "binary": binary,
        "binary_remote": binary_remote,
    }


_GRAPH = {
    "graph": {
        "nodes": {
            # The consumer conanfile.txt
            "0": {
                "ref": "",
                "recipe": "Consumer",
                "package_id": None,
                "prev": None,
                "binary": None,
                "binary_remote": None,
            },
            "1": _node(
                "openexr/3.4.3@aswf/vfx2027_4#rrev1", "Download", "aswf", "pid1"
            ),
            # Already in the cache: nothing to download.
            "2": _node("imath/3.2.2@aswf/vfx2027_4#rrev2", "Cache", None, "pid2"),
            # A tool requirement `conan install` skips, though it's on the
            # remote.
            "3": _node("cmake/4.1.2@aswf/vfx2027_4#rrev3", "Skip", "aswf", "pid3"),
            # On another remote.
            "4": _node("zlib/1.3.1@aswf/vfx2027_4#rrev4", "Download", "other", "pid4"),
            "5": _node("ptex/2.5.0@aswf/vfx2027_4#rrev5", "Download", "aswf", "pid5"),
        }
    }
}


class _FakeDownloadAPI:
    def __init__(self):
        self.calls = []

    def recipe(self, ref, remote, metadata=None):
        self.calls.append(("recipe", str(ref), remote.name, metadata))

    def package(self, pref, remote, metadata=None):
        self.calls.append(("package", pref.repr_notime(), remote.name, metadata))


class _FakeRemote:
    name = "aswf"


class _FakeConanAPI:
    def __init__(self):
        self.download = _FakeDownloadAPI()


class TestPackagesToDownload(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()

    def test_only_downloads_from_remote(self):
        prefs = self.module.packages_to_download(_GRAPH, "aswf")
        self.assertEqual(
            [p.repr_notime() for p in prefs],
            [
                "openexr/3.4.3@aswf/vfx2027_4#rrev1:pid1#prev1",
                "ptex/2.5.0@aswf/vfx2027_4#rrev5:pid5#prev1",
            ],
        )
        self.assertEqual(prefs[0].ref.timestamp, 1700000000.0)
        self.assertEqual(prefs[0].timestamp, 1700000001.0)

    def test_nothing_to_download(self):
        self.assertEqual(self.module.packages_to_download(_GRAPH, "missing"), [])


class TestDownloadWithMetadata(unittest.TestCase):
    def setUp(self):
        self.module = _load_module()

    def test_downloads_recipe_then_package_with_metadata(self):
        conan_api = _FakeConanAPI()
        prefs = self.module.packages_to_download(_GRAPH, "aswf")
        self.module.download_with_metadata(
            conan_api, prefs, _FakeRemote(), ["aswf_provenance.json"]
        )
        self.assertEqual(
            conan_api.download.calls,
            [
                ("recipe", "openexr/3.4.3@aswf/vfx2027_4", "aswf", None),
                (
                    "package",
                    "openexr/3.4.3@aswf/vfx2027_4#rrev1:pid1#prev1",
                    "aswf",
                    ["aswf_provenance.json"],
                ),
                ("recipe", "ptex/2.5.0@aswf/vfx2027_4", "aswf", None),
                (
                    "package",
                    "ptex/2.5.0@aswf/vfx2027_4#rrev5:pid5#prev1",
                    "aswf",
                    ["aswf_provenance.json"],
                ),
            ],
        )
