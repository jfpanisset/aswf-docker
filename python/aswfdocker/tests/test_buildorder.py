# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# pylint: disable=protected-access
"""
Tests for buildorder.py
"""

import json
import os
import shlex
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from aswfdocker import buildorder

# TestNameFromRef/TestRecipeFolder/TestExportCommand use real aswf-docker
# package names (osl, qt, zlib) because they exercise this project's real
# on-disk recipe-folder conventions (including qt's config.yml
# multi-version layout). Even there, buildorder.py never inspects a version
# number, so none of the version strings below need to track the real
# values in python/aswfdocker/data/versions.yaml or
# packages/conan/settings/profiles/.
#
# TestParseBuildOrderJson and TestComputeBuildOrder instead use synthetic
# package names (pkga, pkgb, ...): their fixtures encode entirely
# fabricated dependency edges, used only to exercise
# parse_build_order_json()'s graph-handling logic (levels,
# downstream_closure). buildorder.py never looks at a recipe's actual
# requires() -- it only trusts whatever `conan graph build-order` reports --
# so real package names there would wrongly suggest these made-up edges
# mean something about, say, osl's or openexr's real dependencies, which
# they don't and needn't.
TEST_PACKAGE_ORG = "aswf"
TEST_CHANNEL = "vfx2027"
OTHER_TEST_CHANNEL = "other_test_channel"
ZLIB_TEST_VERSION = "1.3.1"
OSL_TEST_VERSION = "1.14.4.0"
QT_TEST_VERSION = "6.8.1"
PKGA_TEST_VERSION = "1.0.0"
PKGB_TEST_VERSION = "2.0.0"
PKGC_TEST_VERSION = "3.0.0"
PKGD_TEST_VERSION = "4.0.0"
PKGE_TEST_VERSION = "5.0.0"


def _ref(name, version, org=TEST_PACKAGE_ORG, channel=TEST_CHANNEL):
    return f"{name}/{version}@{org}/{channel}"


def _ref_entry(ref, depends=(), binary=None):
    packages = [[{"package_id": "x", "binary": binary}]] if binary else []
    return {"ref": ref, "depends": list(depends), "packages": packages}


def _five_package_order(need_build=()):
    """pkga/pkgb (no deps) -> pkgc (depends on both) -> pkgd/pkge (both
    depend on pkgc), with `need_build` marked "Build" and the rest "Cache".
    Purely synthetic -- see module docstring comment above."""

    def entry(name, version, depends=()):
        return _ref_entry(
            _ref(name, version),
            depends=depends,
            binary="Build" if name in need_build else "Cache",
        )

    pkgc_ref = _ref("pkgc", PKGC_TEST_VERSION)
    return buildorder.parse_build_order_json(
        {
            "order": [
                [entry("pkga", PKGA_TEST_VERSION), entry("pkgb", PKGB_TEST_VERSION)],
                [
                    entry(
                        "pkgc",
                        PKGC_TEST_VERSION,
                        [
                            _ref("pkga", PKGA_TEST_VERSION),
                            _ref("pkgb", PKGB_TEST_VERSION),
                        ],
                    )
                ],
                [
                    entry("pkgd", PKGD_TEST_VERSION, [pkgc_ref]),
                    entry("pkge", PKGE_TEST_VERSION, [pkgc_ref]),
                ],
            ]
        }
    )


