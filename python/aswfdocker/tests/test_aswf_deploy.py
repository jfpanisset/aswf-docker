# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Tests for packages/conan/settings/extensions/deployers/aswf_deploy.py.

That module must stay import-free of the `aswfdocker` pip package (nothing
guarantees it's installed inside the CI image being built), so it's loaded
here by file path -- mirroring how Conan's own deployer loader works -- and
exercised against real temporary directories rather than a mocked
filesystem, since the module is fundamentally filesystem-shaped.
"""

# pylint: disable=protected-access,too-many-lines

import contextlib
import hashlib
import importlib.util
import io
import os
import json
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_MODULE_PATH = os.path.join(
    _REPO_ROOT,
    "packages",
    "conan",
    "settings",
    "extensions",
    "deployers",
    "aswf_deploy.py",
)


def _load_aswf_deploy():
    spec = importlib.util.spec_from_file_location("aswf_deploy", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aswf_deploy = _load_aswf_deploy()


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as f:
        f.write(content)


class _FakeRef:
    def __init__(self, name, version="1.0"):
        self.name = name
        self.version = version


class _FakePref:
    def __init__(self, package_id):
        self.package_id = package_id


class _FakeDep:
    def __init__(
        self,
        name,
        package_folder,
        version="1.0",
        context="host",
        package_metadata_folder=None,
    ):
        self.ref = _FakeRef(name, version)
        self.pref = _FakePref(f"{name}-pid")
        self.package_folder = package_folder
        self.package_metadata_folder = package_metadata_folder
        self.context = context  # not read by the deployer; documents intent


class _FakeRequirement:
    def __init__(self, name, build=False, direct=True):
        self.ref = _FakeRef(name)
        self.build = build
        self.direct = direct


class _FakeConf:
    def __init__(self, values=None):
        self._values = values or {}

    def get(
        self, name, default=None, check_type=None
    ):  # pylint: disable=unused-argument
        return self._values.get(name, default)


class _FakeDependencies:
    def __init__(self, deps):
        # deps: iterable of (requirement, dep) pairs, mirroring Conan's own
        # ConanFileDependencies (keyed by Requirement, valued by the dep).
        self._deps = list(deps)

    def values(self):
        return [dep for _req, dep in self._deps]

    def items(self):
        return list(self._deps)


def _direct_host_dep(dep):
    """Wraps a bare _FakeDep as a (requirement, dep) pair with a default
    direct/host requirement, for tests that don't care about scope
    filtering and just want a plain dependency."""
    return (_FakeRequirement(dep.ref.name), dep)


class _FakeConanFile:
    def __init__(self, conf_values=None, deps=()):
        self.conf = _FakeConf(conf_values)
        self.dependencies = _FakeDependencies(deps)


class _FakeGraphRoot:
    def __init__(self, conanfile):
        self.conanfile = conanfile


class _FakeGraph:
    def __init__(self, conanfile):
        self.root = _FakeGraphRoot(conanfile)


def _fake_cyclonedx_1_6(conanfile, name=None, **_kwargs):
    """Stands in for conan.tools.sbom.cyclonedx_1_6(), which needs a real
    Conan graph (conanfile.subgraph): one component per dependency of the
    fake conanfile, all depended on by the root, in Conan's output shape."""
    components = []
    for _req, dep in conanfile.dependencies.items():
        ref = f"pkg:conan/{dep.ref.name}@{dep.ref.version}"
        components.append(
            {"bom-ref": ref, "name": dep.ref.name, "version": str(dep.ref.version)}
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "metadata": {"component": {"bom-ref": "root", "name": name}},
        "components": components,
        "dependencies": [
            {"ref": "root", "dependsOn": [c["bom-ref"] for c in components]}
        ]
        + [{"ref": c["bom-ref"]} for c in components],
    }


def _copy_tree(dep, output, symlinks=True):
    """Test helper: builds a fresh report and calls _copy_package_tree,
    returning the report so tests can inspect warnings/errors."""
    report = aswf_deploy._DeployReport()
    aswf_deploy._copy_package_tree(dep, output, symlinks, report)
    return report


class TestShouldSkip(unittest.TestCase):
    def test_none_package_folder_skipped(self):
        dep = _FakeDep("foo", None)
        self.assertTrue(aswf_deploy._should_skip(dep, None))

    def test_system_version_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "include"))
            dep = _FakeDep("opengl", tmp, version="system")
            self.assertTrue(aswf_deploy._should_skip(dep, None))

    def test_empty_package_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "conaninfo.txt"), "stuff")
            dep = _FakeDep("wrapper", tmp)
            self.assertTrue(aswf_deploy._should_skip(dep, None))

    def test_excluded_by_name_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "include"))
            dep = _FakeDep("openexr", tmp)
            self.assertTrue(aswf_deploy._should_skip(dep, "openexr"))

    def test_excluded_name_only_matches_that_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "include"))
            dep = _FakeDep("zlib", tmp)
            self.assertFalse(aswf_deploy._should_skip(dep, "openexr"))

    def test_normal_package_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "include"))
            dep = _FakeDep("zlib", tmp)
            self.assertFalse(aswf_deploy._should_skip(dep, None))


class TestShouldSkipScope(unittest.TestCase):
    """_should_skip_scope(require) is the Requirement-level scoping check,
    separate from _should_skip's dep/filesystem checks above. See the
    ci-usd/osl bug this encodes: osl.requires("clang/...") is a genuine host
    dependency of osl, but reaches the root non-direct (require.direct is
    False), and must be skipped since it's expected to collide with whatever
    clang is already baked into the base image."""

    def test_host_direct_kept(self):
        req = _FakeRequirement("openusd", build=False, direct=True)
        self.assertFalse(aswf_deploy._should_skip_scope(req))

    def test_host_transitive_kept(self):
        # e.g. osl itself, pulled in transitively via openusd -- host-context
        # dependencies are kept at any depth.
        req = _FakeRequirement("osl", build=False, direct=False)
        self.assertFalse(aswf_deploy._should_skip_scope(req))

    def test_build_direct_kept(self):
        # e.g. ci-common's own conanfile.txt directly requiring cmake/ninja.
        req = _FakeRequirement("cmake", build=True, direct=True)
        self.assertFalse(aswf_deploy._should_skip_scope(req))

    def test_build_transitive_skipped(self):
        # e.g. a transitive tool_requires of some other dependency we are
        # not rebuilding ourselves.
        req = _FakeRequirement("cmake", build=True, direct=False)
        self.assertTrue(aswf_deploy._should_skip_scope(req))

    def test_clang_direct_build_kept(self):
        # ci-common's conanfile.txt lists clang directly.
        req = _FakeRequirement("clang", build=True, direct=True)
        self.assertFalse(aswf_deploy._should_skip_scope(req))

    def test_clang_direct_host_kept(self):
        req = _FakeRequirement("clang", build=False, direct=True)
        self.assertFalse(aswf_deploy._should_skip_scope(req))

    def test_clang_transitive_host_context_skipped(self):
        # The exact ci-usd/osl bug shape: osl.requires("clang/...") is host
        # context but non-direct from the root.
        req = _FakeRequirement("clang", build=False, direct=False)
        self.assertTrue(aswf_deploy._should_skip_scope(req))

    def test_clang_transitive_build_context_skipped(self):
        req = _FakeRequirement("clang", build=True, direct=False)
        self.assertTrue(aswf_deploy._should_skip_scope(req))


