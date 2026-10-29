# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the `conan aswf-export` custom command
(packages/conan/settings/extensions/commands/cmd_aswf_export.py).

Like the deployer, that module lives outside the aswfdocker package and is
loaded by Conan from $CONAN_HOME/extensions/commands, so it's loaded here
by file path. export_all() is exercised against a fake conan_api that
records the calls the built-in `conan export` command would make.
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
    "cmd_aswf_export.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("cmd_aswf_export", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRef:
    def __init__(self, text):
        self._text = text

    def repr_notime(self):
        return self._text


class _FakeLocalAPI:
    @staticmethod
    def get_conanfile_path(path, cwd, py):
        assert py is True
        return os.path.join(cwd, path, "conanfile.py")


_IMPLICIT_LOCKFILE = object()


class _FakeLockfileAPI:
    @staticmethod
    def get_lockfile(lockfile, conanfile_path, cwd, partial):
        # No explicit lockfile, but Conan may still pick up an implicit
        # conan.lock next to the recipe -- export() must be handed whatever
        # comes back, same as the built-in `conan export`.
        assert lockfile is None and conanfile_path and cwd and partial is False
        return _IMPLICIT_LOCKFILE


class _FakeExportAPI:
    def __init__(self):
        self.calls = []

    def export(self, **kwargs):
        self.calls.append(kwargs)
        ref = (
            f"{kwargs['name']}/{kwargs['version']}@{kwargs['user']}/{kwargs['channel']}"
        )
        return _FakeRef(ref), None


class _FakeConanAPI:
    def __init__(self):
        self.local = _FakeLocalAPI()
        self.lockfile = _FakeLockfileAPI()
        self.export = _FakeExportAPI()


class TestExportAll(unittest.TestCase):
    def test_exports_each_entry_in_order(self):
        module = _load_module()
        api = _FakeConanAPI()
        refs = module.export_all(
            api,
            [
                ["recipes/zlib", "zlib", "1.3.1", "aswf", "vfx2027"],
                ["recipes/cmake", "cmake", "4.3.3", "aswf", "ci_common7"],
            ],
            "/work",
            ["remote"],
        )
        self.assertEqual(
            [r.repr_notime() for r in refs],
            ["zlib/1.3.1@aswf/vfx2027", "cmake/4.3.3@aswf/ci_common7"],
        )
        self.assertEqual(
            api.export.calls,
            [
                {
                    "path": "/work/recipes/zlib/conanfile.py",
                    "name": "zlib",
                    "version": "1.3.1",
                    "user": "aswf",
                    "channel": "vfx2027",
                    "lockfile": _IMPLICIT_LOCKFILE,
                    "remotes": ["remote"],
                },
                {
                    "path": "/work/recipes/cmake/conanfile.py",
                    "name": "cmake",
                    "version": "4.3.3",
                    "user": "aswf",
                    "channel": "ci_common7",
                    "lockfile": _IMPLICIT_LOCKFILE,
                    "remotes": ["remote"],
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