class TestNameFromRef(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(
            buildorder._name_from_ref(_ref("osl", OSL_TEST_VERSION)), "osl"
        )

    def test_no_user_channel(self):
        # Conan's own ref grammar is name/version[@user/channel] -- the
        # @user/channel suffix is optional in general, even though every ref
        # this project's own pipeline actually resolves will have one, since
        # the vfx profiles' replace_requires/replace_tool_requires
        # always redirect to a fully-qualified aswf/<channel> ref. This case
        # exercises that general grammar, not a shape our own pipeline will
        # ever actually produce.
        self.assertEqual(buildorder._name_from_ref(f"zlib/{ZLIB_TEST_VERSION}"), "zlib")


class TestParseBuildOrderJson(unittest.TestCase):
    """Fixture shape: pkga/pkgb (no deps) -> pkgc (depends on both) ->
    pkgd/pkge (both depend on pkgc). Purely synthetic -- see module
    docstring comment above."""

    def setUp(self):
        self.data = {
            "order_by": "recipe",
            "reduced": True,
            "order": [
                [
                    _ref_entry(_ref("pkga", PKGA_TEST_VERSION)),
                    _ref_entry(_ref("pkgb", PKGB_TEST_VERSION)),
                ],
                [
                    _ref_entry(
                        _ref("pkgc", PKGC_TEST_VERSION),
                        depends=[
                            _ref("pkga", PKGA_TEST_VERSION),
                            _ref("pkgb", PKGB_TEST_VERSION),
                        ],
                    ),
                ],
                [
                    _ref_entry(
                        _ref("pkgd", PKGD_TEST_VERSION),
                        depends=[_ref("pkgc", PKGC_TEST_VERSION)],
                    ),
                    _ref_entry(
                        _ref("pkge", PKGE_TEST_VERSION),
                        depends=[_ref("pkgc", PKGC_TEST_VERSION)],
                    ),
                ],
            ],
        }

    def test_levels(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(
            order.levels,
            [
                ["pkga", "pkgb"],
                ["pkgc"],
                ["pkgd", "pkge"],
            ],
        )

    def test_flat_order(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(order.flat_order(), ["pkga", "pkgb", "pkgc", "pkgd", "pkge"])

    def test_downstream_closure_from_root(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(order.downstream_closure(["pkga"]), {"pkgc", "pkgd", "pkge"})

    def test_downstream_closure_from_middle(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(order.downstream_closure(["pkgc"]), {"pkgd", "pkge"})

    def test_downstream_closure_from_leaf_is_empty(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(order.downstream_closure(["pkgd"]), set())

    def test_downstream_closure_multiple_seeds(self):
        order = buildorder.parse_build_order_json(self.data)
        self.assertEqual(
            order.downstream_closure(["pkga", "pkgb"]),
            {"pkgc", "pkgd", "pkge"},
        )

    def test_missing_order_key_raises(self):
        with self.assertRaises(buildorder.BuildOrderError):
            buildorder.parse_build_order_json({"order_by": "recipe"})

    def test_none_raises(self):
        with self.assertRaises(buildorder.BuildOrderError):
            buildorder.parse_build_order_json(None)

    def test_need_build_from_binary_status(self):
        order = _five_package_order(need_build={"pkgb", "pkgd"})
        self.assertEqual(order.need_build, {"pkgb", "pkgd"})
        self.assertEqual(order.refs["pkgb"], {_ref("pkgb", PKGB_TEST_VERSION)})

    def test_upstream_closure(self):
        order = _five_package_order()
        self.assertEqual(order.upstream_closure(["pkgd"]), {"pkgc", "pkga", "pkgb"})
        self.assertEqual(order.upstream_closure(["pkga"]), set())

    def test_unbuildable_packages_raise(self):
        # graph build-order records these instead of failing
        for binary in ("Invalid", "Missing"):
            with self.assertRaises(buildorder.BuildOrderError) as ctx:
                buildorder.parse_build_order_json(
                    {
                        "order": [
                            [_ref_entry(_ref("pkga", PKGA_TEST_VERSION), binary=binary)]
                        ]
                    }
                )
            self.assertIn(
                f"{_ref('pkga', PKGA_TEST_VERSION)} ({binary})", str(ctx.exception)
            )


class TestPlanBuild(unittest.TestCase):
    """Uses _five_package_order(): pkga/pkgb -> pkgc -> pkgd/pkge."""

    ALL = ["pkga", "pkgb", "pkgc", "pkgd", "pkge"]

    def test_whole_group_builds_only_what_needs_building_in_order(self):
        order = _five_package_order(need_build={"pkgd", "pkgb", "pkgc"})
        plan = buildorder.plan_build(order, self.ALL)
        self.assertEqual(plan.to_build, ["pkgb", "pkgc", "pkgd"])
        self.assertEqual(plan.up_to_date, ["pkga", "pkge"])

    def test_force_rebuilds_every_selected_member(self):
        order = _five_package_order()
        plan = buildorder.plan_build(order, self.ALL, force=True)
        self.assertEqual(plan.to_build, self.ALL)
        self.assertEqual(plan.up_to_date, [])

    def test_targets_build_only_named_packages(self):
        order = _five_package_order(need_build={"pkgd", "pkge"})
        plan = buildorder.plan_build(order, self.ALL, targets=["pkge"])
        self.assertEqual(plan.to_build, ["pkge"])

    def test_target_to_includes_upstream(self):
        order = _five_package_order(need_build={"pkga", "pkgc", "pkgd", "pkge"})
        plan = buildorder.plan_build(order, self.ALL, targets_to=["pkgd"])
        self.assertEqual(plan.to_build, ["pkga", "pkgc", "pkgd"])
        self.assertEqual(plan.up_to_date, ["pkgb"])

    def test_target_from_includes_downstream_through_up_to_date(self):
        # pkgc is up to date but still links pkga to pkgd/pkge
        order = _five_package_order()
        plan = buildorder.plan_build(order, self.ALL, targets_from=["pkga"], force=True)
        self.assertEqual(plan.to_build, ["pkga", "pkgc", "pkgd", "pkge"])

    def test_done_packages_are_skipped_and_never_block(self):
        # An earlier plan of the same run builds pkga/pkgb: even forced,
        # they're not rebuilt, and pkgc doesn't wait for them.
        order = _five_package_order(need_build=set(self.ALL))
        plan = buildorder.plan_build(
            order, self.ALL, targets=["pkgc"], force=True, done={"pkga", "pkgb"}
        )
        self.assertEqual(plan.to_build, ["pkgc"])
        self.assertEqual(plan.up_to_date, [])

    def test_selections_union(self):
        order = _five_package_order()
        plan = buildorder.plan_build(
            order, self.ALL, targets=["pkgb"], targets_from=["pkgd"], force=True
        )
        self.assertEqual(plan.to_build, ["pkgb", "pkgd"])

    def test_unselected_dependency_needing_build_refuses(self):
        # same-group dependency outside the --target selection
        order = _five_package_order(need_build={"pkga", "pkgd"})
        with self.assertRaises(buildorder.UnbuiltDependenciesError) as ctx:
            buildorder.plan_build(order, self.ALL, targets=["pkgd"])
        self.assertEqual(ctx.exception.blockers, [_ref("pkga", PKGA_TEST_VERSION)])

    def test_out_of_group_dependency_needing_build_refuses(self):
        # pkga/pkgb stand in for an earlier group's packages
        order = _five_package_order(need_build={"pkgb", "pkgc"})
        with self.assertRaises(buildorder.UnbuiltDependenciesError) as ctx:
            buildorder.plan_build(order, ["pkgc", "pkgd", "pkge"])
        self.assertEqual(ctx.exception.blockers, [_ref("pkgb", PKGB_TEST_VERSION)])

    def test_force_still_refuses_unbuilt_out_of_group_dependency(self):
        order = _five_package_order(need_build={"pkga"})
        with self.assertRaises(buildorder.UnbuiltDependenciesError):
            buildorder.plan_build(order, ["pkgc", "pkgd", "pkge"], force=True)

    def test_unrelated_unbuilt_package_does_not_block(self):
        # pkgb needs building too, but pkga doesn't depend on it
        order = _five_package_order(need_build={"pkga", "pkgb"})
        plan = buildorder.plan_build(order, self.ALL, targets=["pkga"])
        self.assertEqual(plan.to_build, ["pkga"])

    def test_unknown_target_raises(self):
        order = _five_package_order()
        with self.assertRaises(buildorder.BuildOrderError):
            buildorder.plan_build(order, self.ALL, targets_to=["nope"])

    def test_nothing_to_build(self):
        order = _five_package_order()
        plan = buildorder.plan_build(order, self.ALL)
        self.assertEqual(plan.to_build, [])
        self.assertEqual(plan.up_to_date, self.ALL)


class TestRecipeFolder(unittest.TestCase):
    def test_single_version_recipe_has_no_config_yml(self):
        with tempfile.TemporaryDirectory() as repo_root:
            recipe_dir = os.path.join(repo_root, "packages", "conan", "recipes", "osl")
            os.makedirs(recipe_dir)
            self.assertEqual(
                buildorder._recipe_folder(repo_root, "osl", OSL_TEST_VERSION),
                recipe_dir,
            )

    def test_multi_version_recipe_uses_config_yml_folder(self):
        with tempfile.TemporaryDirectory() as repo_root:
            recipe_dir = os.path.join(repo_root, "packages", "conan", "recipes", "qt")
            os.makedirs(recipe_dir)
            with open(
                os.path.join(recipe_dir, "config.yml"), "w", encoding="utf-8"
            ) as f:
                f.write(f"versions:\n  '{QT_TEST_VERSION}':\n    folder: all\n")
            self.assertEqual(
                buildorder._recipe_folder(repo_root, "qt", QT_TEST_VERSION),
                os.path.join(recipe_dir, "all"),
            )

    def test_multi_version_recipe_missing_version_entry_raises(self):
        with tempfile.TemporaryDirectory() as repo_root:
            recipe_dir = os.path.join(repo_root, "packages", "conan", "recipes", "qt")
            os.makedirs(recipe_dir)
            with open(
                os.path.join(recipe_dir, "config.yml"), "w", encoding="utf-8"
            ) as f:
                f.write(f"versions:\n  '{QT_TEST_VERSION}':\n    folder: all\n")
            with self.assertRaises(buildorder.BuildOrderError):
                buildorder._recipe_folder(repo_root, "qt", "0.0.0")


class TestExportEntry(unittest.TestCase):
    def test_single_version(self):
        with tempfile.TemporaryDirectory() as repo_root:
            os.makedirs(os.path.join(repo_root, "packages", "conan", "recipes", "osl"))
            entry = buildorder._export_entry(
                repo_root, "osl", OSL_TEST_VERSION, TEST_PACKAGE_ORG, TEST_CHANNEL
            )
        self.assertEqual(
            entry,
            [
                os.path.join(buildorder.constants.ASWF_CONAN_HOME, "recipes", "osl"),
                "osl",
                OSL_TEST_VERSION,
                TEST_PACKAGE_ORG,
                TEST_CHANNEL,
            ],
        )

    def test_multi_version(self):
        with tempfile.TemporaryDirectory() as repo_root:
            recipe_dir = os.path.join(repo_root, "packages", "conan", "recipes", "qt")
            os.makedirs(recipe_dir)
            with open(
                os.path.join(recipe_dir, "config.yml"), "w", encoding="utf-8"
            ) as f:
                f.write(f"versions:\n  '{QT_TEST_VERSION}':\n    folder: all\n")
            entry = buildorder._export_entry(
                repo_root, "qt", QT_TEST_VERSION, TEST_PACKAGE_ORG, TEST_CHANNEL
            )
        self.assertEqual(
            entry[0],
            os.path.join(buildorder.constants.ASWF_CONAN_HOME, "recipes", "qt", "all"),
        )


class TestExportAllCommand(unittest.TestCase):
    def test_round_trips_through_shell_quoting(self):
        entries = [
            ["/opt/conan_home/recipes/osl", "osl", OSL_TEST_VERSION, "aswf", "vfx2027"],
            ["/opt/conan_home/recipes/zlib", "zlib", ZLIB_TEST_VERSION, "aswf", "x'y"],
        ]
        argv = shlex.split(
            buildorder._export_all_command(entries, ["--remote", "aswftesting"])
        )
        self.assertEqual(argv[:4], ["conan", "aswf-export", "--remote", "aswftesting"])
        self.assertEqual(len(argv), 5)
        self.assertEqual(json.loads(argv[4]), entries)


class TestBuildOrderCommand(unittest.TestCase):
    def test_contains_requires_and_profile(self):
        cmd = buildorder._build_order_command(
            [("pkga", PKGA_TEST_VERSION), ("pkgb", PKGB_TEST_VERSION)],
            TEST_PACKAGE_ORG,
            TEST_CHANNEL,
            ["--no-remote"],
        )
        self.assertIn(" --no-remote ", cmd)
        self.assertIn(f"--requires={_ref('pkga', PKGA_TEST_VERSION)}", cmd)
        self.assertIn(f"--requires={_ref('pkgb', PKGB_TEST_VERSION)}", cmd)
        self.assertNotIn("--reduce", cmd)  # plan_build() needs the full graph
        self.assertIn("--order-by=recipe", cmd)
        self.assertIn("--format=json", cmd)
        self.assertIn(
            os.path.join(
                buildorder.constants.ASWF_CONAN_HOME,
                ".conan2",
                "profiles",
                TEST_CHANNEL,
            ),
            cmd,
        )


def _write_profiles(repo_root):
    """A synthetic pair of generated profiles: TEST_CHANNEL includes
    OTHER_TEST_CHANNEL (profile_refs() follows include()s, although the
    generated profiles don't currently use them), redirects pkga/pkgb to
    itself, pkgc (via the include) and pkgd (via a *_ref conf value) to
    OTHER_TEST_CHANNEL, and also lists refs that must be ignored: a
    platform-style ref with no {{ org }}/channel, and one of our refs with
    no recipe directory."""
    profiles_dir = os.path.join(repo_root, "packages", "conan", "settings", "profiles")
    recipes_root = os.path.join(repo_root, "packages", "conan", "recipes")
    os.makedirs(profiles_dir)
    for name in ("pkga", "pkgb", "pkgc", "pkgd"):
        os.makedirs(os.path.join(recipes_root, name))
    org = '{% set org = os.getenv("ASWF_PKG_ORG", "aswf") %}'
    with open(
        os.path.join(profiles_dir, OTHER_TEST_CHANNEL), "w", encoding="utf-8"
    ) as f:
        f.write(
            f"{org}\n"
            "[replace_tool_requires]\n"
            f"pkgc/*: pkgc/{PKGC_TEST_VERSION}@{{{{ org }}}}/{OTHER_TEST_CHANNEL}\n"
        )
    with open(os.path.join(profiles_dir, TEST_CHANNEL), "w", encoding="utf-8") as f:
        f.write(
            "# generated\n"
            f"{org}\n"
            f"include({OTHER_TEST_CHANNEL})\n"
            "[options]\n"
            "*:shared=True\n"
            "[replace_requires]\n"
            f"pkga/*: pkga/{PKGA_TEST_VERSION}@{{{{ org }}}}/{TEST_CHANNEL}\n"
            f"pkgb/*: pkgb/{PKGB_TEST_VERSION}@{{{{ org }}}}/{TEST_CHANNEL}\n"
            "pkgconf/*: pkgconf/1.7.3@system\n"
            f"norecipe/*: norecipe/9.9.9@{{{{ org }}}}/{TEST_CHANNEL}\n"
            "[conf]\n"
            f"user.aswf:pkgd_ref=pkgd/{PKGD_TEST_VERSION}@{{{{ org }}}}/{OTHER_TEST_CHANNEL}\n"
            "user.aswf:unrelated_setting=True\n"
        )


class TestProfileRefs(unittest.TestCase):
    def test_follows_include_replace_sections_and_ref_conf(self):
        with tempfile.TemporaryDirectory() as repo_root:
            _write_profiles(repo_root)
            self.assertEqual(
                buildorder.profile_refs(repo_root, TEST_CHANNEL),
                [
                    ("pkgc", PKGC_TEST_VERSION, OTHER_TEST_CHANNEL),
                    ("pkga", PKGA_TEST_VERSION, TEST_CHANNEL),
                    ("pkgb", PKGB_TEST_VERSION, TEST_CHANNEL),
                    ("pkgd", PKGD_TEST_VERSION, OTHER_TEST_CHANNEL),
                ],
            )


class TestComputeBuildOrder(unittest.TestCase):
    """Fixture shape: pkga (no deps) -> pkgb (depends on pkga), requested as
    members; pkgc/pkgd are only reachable through the profile (standing in
    for out-of-group dependencies like cpython, or cmake from common).
    Purely synthetic -- see module docstring comment above."""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmpdir.cleanup)
        self.repo_root = tmpdir.name
        _write_profiles(self.repo_root)
        self.members = [("pkga", PKGA_TEST_VERSION), ("pkgb", PKGB_TEST_VERSION)]
        self.fixture = {
            "order_by": "recipe",
            "order": [
                [_ref_entry(_ref("pkga", PKGA_TEST_VERSION))],
                [
                    _ref_entry(
                        _ref("pkgb", PKGB_TEST_VERSION),
                        depends=[_ref("pkga", PKGA_TEST_VERSION)],
                    )
                ],
            ],
        }

    def _compute(
        self, members=None, conan_remote=buildorder.constants.CONAN_REMOTE_LOCAL
    ):
        # conan_remote is explicit by default so these tests don't depend on
        # GITHUB_ACTIONS when run on CI.
        return buildorder.compute_build_order(
            self.repo_root,
            TEST_PACKAGE_ORG,
            TEST_PACKAGE_ORG,
            "2027",
            TEST_CHANNEL,
            self.members if members is None else members,
            conan_remote=conan_remote,
        )

    def test_empty_members_skips_subprocess(self):
        with patch.object(buildorder.subprocess, "run") as mock_run:
            order = self._compute(members=[])
        mock_run.assert_not_called()
        self.assertEqual(order.levels, [])

    @staticmethod
    def _output_dir_from_cmd(cmd):
        prefix = f"--set={buildorder.BAKE_TARGET_NAME}.output=type=local,dest="
        for arg in cmd:
            if arg.startswith(prefix):
                return arg[len(prefix) :]
        raise AssertionError(f"no --set=...output=type=local,dest=... in {cmd!r}")

    def _fake_bake_run(self, output_contents, captured=None):
        """Returns a subprocess.run replacement that writes
        `output_contents` into the --output=type=local,dest=... directory
        the real `compute_build_order()` passes, mimicking what `docker
        buildx bake` would actually leave there. If `captured` is given,
        records the invoked cmd and the bake JSON file's parsed content
        into it -- both read here, synchronously, since
        compute_build_order() deletes its tempdir (bake jsonfile included)
        before returning."""

        def run(cmd, **_kwargs):
            if captured is not None:
                captured["cmd"] = cmd
                bake_jsonfile = cmd[cmd.index("-f") + 1]
                with open(bake_jsonfile, "r", encoding="utf-8") as f:
                    captured["bake_dict"] = json.load(f)
            output_dir = self._output_dir_from_cmd(cmd)
            output_path = os.path.join(output_dir, buildorder.BUILD_ORDER_FILENAME)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(output_contents)
            return MagicMock(returncode=0, stdout="", stderr="")

        return run

    def test_happy_path_parses_result(self):
        captured = {}
        with patch.object(
            buildorder.subprocess,
            "run",
            side_effect=self._fake_bake_run(json.dumps(self.fixture), captured),
        ):
            order = self._compute()
        self.assertEqual(order.levels, [["pkga"], ["pkgb"]])

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "buildx", "bake"])
        self.assertIn(
            f"--set={buildorder.BAKE_TARGET_NAME}.target=ci-conan-graph-output", cmd
        )
        self.assertIn(buildorder.BAKE_TARGET_NAME, cmd)

        args = captured["bake_dict"]["target"][buildorder.BAKE_TARGET_NAME]["args"]
        self.assertEqual(args["ASWF_ORG"], TEST_PACKAGE_ORG)
        self.assertEqual(args["ASWF_PKG_ORG"], TEST_PACKAGE_ORG)
        self.assertEqual(args["ASWF_VFXPLATFORM_VERSION"], "2027")
        self.assertEqual(args["ASWF_CONAN_CHANNEL"], TEST_CHANNEL)
        self.assertIn("ASWF_CONAN_GRAPH_SCRIPT", args)
        self.assertIn("conan graph build-order", args["ASWF_CONAN_GRAPH_SCRIPT"])

    def test_forces_fresh_run_without_resetting_cache_mounts(self):
        # A per-run nonce ARG defeats BuildKit's layer-cache replay of the
        # graph RUN. no-cache/no-cache-filter must never be used for this:
        # they also make BuildKit replace the shared aswf-conan-cache (and
        # aswf-ccache) mounts with fresh empty ones.
        nonces = []
        for _ in range(2):
            captured = {}
            with patch.object(
                buildorder.subprocess,
                "run",
                side_effect=self._fake_bake_run(json.dumps(self.fixture), captured),
            ):
                self._compute()
            target = captured["bake_dict"]["target"][buildorder.BAKE_TARGET_NAME]
            self.assertNotIn("no-cache", target)
            self.assertNotIn("no-cache-filter", target)
            nonces.append(target["args"]["ASWF_CONAN_NONCE"])
            self.assertFalse(
                any("no-cache" in arg for arg in captured["cmd"]), captured["cmd"]
            )
        self.assertNotEqual(nonces[0], nonces[1])

    def test_exports_profile_refs_on_their_own_channel_requires_only_members(self):
        captured = {}
        with patch.object(
            buildorder.subprocess,
            "run",
            side_effect=self._fake_bake_run(json.dumps(self.fixture), captured),
        ):
            self._compute()
        args = captured["bake_dict"]["target"][buildorder.BAKE_TARGET_NAME]["args"]
        script = args["ASWF_CONAN_GRAPH_SCRIPT"]
        export_part, graph_part = script.split(" && ")
        argv = shlex.split(export_part)
        self.assertEqual(argv[:2], ["conan", "aswf-export"])
        exported = [tuple(entry[1:]) for entry in json.loads(argv[-1])]
        self.assertEqual(
            exported,
            [
                ("pkgc", PKGC_TEST_VERSION, TEST_PACKAGE_ORG, OTHER_TEST_CHANNEL),
                ("pkga", PKGA_TEST_VERSION, TEST_PACKAGE_ORG, TEST_CHANNEL),
                ("pkgb", PKGB_TEST_VERSION, TEST_PACKAGE_ORG, TEST_CHANNEL),
                ("pkgd", PKGD_TEST_VERSION, TEST_PACKAGE_ORG, OTHER_TEST_CHANNEL),
            ],
        )
        self.assertIn(f"--requires={_ref('pkga', PKGA_TEST_VERSION)}", graph_part)
        self.assertIn(f"--requires={_ref('pkgb', PKGB_TEST_VERSION)}", graph_part)
        self.assertNotIn("--requires=pkgc", graph_part)
        self.assertNotIn("--requires=pkgd", graph_part)

    def test_remote_policy_applies_to_export_and_graph(self):
        for conan_remote, expected in (
            (buildorder.constants.CONAN_REMOTE_LOCAL, ["--no-remote"]),
            ("aswftesting", ["--remote", "aswftesting"]),
        ):
            captured = {}
            with patch.object(
                buildorder.subprocess,
                "run",
                side_effect=self._fake_bake_run(json.dumps(self.fixture), captured),
            ):
                self._compute(conan_remote=conan_remote)
            target = captured["bake_dict"]["target"][buildorder.BAKE_TARGET_NAME]
            export_part, graph_part = target["args"]["ASWF_CONAN_GRAPH_SCRIPT"].split(
                " && "
            )
            self.assertEqual(shlex.split(export_part)[2:-1], expected)
            self.assertIn(f" {' '.join(expected)} ", graph_part)

    def test_nonzero_exit_raises(self):
        mock_result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch.object(buildorder.subprocess, "run", return_value=mock_result):
            with self.assertRaises(buildorder.BuildOrderError):
                self._compute()

    def test_invalid_json_raises(self):
        with patch.object(
            buildorder.subprocess, "run", side_effect=self._fake_bake_run("not json")
        ):
            with self.assertRaises(buildorder.BuildOrderError):
                self._compute()

    def test_missing_output_file_raises(self):
        # docker buildx bake "succeeded" but never actually wrote the
        # extracted file -- shouldn't happen, but must not be misread as
        # an empty/trivial result.
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(buildorder.subprocess, "run", return_value=mock_result):
            with self.assertRaises(buildorder.BuildOrderError):
                self._compute()


if __name__ == "__main__":
    unittest.main()