class TestCacheRoot(unittest.TestCase):
    def test_reads_storage_path_from_global_conf(self):
        with tempfile.TemporaryDirectory() as conan_home:
            _write(
                os.path.join(conan_home, "global.conf"),
                "core.cache:storage_path=/custom/cache/path\n",
            )
            self.assertEqual(aswf_deploy._cache_root(conan_home), "/custom/cache/path")

    def test_falls_back_to_default_when_absent(self):
        with tempfile.TemporaryDirectory() as conan_home:
            self.assertEqual(
                aswf_deploy._cache_root(conan_home),
                os.path.join(conan_home, "p"),
            )

    def test_falls_back_when_global_conf_has_no_matching_line(self):
        with tempfile.TemporaryDirectory() as conan_home:
            _write(
                os.path.join(conan_home, "global.conf"),
                "core.some:other_setting=value\n",
            )
            self.assertEqual(
                aswf_deploy._cache_root(conan_home),
                os.path.join(conan_home, "p"),
            )


class TestCopyAndMerge(unittest.TestCase):
    def test_generic_copy_merges_shared_destination_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg_a, "include", "a.h"), "a")
            _write(os.path.join(pkg_b, "include", "b.h"), "b")

            _copy_tree(_FakeDep("a", pkg_a), output, True)
            _copy_tree(_FakeDep("b", pkg_b), output, True)

            self.assertTrue(os.path.isfile(os.path.join(output, "include", "a.h")))
            self.assertTrue(os.path.isfile(os.path.join(output, "include", "b.h")))

    def test_bookkeeping_files_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "conaninfo.txt"), "info")
            _write(os.path.join(pkg, "conanmanifest.txt"), "manifest")
            _write(os.path.join(pkg, "include", "a.h"), "a")

            _copy_tree(_FakeDep("a", pkg), output, True)

            self.assertFalse(os.path.exists(os.path.join(output, "conaninfo.txt")))
            self.assertFalse(os.path.exists(os.path.join(output, "conanmanifest.txt")))
            self.assertTrue(os.path.isfile(os.path.join(output, "include", "a.h")))

    def test_licenses_excluded_from_generic_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "licenses", "LICENSE"), "text")
            _write(os.path.join(pkg, "include", "a.h"), "a")

            _copy_tree(_FakeDep("a", pkg), output, True)

            self.assertFalse(os.path.exists(os.path.join(output, "licenses")))


class TestLicenses(unittest.TestCase):
    def test_basic_namespacing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "licenses", "LICENSE"), "text")

            aswf_deploy._deploy_licenses(_FakeDep("zlib", pkg), output)

            self.assertTrue(
                os.path.isfile(os.path.join(output, "licenses", "zlib", "LICENSE"))
            )

    def test_self_nested_recipe_deployed_as_is_not_double_nested(self):
        # Recipes not yet migrated off the old per-recipe licenses/<name>
        # local modification already produce licenses/<name>/... themselves;
        # the deployer must not add a second <name> level on top of that.
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "licenses", "zlib", "LICENSE"), "text")

            aswf_deploy._deploy_licenses(_FakeDep("zlib", pkg), output)

            self.assertTrue(
                os.path.isfile(os.path.join(output, "licenses", "zlib", "LICENSE"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(output, "licenses", "zlib", "zlib"))
            )

    def test_no_licenses_dir_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            os.makedirs(pkg)
            aswf_deploy._deploy_licenses(_FakeDep("zlib", pkg), output)
            self.assertFalse(os.path.exists(os.path.join(output, "licenses")))


class TestCachePathRewrite(unittest.TestCase):
    def test_cmake_file_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            baked_path = os.path.join(cache_root, "b", "cpyth1234abcd", "p")
            _write(
                os.path.join(pkg, "lib", "cmake", "foo.cmake"),
                f'set(FOO_INCLUDE_DIR "{baked_path}/include")\n',
            )
            _copy_tree(_FakeDep("foo", pkg), output, True)

            aswf_deploy._rewrite_cache_paths(_FakeDep("foo", pkg), output, cache_root)

            with open(
                os.path.join(output, "lib", "cmake", "foo.cmake"), encoding="utf-8"
            ) as f:
                content = f.read()
            self.assertIn(f'"{output}/include"', content)
            self.assertNotIn(cache_root, content)

    def test_cmake_file_rewritten_no_b_prefix(self):
        # A package downloaded as a prebuilt binary from a remote lives at
        # <cache_root>/<hash>/p (PkgCache.create_pkg_layout()), unlike a
        # locally-built package's <cache_root>/b/<hash>/p
        # (PkgCache.create_build_pkg_layout()) -- both shapes must be caught.
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            baked_path = os.path.join(cache_root, "cpyth1234abcd", "p")
            _write(
                os.path.join(pkg, "lib", "cmake", "foo.cmake"),
                f'set(FOO_INCLUDE_DIR "{baked_path}/include")\n',
            )
            _copy_tree(_FakeDep("foo", pkg), output, True)

            aswf_deploy._rewrite_cache_paths(_FakeDep("foo", pkg), output, cache_root)

            with open(
                os.path.join(output, "lib", "cmake", "foo.cmake"), encoding="utf-8"
            ) as f:
                content = f.read()
            self.assertIn(f'"{output}/include"', content)
            self.assertNotIn(cache_root, content)

    def test_non_matching_file_left_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            original = 'set(BAR_INCLUDE_DIR "/some/unrelated/path")\n'
            _write(os.path.join(pkg, "lib", "cmake", "bar.cmake"), original)
            _copy_tree(_FakeDep("bar", pkg), output, True)

            aswf_deploy._rewrite_cache_paths(_FakeDep("bar", pkg), output, cache_root)

            with open(
                os.path.join(output, "lib", "cmake", "bar.cmake"), encoding="utf-8"
            ) as f:
                self.assertEqual(f.read(), original)


class TestCpythonFixup(unittest.TestCase):
    def _setup(self, tmp):
        cache_root = os.path.join(tmp, "conan_home", "d")
        pkg = os.path.join(tmp, "pkg")
        output = os.path.join(tmp, "output")
        return cache_root, pkg, output

    def test_sysconfigdata_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            baked_path = os.path.join(cache_root, "b", "cpyth1234abcd", "p")
            _write(
                os.path.join(pkg, "lib", "_sysconfigdata__linux_x86_64-linux-gnu.py"),
                f"build_time_vars = {{'prefix': '{baked_path}'}}\n",
            )
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(
                os.path.join(
                    output, "lib", "_sysconfigdata__linux_x86_64-linux-gnu.py"
                ),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertIn(output, content)
            self.assertNotIn(cache_root, content)

    def test_sysconfigdata_build_dir_variables_stripped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            build_dir = os.path.join(
                cache_root, "b", "cpytha62809013e2f2", "b", "build-release"
            )
            _write(
                os.path.join(pkg, "lib", "_sysconfigdata__linux_x86_64-linux-gnu.py"),
                "build_time_vars = {\n"
                f"    'abs_builddir': '{build_dir}',\n"
                "    'RUNSHARED': 'LD_LIBRARY_PATH="
                f"{build_dir}:/usr/local/lib',\n"
                "    'COVERAGE_INFO': "
                f"'{build_dir}/coverage.info',\n"
                "    'CONFIG_ARGS': \"'PKG_CONFIG_PATH="
                f"{build_dir}/conan/../../build-release/conan:/usr/local/lib/pkgconfig'\",\n"
                "}\n",
            )
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(
                os.path.join(
                    output, "lib", "_sysconfigdata__linux_x86_64-linux-gnu.py"
                ),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertNotIn(cache_root, content)
            self.assertIn("'abs_builddir': '',", content)
            self.assertIn("'RUNSHARED': 'LD_LIBRARY_PATH=:/usr/local/lib',", content)
            self.assertIn("'COVERAGE_INFO': '',", content)
            self.assertIn(
                "'CONFIG_ARGS': \"'PKG_CONFIG_PATH=:/usr/local/lib/pkgconfig'\",",
                content,
            )

    def test_config_makefile_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            baked_path = os.path.join(cache_root, "b", "cpyth1234abcd", "p")
            _write(
                os.path.join(
                    pkg, "lib", "python3.13", "config-3.13-x86_64-linux-gnu", "Makefile"
                ),
                f"prefix=\t{baked_path}\nLDSHARED=\tclang -shared\n",
            )
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(
                os.path.join(
                    output,
                    "lib",
                    "python3.13",
                    "config-3.13-x86_64-linux-gnu",
                    "Makefile",
                ),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertIn(output, content)
            self.assertNotIn(cache_root, content)

    def test_lib_shebang_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            shebang_target = os.path.join(cache_root, "b", "cpyth1234abcd", "p", "bin")
            _write(
                os.path.join(pkg, "lib", "somescript.py"),
                f"#!{shebang_target}/python3.11\nprint('hi')\n",
            )
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(
                os.path.join(output, "lib", "somescript.py"), encoding="utf-8"
            ) as f:
                lines = f.readlines()
            self.assertEqual(lines[0], "#!/usr/bin/env python3.11\n")

    def test_bin_python_shebang_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            shebang_target = os.path.join(cache_root, "b", "cpyth1234abcd", "p", "bin")
            _write(
                os.path.join(pkg, "bin", "pip3"),
                f"#!{shebang_target}/python3.11\nimport pip\n",
            )
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(os.path.join(output, "bin", "pip3"), encoding="utf-8") as f:
                lines = f.readlines()
            self.assertEqual(lines[0], "#!/usr/bin/env python3.11\n")

    def test_bin_non_python_shebang_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root, pkg, output = self._setup(tmp)
            original = "#!/bin/sh\necho hi\n"
            _write(os.path.join(pkg, "bin", "somescript"), original)
            dep = _FakeDep("cpython", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_cpython(dep, output, cache_root)

            with open(os.path.join(output, "bin", "somescript"), encoding="utf-8") as f:
                self.assertEqual(f.read(), original)


class TestPybind11Fixup(unittest.TestCase):
    def test_restores_stashed_unmodified_cmake(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            cmake_dir = os.path.join(pkg, "lib", "cmake", "pybind11")
            _write(
                os.path.join(cmake_dir, "pybind11Common.cmake"),
                "# conan-patched version\n",
            )
            _write(
                os.path.join(cmake_dir, "pybind11Common.cmake_NO_CONAN"),
                "# original upstream version\n",
            )
            dep = _FakeDep("pybind11", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_pybind11(dep, output, None)

            out_cmake_dir = os.path.join(output, "lib", "cmake", "pybind11")
            with open(
                os.path.join(out_cmake_dir, "pybind11Common.cmake"), encoding="utf-8"
            ) as f:
                self.assertEqual(f.read(), "# original upstream version\n")
            self.assertFalse(
                os.path.exists(
                    os.path.join(out_cmake_dir, "pybind11Common.cmake_NO_CONAN")
                )
            )


class TestQtFixup(unittest.TestCase):
    def test_qt_cmake_scripts_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            baked_cmake = os.path.join(
                cache_root, "b", "cmake1234abcd", "p", "bin", "cmake"
            )
            _write(
                os.path.join(pkg, "bin", "qt-cmake"),
                f'#!/bin/sh\nexec "{baked_cmake}" "$@"\n',
            )
            _write(
                os.path.join(pkg, "bin", "qt-cmake-create"),
                f'#!/bin/sh\nexec "{baked_cmake}" "$@"\n',
            )
            dep = _FakeDep("qt", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_qt(dep, output, cache_root)

            for name in ("qt-cmake", "qt-cmake-create"):
                with open(os.path.join(output, "bin", name), encoding="utf-8") as f:
                    content = f.read()
                self.assertIn(output, content)
                self.assertNotIn(cache_root, content)

    def test_mkspecs_pri_files_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            zlib_libdir = os.path.join(cache_root, "b", "zlibbe95832dc71d1", "p", "lib")
            zlib_incdir = os.path.join(
                cache_root, "b", "zlibbe95832dc71d1", "p", "include"
            )
            _write(
                os.path.join(pkg, "mkspecs", "qmodule.pri"),
                f"QMAKE_LIBDIR_ZLIB = {zlib_libdir}\n"
                f"QMAKE_INCDIR_ZLIB = {zlib_incdir}\n",
            )
            freetype_libdir = os.path.join(
                cache_root, "b", "freet30d7274aac070", "p", "lib"
            )
            _write(
                os.path.join(pkg, "mkspecs", "modules", "qt_lib_gui_private.pri"),
                f'QMAKE_LIBS_FREETYPE = -L"{freetype_libdir}" -lfreetype\n',
            )
            dep = _FakeDep("qt", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_qt(dep, output, cache_root)

            with open(
                os.path.join(output, "mkspecs", "qmodule.pri"), encoding="utf-8"
            ) as f:
                content = f.read()
            self.assertIn(f"QMAKE_LIBDIR_ZLIB = {output}/lib", content)
            self.assertIn(f"QMAKE_INCDIR_ZLIB = {output}/include", content)
            self.assertNotIn(cache_root, content)

            with open(
                os.path.join(output, "mkspecs", "modules", "qt_lib_gui_private.pri"),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertIn(f'-L"{output}/lib" -lfreetype', content)
            self.assertNotIn(cache_root, content)


class TestPkgconfigFixup(unittest.TestCase):
    def test_lensfun_pc_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            prefix = os.path.join(cache_root, "b", "lensfd42af8fbc2caa", "p")
            _write(
                os.path.join(pkg, "lib", "pkgconfig", "lensfun.pc"),
                f"prefix={prefix}\n"
                f"bindir={prefix}/bin\n"
                f"libdir={prefix}/lib\n"
                f"includedir={prefix}/include\n"
                f"datadir={prefix}/share\n"
                f"docdir={prefix}/share/doc/lensfun\n",
            )
            dep = _FakeDep("lensfun", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_pkgconfig(dep, output, cache_root)

            with open(
                os.path.join(output, "lib", "pkgconfig", "lensfun.pc"),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertIn(f"prefix={output}\n", content)
            self.assertIn(f"docdir={output}/share/doc/lensfun\n", content)
            self.assertNotIn(cache_root, content)

    def test_shiboken_pc_rewritten_with_cpython_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = os.path.join(tmp, "conan_home", "d")
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            cpython_prefix = os.path.join(cache_root, "b", "cpytha62809013e2f2", "p")
            install_dir = os.path.join(
                cache_root,
                "pysidbd7bd8f2345a5",
                "s",
                "pyside_src",
                "build",
                "qfp-py3.13-qt6.8.3-64bit-release",
                "install",
            )
            _write(
                os.path.join(pkg, "lib", "pkgconfig", "shiboken6.pc"),
                f"prefix={install_dir}\n"
                f"exec_prefix={install_dir}\n"
                f"libdir={install_dir}/lib\n"
                f"includedir={install_dir}/include/shiboken6\n"
                f"python_interpreter={cpython_prefix}/bin/python\n"
                f"python_include_dir={cpython_prefix}/include/python3.13\n"
                f"Cflags: -I{cpython_prefix}/include/python3.13 -I${{includedir}}/\n",
            )
            dep = _FakeDep("pyside", pkg)
            _copy_tree(dep, output, True)

            aswf_deploy._fixup_pyside(dep, output, cache_root)

            with open(
                os.path.join(output, "lib", "pkgconfig", "shiboken6.pc"),
                encoding="utf-8",
            ) as f:
                content = f.read()
            self.assertIn(f"prefix={output}\n", content)
            self.assertIn(f"exec_prefix={output}\n", content)
            self.assertIn(f"libdir={output}/lib\n", content)
            self.assertIn(f"includedir={output}/include/shiboken6\n", content)
            self.assertIn(f"python_interpreter={output}/bin/python\n", content)
            self.assertIn(
                f"Cflags: -I{output}/include/python3.13 -I${{includedir}}/\n", content
            )
            self.assertNotIn(cache_root, content)


class TestSymlinks(unittest.TestCase):
    def test_symlinks_preserved_when_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "lib", "libfoo.so.1"), "content")
            os.symlink(
                "libfoo.so.1",
                os.path.join(pkg, "lib", "libfoo.so"),
            )
            _copy_tree(_FakeDep("foo", pkg), output, True)
            link_path = os.path.join(output, "lib", "libfoo.so")
            self.assertTrue(os.path.islink(link_path))
            self.assertEqual(os.readlink(link_path), "libfoo.so.1")

    def test_symlinks_followed_when_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "pkg")
            output = os.path.join(tmp, "output")
            _write(os.path.join(pkg, "lib", "libfoo.so.1"), "content")
            os.symlink(
                "libfoo.so.1",
                os.path.join(pkg, "lib", "libfoo.so"),
            )
            _copy_tree(_FakeDep("foo", pkg), output, False)
            link_path = os.path.join(output, "lib", "libfoo.so")
            self.assertFalse(os.path.islink(link_path))


class TestSymlinkCollisions(unittest.TestCase):
    def test_same_target_is_warning_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            # Only pkg_a ships the real libfoo.so.1; pkg_b only ships the
            # symlink (pointing at the same relative name pkg_a resolved),
            # so the only collision exercised here is the symlink itself,
            # not also a duplicate-regular-file collision underneath it.
            _write(os.path.join(pkg_a, "lib", "libfoo.so.1"), "content")
            os.symlink("libfoo.so.1", os.path.join(pkg_a, "lib", "libfoo.so"))
            os.makedirs(os.path.join(pkg_b, "lib"))
            os.symlink("libfoo.so.1", os.path.join(pkg_b, "lib", "libfoo.so"))

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(_FakeDep("a", pkg_a), output, True, report)
            aswf_deploy._copy_package_tree(_FakeDep("b", pkg_b), output, True, report)

            link_path = os.path.join(output, "lib", "libfoo.so")
            self.assertEqual(os.readlink(link_path), "libfoo.so.1")
            self.assertEqual(len(report.warnings), 1)
            self.assertEqual(len(report.errors), 0)
            self.assertIn("libfoo.so", report.warnings[0])

    def test_different_target_is_error_and_first_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            _write(os.path.join(pkg_a, "lib", "libclang.so.21"), "clang21")
            os.symlink("libclang.so.21", os.path.join(pkg_a, "lib", "libclang.so"))
            _write(os.path.join(pkg_b, "lib", "libclang.so.22"), "clang22")
            os.symlink("libclang.so.22", os.path.join(pkg_b, "lib", "libclang.so"))

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(
                _FakeDep("clang", pkg_a), output, True, report
            )
            aswf_deploy._copy_package_tree(
                _FakeDep("clang", pkg_b), output, True, report
            )

            link_path = os.path.join(output, "lib", "libclang.so")
            self.assertEqual(os.readlink(link_path), "libclang.so.21")
            self.assertEqual(len(report.warnings), 0)
            self.assertEqual(len(report.errors), 1)
            self.assertIn("libclang.so.21", report.errors[0])
            self.assertIn("libclang.so.22", report.errors[0])

    def test_destination_exists_as_plain_file_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            _write(os.path.join(pkg_a, "lib", "libbar.so"), "a real file")
            _write(os.path.join(pkg_b, "lib", "libbar.so.1"), "content")
            os.symlink("libbar.so.1", os.path.join(pkg_b, "lib", "libbar.so"))

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(_FakeDep("a", pkg_a), output, True, report)
            aswf_deploy._copy_package_tree(_FakeDep("b", pkg_b), output, True, report)

            link_path = os.path.join(output, "lib", "libbar.so")
            self.assertFalse(os.path.islink(link_path))
            self.assertEqual(len(report.errors), 1)
            self.assertIn("file", report.errors[0])

    def test_destination_exists_as_directory_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            os.makedirs(os.path.join(pkg_a, "lib", "libbaz.so"))
            _write(os.path.join(pkg_b, "lib", "libbaz.so.1"), "content")
            os.symlink("libbaz.so.1", os.path.join(pkg_b, "lib", "libbaz.so"))

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(_FakeDep("a", pkg_a), output, True, report)
            aswf_deploy._copy_package_tree(_FakeDep("b", pkg_b), output, True, report)

            self.assertTrue(os.path.isdir(os.path.join(output, "lib", "libbaz.so")))
            self.assertEqual(len(report.errors), 1)
            self.assertIn("directory", report.errors[0])

    def test_unrelated_srcname_not_a_symlink_is_not_our_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "regular_file")
            dst = os.path.join(tmp, "dest")
            _write(src, "content")
            _write(dst, "other content")
            report = aswf_deploy._DeployReport()
            handled = aswf_deploy._classify_symlink_collision(
                _FakeDep("foo", tmp), src, dst, report
            )
            self.assertFalse(handled)
            self.assertEqual(report.warnings, [])
            self.assertEqual(report.errors, [])


class TestFileCollisions(unittest.TestCase):
    def test_identical_content_is_warning_and_original_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            content = b"x" * (aswf_deploy._MD5_CHUNK_SIZE + 1)
            _write(os.path.join(pkg_a, "lib", "libcommon.so"), content)
            _write(os.path.join(pkg_b, "lib", "libcommon.so"), content)

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(_FakeDep("a", pkg_a), output, True, report)
            aswf_deploy._copy_package_tree(_FakeDep("b", pkg_b), output, True, report)

            with open(os.path.join(output, "lib", "libcommon.so"), "rb") as f:
                self.assertEqual(f.read(), content)
            self.assertEqual(len(report.warnings), 1)
            self.assertEqual(len(report.errors), 0)

    def test_different_content_is_error_and_first_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg_a = os.path.join(tmp, "pkg_a")
            pkg_b = os.path.join(tmp, "pkg_b")
            _write(os.path.join(pkg_a, "bin", "mytool"), "AAAA")
            _write(os.path.join(pkg_b, "bin", "mytool"), "BBBB")

            report = aswf_deploy._DeployReport()
            aswf_deploy._copy_package_tree(_FakeDep("a", pkg_a), output, True, report)
            aswf_deploy._copy_package_tree(_FakeDep("b", pkg_b), output, True, report)

            with open(os.path.join(output, "bin", "mytool"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "AAAA")
            self.assertEqual(len(report.warnings), 0)
            self.assertEqual(len(report.errors), 1)

    def test_no_collision_copies_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "output")
            pkg = os.path.join(tmp, "pkg")
            _write(os.path.join(pkg, "include", "a.h"), "a")
            report = _copy_tree(_FakeDep("a", pkg), output, True)
            with open(os.path.join(output, "include", "a.h"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "a")
            self.assertEqual(report.warnings, [])
            self.assertEqual(report.errors, [])

    def test_md5_helper_chunked_on_large_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big_file")
            content = os.urandom(aswf_deploy._MD5_CHUNK_SIZE + 1)
            _write(path, content)
            self.assertEqual(
                aswf_deploy._md5_of_file(path), hashlib.md5(content).hexdigest()
            )


class TestDeployReport(unittest.TestCase):
    def test_print_report_includes_warnings_and_errors(self):
        report = aswf_deploy._DeployReport()
        report.add_warning("zlib", "/usr/local/lib/libz.so", "harmless duplicate")
        report.add_error("clang", "/usr/local/lib/libclang.so", "version mismatch")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.print_report()
        output = buf.getvalue()
        self.assertIn("harmless duplicate", output)
        self.assertIn("version mismatch", output)

    def test_raise_if_errors_raises_when_errors_present(self):
        report = aswf_deploy._DeployReport()
        report.add_error("clang", "/usr/local/lib/libclang.so", "version mismatch")
        with self.assertRaises(RuntimeError):
            report.raise_if_errors()

    def test_raise_if_errors_does_not_raise_for_warnings_only(self):
        report = aswf_deploy._DeployReport()
        report.add_warning("zlib", "/usr/local/lib/libz.so", "harmless duplicate")
        report.raise_if_errors()  # must not raise


class TestComponentProperties(unittest.TestCase):
    def test_package_id_only_without_metadata(self):
        dep = _FakeDep("zlib", None)
        self.assertEqual(
            aswf_deploy._component_properties(dep), {"aswf:package_id": "zlib-pid"}
        )

    def test_metadata_folder_without_provenance_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = _FakeDep("zlib", None, package_metadata_folder=tmp)
            self.assertEqual(
                aswf_deploy._component_properties(dep),
                {"aswf:package_id": "zlib-pid"},
            )

    def test_system_wrapper_is_tagged(self):
        dep = _FakeDep("opengl", None, version="system")
        self.assertEqual(
            aswf_deploy._component_properties(dep, system=True),
            {"aswf:package_id": "opengl-pid", "aswf:provided_by": "system"},
        )

    def test_reads_clean_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "aswf_provenance.json"),
                json.dumps({"git_sha": "abc123", "git_dirty": False}),
            )
            dep = _FakeDep("zlib", None, package_metadata_folder=tmp)
            self.assertEqual(
                aswf_deploy._component_properties(dep),
                {
                    "aswf:package_id": "zlib-pid",
                    "aswf:git_sha": "abc123",
                    "aswf:git_dirty": "false",
                },
            )

    def test_reads_dirty_provenance_with_diff_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                os.path.join(tmp, "aswf_provenance.json"),
                json.dumps(
                    {"git_sha": "abc123", "git_dirty": True, "git_diff_hash": "f00d"}
                ),
            )
            dep = _FakeDep("zlib", None, package_metadata_folder=tmp)
            props = aswf_deploy._component_properties(dep)
            self.assertEqual(props["aswf:git_dirty"], "true")
            self.assertEqual(props["aswf:git_diff_hash"], "f00d")


class TestIsSystemWrapper(unittest.TestCase):
    def test_empty_package_with_system_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = _FakeDep("opengl", tmp, version="system")
            self.assertTrue(aswf_deploy._is_system_wrapper(dep))

    def test_version_string_alone_does_not_decide(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "include"))
            dep = _FakeDep("opengl", tmp, version="system")
            self.assertFalse(aswf_deploy._is_system_wrapper(dep))

    def test_empty_package_with_real_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "conaninfo.txt"), "stuff")
            dep = _FakeDep("openssl", tmp, version="3.5.8")
            self.assertTrue(aswf_deploy._is_system_wrapper(dep))

    def test_normal_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "lib"))
            self.assertFalse(aswf_deploy._is_system_wrapper(_FakeDep("zlib", tmp)))

    def test_skipped_binary_is_not_a_wrapper(self):
        # package_folder is None when Conan skips a binary nothing needs,
        # e.g. a static library already linked into its consumers.
        self.assertFalse(aswf_deploy._is_system_wrapper(_FakeDep("zlib", None)))


class TestSpdxLicenses(unittest.TestCase):
    def test_expression_name_becomes_expression(self):
        self.assertEqual(
            aswf_deploy._spdx_licenses(
                [{"license": {"name": "Apache-2.0 WITH LLVM-exception"}}]
            ),
            [{"expression": "Apache-2.0 WITH LLVM-exception"}],
        )

    def test_ids_and_free_text_are_unchanged(self):
        for licenses in (
            [{"license": {"id": "BSD-3-Clause"}}],
            # Prose, not an SPDX expression (operators must be uppercase here).
            [{"license": {"name": "Python-2.0 and numpy (BSD-3-Clause license)"}}],
            # An expression can't sit alongside other license entries.
            [{"license": {"id": "MIT"}}, {"license": {"name": "A OR B"}}],
        ):
            self.assertEqual(aswf_deploy._spdx_licenses(licenses), licenses)

    def test_applied_to_kept_components(self):
        sbom = _sbom(["clang"], {"root": ["clang"]})
        sbom["components"][0]["licenses"] = [
            {"license": {"name": "Apache-2.0 WITH LLVM-exception"}}
        ]
        result = aswf_deploy._filter_and_annotate_sbom(sbom, {("clang", "1.0"): {}})
        self.assertEqual(
            result["components"][0]["licenses"],
            [{"expression": "Apache-2.0 WITH LLVM-exception"}],
        )


def _sbom(components, depends_on):
    """A minimal Conan-shaped CycloneDX SBOM: `components` is a list of
    names (version 1.0, bom-ref == name), `depends_on` maps a ref (or
    "root") to the refs it depends on."""
    return {
        "bomFormat": "CycloneDX",
        "metadata": {"component": {"bom-ref": "root", "name": "ci-test"}},
        "components": [{"bom-ref": n, "name": n, "version": "1.0"} for n in components],
        "dependencies": [
            {"ref": ref, **({"dependsOn": deps} if deps else {})}
            for ref, deps in depends_on.items()
        ],
    }


class TestFilterAndAnnotateSbom(unittest.TestCase):
    def test_keeps_listed_components_and_adds_sorted_properties(self):
        sbom = _sbom(["zlib", "cmake"], {"root": ["zlib", "cmake"]})
        result = aswf_deploy._filter_and_annotate_sbom(
            sbom, {("zlib", "1.0"): {"b": "2", "a": "1"}}
        )
        self.assertEqual(
            result["components"],
            [
                {
                    "bom-ref": "zlib",
                    "name": "zlib",
                    "version": "1.0",
                    "properties": [
                        {"name": "a", "value": "1"},
                        {"name": "b", "value": "2"},
                    ],
                }
            ],
        )
        self.assertEqual(
            result["dependencies"], [{"ref": "root", "dependsOn": ["zlib"]}]
        )
        self.assertEqual(result["metadata"], sbom["metadata"])
        self.assertEqual(result["bomFormat"], "CycloneDX")

    def test_version_must_match(self):
        sbom = _sbom(["zlib"], {"root": ["zlib"]})
        result = aswf_deploy._filter_and_annotate_sbom(
            sbom, {("zlib", "2.0"): {"a": "1"}}
        )
        self.assertNotIn("components", result)
        self.assertEqual(result["dependencies"], [{"ref": "root"}])

    def test_rewires_dependencies_past_dropped_components(self):
        # openexr -> imath (dropped) -> zlib; openexr must end up on zlib.
        sbom = _sbom(
            ["openexr", "imath", "zlib"],
            {
                "root": ["openexr"],
                "openexr": ["imath"],
                "imath": ["zlib"],
                "zlib": [],
            },
        )
        props = {("openexr", "1.0"): {"x": "1"}, ("zlib", "1.0"): {"x": "1"}}
        result = aswf_deploy._filter_and_annotate_sbom(sbom, props)
        self.assertEqual(
            result["dependencies"],
            [
                {"ref": "root", "dependsOn": ["openexr"]},
                {"ref": "openexr", "dependsOn": ["zlib"]},
                {"ref": "zlib"},
            ],
        )

    def test_duplicate_bom_refs_are_collapsed(self):
        # Conan emits one component and one dependencies entry per graph
        # node, and two nodes of the same recipe revision share a bom-ref.
        sbom = _sbom(
            ["cmake", "openssl", "cmake"],
            {"root": ["cmake"], "cmake": ["openssl"], "openssl": []},
        )
        sbom["dependencies"].append({"ref": "cmake", "dependsOn": ["zlib"]})
        sbom["components"].append({"bom-ref": "zlib", "name": "zlib", "version": "1.0"})
        props = {(n, "1.0"): {"x": "1"} for n in ("cmake", "openssl", "zlib")}
        result = aswf_deploy._filter_and_annotate_sbom(sbom, props)
        self.assertEqual(
            [c["bom-ref"] for c in result["components"]], ["cmake", "openssl", "zlib"]
        )
        self.assertEqual(
            [e for e in result["dependencies"] if e["ref"] == "cmake"],
            [{"ref": "cmake", "dependsOn": ["openssl", "zlib"]}],
        )

    def test_rewiring_handles_diamonds_without_duplicates(self):
        sbom = _sbom(
            ["app", "a", "b", "zlib"],
            {"root": ["app"], "app": ["a", "b"], "a": ["zlib"], "b": ["zlib"]},
        )
        props = {("app", "1.0"): {"x": "1"}, ("zlib", "1.0"): {"x": "1"}}
        result = aswf_deploy._filter_and_annotate_sbom(sbom, props)
        app = next(e for e in result["dependencies"] if e["ref"] == "app")
        self.assertEqual(app["dependsOn"], ["zlib"])


class TestSetSbomRoot(unittest.TestCase):
    def test_replaces_random_root_with_stable_image_ref(self):
        sbom = _sbom(["zlib"], {"root": ["zlib"], "zlib": []})
        result = aswf_deploy._set_sbom_root(sbom, "ci-base", "2027.10")
        self.assertEqual(
            result["metadata"]["component"],
            {
                "bom-ref": "ci-base@2027.10",
                "name": "ci-base",
                "version": "2027.10",
                "type": "container",
            },
        )
        self.assertEqual(
            result["dependencies"],
            [{"ref": "ci-base@2027.10", "dependsOn": ["zlib"]}, {"ref": "zlib"}],
        )

    def test_without_version(self):
        sbom = _sbom([], {"root": []})
        result = aswf_deploy._set_sbom_root(sbom, "ci-base", None)
        self.assertEqual(result["metadata"]["component"]["bom-ref"], "ci-base")
        self.assertNotIn("version", result["metadata"]["component"])


def _layer(name, timestamp, components, depends_on):
    sbom = _sbom(components, {"root": [], **depends_on})
    sbom["metadata"]["timestamp"] = timestamp
    return aswf_deploy._set_sbom_root(sbom, name, "1.0")


class TestMergeSboms(unittest.TestCase):
    def setUp(self):
        self.common = _layer(
            "ci-common",
            "2026-01-01T00:00:00Z",
            ["cmake", "openssl"],
            {"root": ["cmake"], "cmake": ["openssl"], "openssl": []},
        )
        self.base = _layer(
            "ci-base",
            "2026-01-02T00:00:00Z",
            ["openssl", "zlib"],
            {"root": ["zlib", "openssl"], "zlib": [], "openssl": []},
        )

    def test_merges_layers_under_image_root(self):
        merged = aswf_deploy._merge_sboms(
            [self.common, self.base], self.base, "ci-base", "1.0"
        )
        self.assertEqual(
            [c["bom-ref"] for c in merged["components"]], ["cmake", "openssl", "zlib"]
        )
        self.assertEqual(
            merged["dependencies"],
            [
                {"ref": "ci-base@1.0", "dependsOn": ["cmake", "zlib", "openssl"]},
                {"ref": "cmake", "dependsOn": ["openssl"]},
                {"ref": "openssl"},
                {"ref": "zlib"},
            ],
        )
        root = merged["metadata"]["component"]
        self.assertEqual(root["bom-ref"], "ci-base@1.0")
        self.assertEqual(
            root["properties"], [{"name": "aswf:layers", "value": "ci-common,ci-base"}]
        )
        self.assertTrue(merged["serialNumber"].startswith("urn:uuid:"))
        self.assertEqual(merged["metadata"]["timestamp"], "2026-01-02T00:00:00Z")

    def test_different_builds_of_a_package_are_kept_and_reported(self):
        self.base["components"][0]["bom-ref"] = "openssl-rev2"
        with mock.patch("builtins.print") as mock_print:
            merged = aswf_deploy._merge_sboms(
                [self.common, self.base], self.base, "ci-base", "1.0"
            )
        refs = [c["bom-ref"] for c in merged["components"]]
        self.assertIn("openssl", refs)
        self.assertIn("openssl-rev2", refs)
        self.assertIn("different builds of openssl", mock_print.call_args[0][0])


class TestDeployEndToEnd(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            aswf_deploy, "cyclonedx_1_6", side_effect=_fake_cyclonedx_1_6
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_full_deploy_excludes_named_package_and_deploys_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_zlib = os.path.join(tmp, "pkg_zlib")
                pkg_openexr = os.path.join(tmp, "pkg_openexr")
                _write(os.path.join(pkg_zlib, "include", "zlib.h"), "zlib")
                _write(os.path.join(pkg_openexr, "include", "openexr.h"), "openexr")

                conanfile = _FakeConanFile(
                    conf_values={"user.aswf:exclude_package": "openexr"},
                    deps=[
                        _direct_host_dep(_FakeDep("zlib", pkg_zlib)),
                        _direct_host_dep(_FakeDep("openexr", pkg_openexr)),
                    ],
                )
                graph = _FakeGraph(conanfile)

                aswf_deploy.deploy(graph, output)

                self.assertTrue(
                    os.path.isfile(os.path.join(output, "include", "zlib.h"))
                )
                self.assertFalse(
                    os.path.isfile(os.path.join(output, "include", "openexr.h"))
                )
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_skips_transitive_build_context_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_cmake = os.path.join(tmp, "pkg_cmake")
                _write(os.path.join(pkg_cmake, "bin", "cmake"), "binary")

                conanfile = _FakeConanFile(
                    deps=[
                        (
                            _FakeRequirement("cmake", build=True, direct=False),
                            _FakeDep("cmake", pkg_cmake),
                        ),
                    ],
                )
                graph = _FakeGraph(conanfile)
                aswf_deploy.deploy(graph, output)
                self.assertFalse(os.path.exists(os.path.join(output, "bin", "cmake")))
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_keeps_direct_clang_skips_transitive_clang(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_direct = os.path.join(tmp, "pkg_clang_direct")
                pkg_transitive = os.path.join(tmp, "pkg_clang_transitive")
                _write(
                    os.path.join(pkg_direct, "include", "clang-c", "Index.h"), "direct"
                )
                _write(
                    os.path.join(pkg_transitive, "include", "clang-c", "Index.h"),
                    "transitive",
                )

                conanfile = _FakeConanFile(
                    deps=[
                        (
                            _FakeRequirement("clang", build=True, direct=True),
                            _FakeDep("clang", pkg_direct),
                        ),
                        (
                            _FakeRequirement("clang", build=False, direct=False),
                            _FakeDep("clang", pkg_transitive),
                        ),
                    ],
                )
                graph = _FakeGraph(conanfile)
                aswf_deploy.deploy(graph, output)

                with open(
                    os.path.join(output, "include", "clang-c", "Index.h"),
                    encoding="utf-8",
                ) as f:
                    self.assertEqual(f.read(), "direct")
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_raises_when_collision_error_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_a = os.path.join(tmp, "pkg_a")
                pkg_b = os.path.join(tmp, "pkg_b")
                _write(os.path.join(pkg_a, "bin", "mytool"), "AAAA")
                _write(os.path.join(pkg_b, "bin", "mytool"), "BBBB")

                conanfile = _FakeConanFile(
                    deps=[
                        _direct_host_dep(_FakeDep("a", pkg_a)),
                        _direct_host_dep(_FakeDep("b", pkg_b)),
                    ],
                )
                graph = _FakeGraph(conanfile)
                with self.assertRaises(RuntimeError):
                    aswf_deploy.deploy(graph, output)
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_does_not_raise_for_warnings_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_a = os.path.join(tmp, "pkg_a")
                pkg_b = os.path.join(tmp, "pkg_b")
                _write(os.path.join(pkg_a, "bin", "mytool"), "AAAA")
                _write(os.path.join(pkg_b, "bin", "mytool"), "AAAA")

                conanfile = _FakeConanFile(
                    deps=[
                        _direct_host_dep(_FakeDep("a", pkg_a)),
                        _direct_host_dep(_FakeDep("b", pkg_b)),
                    ],
                )
                graph = _FakeGraph(conanfile)
                aswf_deploy.deploy(graph, output)  # must not raise
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_writes_filtered_annotated_sbom(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_zlib = os.path.join(tmp, "pkg_zlib")
                pkg_openexr = os.path.join(tmp, "pkg_openexr")
                pkg_cmake = os.path.join(tmp, "pkg_cmake")
                pkg_opengl = os.path.join(tmp, "pkg_opengl")
                meta_zlib = os.path.join(tmp, "meta_zlib")
                _write(os.path.join(pkg_zlib, "include", "zlib.h"), "zlib")
                _write(os.path.join(pkg_openexr, "include", "openexr.h"), "exr")
                _write(os.path.join(pkg_cmake, "bin", "cmake"), "binary")
                os.makedirs(pkg_opengl)
                pkg_openssl = os.path.join(tmp, "pkg_openssl")
                _write(os.path.join(pkg_openssl, "conaninfo.txt"), "wrapper")
                _write(
                    os.path.join(meta_zlib, "aswf_provenance.json"),
                    json.dumps({"git_sha": "abc123", "git_dirty": False}),
                )

                conanfile = _FakeConanFile(
                    conf_values={
                        "user.aswf:exclude_package": "openexr",
                        "user.aswf:sbom_name": "ci-test",
                    },
                    deps=[
                        _direct_host_dep(
                            _FakeDep(
                                "zlib", pkg_zlib, package_metadata_folder=meta_zlib
                            )
                        ),
                        _direct_host_dep(_FakeDep("openexr", pkg_openexr)),
                        _direct_host_dep(
                            _FakeDep("opengl", pkg_opengl, version="system")
                        ),
                        _direct_host_dep(
                            _FakeDep("openssl", pkg_openssl, version="3.5.8")
                        ),
                        (
                            _FakeRequirement("cmake", build=True, direct=False),
                            _FakeDep("cmake", pkg_cmake),
                        ),
                    ],
                )
                aswf_deploy.deploy(_FakeGraph(conanfile), output)

                with open(
                    os.path.join(output, "aswf", "ci-test.cdx.json"), encoding="utf-8"
                ) as f:
                    sbom = json.load(f)
                components = {c["name"]: c for c in sbom["components"]}
                # Excluded and out-of-scope packages are left out, system
                # wrappers (not copied, but provided by the image) are kept.
                self.assertEqual(sorted(components), ["opengl", "openssl", "zlib"])
                self.assertEqual(sbom["metadata"]["component"]["name"], "ci-test")
                self.assertIn(
                    {"name": "aswf:git_sha", "value": "abc123"},
                    components["zlib"]["properties"],
                )
                self.assertEqual(
                    components["opengl"]["properties"],
                    [
                        {"name": "aswf:package_id", "value": "opengl-pid"},
                        {"name": "aswf:provided_by", "value": "system"},
                    ],
                )
                self.assertIn(
                    {"name": "aswf:provided_by", "value": "system"},
                    components["openssl"]["properties"],
                )
                self.assertNotIn(
                    "aswf:provided_by",
                    [p["name"] for p in components["zlib"]["properties"]],
                )
            finally:
                del os.environ["CONAN_HOME"]

    def test_layered_deploys_merge_into_image_sbom(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                pkg_cmake = os.path.join(tmp, "pkg_cmake")
                pkg_zlib = os.path.join(tmp, "pkg_zlib")
                _write(os.path.join(pkg_cmake, "bin", "cmake"), "binary")
                _write(os.path.join(pkg_zlib, "include", "zlib.h"), "zlib")
                for image, dep in (
                    ("ci-common", _FakeDep("cmake", pkg_cmake)),
                    ("ci-base", _FakeDep("zlib", pkg_zlib)),
                ):
                    conanfile = _FakeConanFile(
                        conf_values={"user.aswf:sbom_name": f"{image}/2027.10"},
                        deps=[_direct_host_dep(dep)],
                    )
                    aswf_deploy.deploy(_FakeGraph(conanfile), output)

                sbom_dir = os.path.join(output, "aswf")
                self.assertEqual(
                    sorted(os.listdir(sbom_dir)),
                    ["ci-base.cdx.json", "ci-common.cdx.json", "sbom.cdx.json"],
                )
                with open(
                    os.path.join(sbom_dir, "sbom.cdx.json"), encoding="utf-8"
                ) as f:
                    merged = json.load(f)
                self.assertEqual(
                    sorted(c["name"] for c in merged["components"]), ["cmake", "zlib"]
                )
                self.assertEqual(
                    merged["metadata"]["component"]["bom-ref"], "ci-base@2027.10"
                )
                self.assertEqual(
                    sorted(merged["dependencies"][0]["dependsOn"]),
                    ["pkg:conan/cmake@1.0", "pkg:conan/zlib@1.0"],
                )
            finally:
                del os.environ["CONAN_HOME"]

    def test_deploy_uses_default_sbom_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            conan_home = os.path.join(tmp, "conan_home")
            os.makedirs(conan_home)
            os.environ["CONAN_HOME"] = conan_home
            try:
                output = os.path.join(tmp, "output")
                aswf_deploy.deploy(_FakeGraph(_FakeConanFile()), output)
                self.assertTrue(
                    os.path.isfile(os.path.join(output, "aswf", "conan-sbom.cdx.json"))
                )
            finally:
                del os.environ["CONAN_HOME"]


if __name__ == "__main__":
    unittest.main()
