# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Computes the Conan dependency build order for a set of packages, so a
package group (e.g. "vfx") can be built/released in the right order without
hand-maintaining split layers in versions.yaml, and plans which of those
packages a build actually needs to (re)build.

The order is computed by running `conan graph build-order` via a
`docker buildx bake` call against packages/common/Dockerfile's
ci-conan-package-builder stage/ci-conan-graph-output stage -- the same
Dockerfile, base image, bind-mounted recipes/profiles and persistent Conan
cache mount (id=aswf-conan-cache) real `conan create` package builds use.
Nothing is actually built: recipes are exported (metadata only, no
download/build) so Conan can see their requires() -- all in one Conan
process via the `conan aswf-export` custom command -- then a single
`conan graph build-order` call resolves the graph.

Reusing that same cache mount (rather than a plain `docker run`, which
cannot see a BuildKit cache mount at all) matters for correctness, not just
consistency: `--build=missing`'s decision of what still needs building has
to see whatever is already sitting in the *local* Conan cache from a
previous local build, so a local `aswfdocker build --group vfx1` only
rebuilds what actually changed and its dependents. On a fresh GHA runner
that cache mount starts empty, so the same mechanism naturally falls
through to checking the configured remotes (remotes.json) instead -- which
is also exactly the right behavior there, since every package upload
completes before its orchestrator job reports success, making the remote
the authoritative record between levels. No environment-specific branching
is needed in this module for that: it's just whatever the cache mount
actually contains, in either case.

Every one of our packages the profile can redirect a dependency to is
exported, each under its own channel (see profile_refs()), not just the
requested members: Conan has to read a dependency's recipe to follow its
requires(), so a dependency outside the group whose recipe was never
exported (e.g. a freshly bumped cpython, or cmake on ci_common<N>) would
otherwise make graph resolution fail outright instead of being reported. Missing *binaries* are
never a problem -- `--build=missing` just marks them "Build". With every
recipe available, the result answers "what needs building for these
members" from any cache state, including an empty or fully invalidated one.

The graph is deliberately not `--reduce`d: it keeps packages that already
have an up-to-date binary, along with each package's build status, so the
selection logic in plan_build() can follow edges through up-to-date packages
(--target-to/--target-from) and place packages Conan considers current
(--force-rebuild).
"""

import json
import os
import re
import shlex
import subprocess
import tempfile
import typing
import uuid

import yaml

from aswfdocker import bake, constants, utils

CONFIG_YML_NAME = "config.yml"
BAKE_TARGET_NAME = "ci-conan-graph"
BUILD_ORDER_FILENAME = "aswf-build-order.json"


# `binary` statuses from conan/internal/graph/graph.py (BINARY_*)
_NEEDS_BUILD = {"Build", "EditableBuild"}
_UNBUILDABLE = {"Invalid", "Missing"}


class BuildOrderError(Exception):
    """Raised when the Conan build-order graph could not be computed or
    parsed, or a build can't be planned from it."""


class UnbuiltDependenciesError(BuildOrderError):
    """Packages a planned build depends on need (re)building but weren't
    selected -- they belong to an earlier group, or the same group but
    outside the --target selection."""

    def __init__(self, blockers: typing.Sequence[str]):
        self.blockers = list(blockers)
        super().__init__(
            "These dependencies need to be (re)built first, but aren't part of "
            "this build's selection -- build their group first, or widen the "
            "selection (e.g. --target-to):\n  " + "\n  ".join(self.blockers)
        )


def _name_from_ref(ref: str) -> str:
    """Extracts the bare package name from a Conan ref string, e.g.
    "osl/1.14.4.0@aswf/vfx2027" -> "osl"."""
    return ref.split("@", 1)[0].split("/", 1)[0]


class BuildOrder:
    """The leveled dependency graph from one `conan graph build-order`
    result: every package in it (whether or not it needs building), their
    dependency edges, and which ones need (re)building.

    `levels[i]` only ever depends on names in `levels[0:i]` (or on nothing),
    so each level can in principle be built in parallel once Conan 2's
    sqlite build-cache concurrency issue no longer forces sequential builds.
    """

    def __init__(self, levels: typing.List[typing.List[str]]):
        self.levels: typing.List[typing.List[str]] = levels
        self.need_build: typing.Set[str] = set()
        # name -> full refs, e.g. two clang versions selected via conf refs
        self.refs: typing.Dict[str, typing.Set[str]] = {}
        self._depends: typing.Dict[str, typing.Set[str]] = {}
        self._dependents: typing.Dict[str, typing.Set[str]] = {}

    def add_edge(self, name: str, depends_on: str) -> None:
        self._depends.setdefault(name, set()).add(depends_on)
        self._dependents.setdefault(depends_on, set()).add(name)

    def flat_order(self) -> typing.List[str]:
        """The levels flattened into a single sequential build order, each
        name once."""
        return list(dict.fromkeys(name for level in self.levels for name in level))

    @staticmethod
    def _closure(
        seed_names: typing.Iterable[str],
        edges: typing.Dict[str, typing.Set[str]],
    ) -> typing.Set[str]:
        closure: typing.Set[str] = set()
        pending = list(seed_names)
        while pending:
            name = pending.pop()
            for other in edges.get(name, ()):
                if other not in closure:
                    closure.add(other)
                    pending.append(other)
        return closure

    def downstream_closure(self, seed_names: typing.Iterable[str]) -> typing.Set[str]:
        """Every package that transitively depends on any of `seed_names`,
        not including the seeds themselves (unless one depends on another).

        Drives --target-from, and later skipping packages blocked by a
        failed build / --resume-from-package.
        """
        return self._closure(seed_names, self._dependents)

    def upstream_closure(self, seed_names: typing.Iterable[str]) -> typing.Set[str]:
        """Every package any of `seed_names` transitively depends on, not
        including the seeds themselves (unless one depends on another).

        Drives --target-to, and finding unbuilt dependencies of a build.
        """
        return self._closure(seed_names, self._depends)


def parse_build_order_json(data: dict) -> BuildOrder:
    """Parses the output of
    `conan graph build-order --order-by=recipe --format=json` (unreduced).

    Each entry in data["order"] is a level (list of recipe-reference
    dicts); each dict has "ref" (e.g. "osl/1.14.4.0@aswf/vfx2027"),
    "depends" (a list of other recipe-ref strings) and "packages" (levels
    of binary-package dicts, each with a "binary" status such as "Build"
    or "Cache").

    `conan graph build-order` doesn't fail on a package it can't build --
    it just records an "Invalid" (e.g. rejected by the recipe's validate())
    or "Missing" status -- so those are turned into an error here.
    """
    try:
        raw_levels = data["order"]
    except (TypeError, KeyError) as exc:
        raise BuildOrderError(
            f"Unexpected conan graph build-order output, missing 'order': {data!r}"
        ) from exc

    levels: typing.List[typing.List[str]] = []
    order = BuildOrder(levels)
    unbuildable = []
    for raw_level in raw_levels:
        level_names = []
        for entry in raw_level:
            name = _name_from_ref(entry["ref"])
            level_names.append(name)
            order.refs.setdefault(name, set()).add(entry["ref"])
            for dep_ref in entry.get("depends", []):
                order.add_edge(name, _name_from_ref(dep_ref))
            binaries = {
                package.get("binary")
                for package_level in entry.get("packages", [])
                for package in package_level
            }
            if binaries & _NEEDS_BUILD:
                order.need_build.add(name)
            for binary in sorted(b for b in binaries & _UNBUILDABLE if b):
                unbuildable.append(f"{entry['ref']} ({binary})")
        levels.append(level_names)
    if unbuildable:
        raise BuildOrderError(
            "Conan can't build these packages with this profile:\n  "
            + "\n  ".join(unbuildable)
        )
    return order


class BuildPlan(typing.NamedTuple):
    to_build: typing.List[str]
    """Selected packages to build, in dependency order."""
    up_to_date: typing.List[str]
    """Selected packages skipped because their binary is already current."""


def plan_build(  # pylint: disable=too-many-arguments
    order: BuildOrder,
    members: typing.Iterable[str],
    targets: typing.Iterable[str] = (),
    targets_to: typing.Iterable[str] = (),
    targets_from: typing.Iterable[str] = (),
    force: bool = False,
) -> BuildPlan:
    """Decides which group members to build, and in what order.

    The selection is every member of the group, unless narrowed by:
    `targets` (just those packages), `targets_to` (each package plus
    everything it transitively requires) and/or `targets_from` (each
    package plus everything that transitively requires it) -- their union,
    limited to the group's members. Selected packages are built if they
    need (re)building, or unconditionally with `force`.

    Raises UnbuiltDependenciesError if anything those builds depend on
    needs building but isn't selected, rather than building against a
    dependency that isn't there.
    """
    member_set = set(members)
    named = set(targets) | set(targets_to) | set(targets_from)
    unknown = sorted(named - member_set)
    if unknown:
        raise BuildOrderError(
            f"Not members of the requested group(s): {', '.join(unknown)}"
        )
    if named:
        selected = (
            named
            | order.upstream_closure(targets_to)
            | order.downstream_closure(targets_from)
        ) & member_set
    else:
        selected = member_set

    flat = order.flat_order()
    to_build = [n for n in flat if n in selected and (force or n in order.need_build)]
    up_to_date = [n for n in flat if n in selected and n not in to_build]
    blocking = (order.upstream_closure(to_build) & order.need_build) - selected
    if blocking:
        raise UnbuiltDependenciesError(
            sorted(ref for name in blocking for ref in order.refs.get(name, {name}))
        )
    return BuildPlan(to_build, up_to_date)


def _recipe_folder(repo_root: str, name: str, version: str) -> str:
    """Mirrors packages/common/Dockerfile's own resolution of a recipe's
    source folder: most recipes are single-version and live directly under
    packages/conan/recipes/<name>, but a few (qt, onetbb, ...) are
    multi-version and keep a config.yml mapping each version to its own
    sub-folder, same as a Conan Center Index "all" recipe layout."""
    recipe_dir = os.path.join(repo_root, "packages", "conan", "recipes", name)
    config_path = os.path.join(recipe_dir, CONFIG_YML_NAME)
    if not os.path.isfile(config_path):
        return recipe_dir
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    folder = (config.get("versions") or {}).get(version, {}).get("folder")
    if not folder:
        raise BuildOrderError(f"{config_path}: no folder entry for version {version!r}")
    return os.path.join(recipe_dir, folder)


def _export_entry(
    repo_root: str, name: str, version: str, package_org: str, channel: str
) -> typing.List[str]:
    """One [path, name, version, user, channel] entry for the `conan
    aswf-export` custom command (packages/conan/settings/extensions/
    commands/cmd_aswf_export.py)."""
    folder = _recipe_folder(repo_root, name, version)
    # folder is a host path (used to compute config.yml), but inside the
    # container recipes are bind-mounted at ASWF_CONAN_HOME/recipes instead.
    recipes_root = os.path.join(repo_root, "packages", "conan", "recipes")
    relative = os.path.relpath(folder, recipes_root)
    container_folder = os.path.join(constants.ASWF_CONAN_HOME, "recipes", relative)
    return [container_folder, name, version, package_org, channel]


def _shell_args(args: typing.Sequence[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _export_all_command(
    entries: typing.Sequence[typing.Sequence[str]],
    remote_args: typing.Sequence[str],
) -> str:
    # One Conan process for every export, instead of one `conan export`
    # (and one interpreter start) per recipe.
    return (
        f"conan aswf-export {_shell_args(remote_args)} "
        f"{shlex.quote(json.dumps(list(entries)))}"
    )


def _build_order_command(
    members: typing.Sequence[typing.Tuple[str, str]],
    package_org: str,
    channel: str,
    remote_args: typing.Sequence[str],
) -> str:
    profile = os.path.join(constants.ASWF_CONAN_HOME, ".conan2", "profiles", channel)
    requires = " ".join(
        f"--requires={shlex.quote(f'{name}/{version}@{package_org}/{channel}')}"
        for name, version in members
    )
    profile_arg = shlex.quote(profile)
    return (
        f"conan graph build-order {requires} "
        f"--profile:all {profile_arg} {_shell_args(remote_args)} --build=missing "
        "--order-by=recipe --format=json"
    )


_PROFILE_REF_RE = re.compile(
    r"^(?P<name>[\w.+-]+)/(?P<version>[^@\s]+)@\{\{\s*org\s*\}\}/(?P<channel>\S+)$"
)
_REPLACE_SECTIONS = {"replace_requires", "replace_tool_requires"}


def profile_refs(
    repo_root: str, profile_name: str
) -> typing.List[typing.Tuple[str, str, str]]:
    """(name, version, channel) for every one of our own packages the given
    generated Conan profile (following its include()s) can redirect a
    dependency to: the targets of [replace_requires]/[replace_tool_requires]
    and user.aswf:*_ref conf values, e.g. "cmake/4.3.3@{{ org }}/ci_common7".

    These are exactly the references Conan will look up while resolving a
    graph with that profile -- including common-tier packages on the
    ci_common<N> channel from a vfx<year> profile, and every clang version
    a recipe can select via conf. Only refs with a recipe directory are
    returned; others (e.g. platform/system packages) aren't ours to export.
    """
    profiles_dir = os.path.join(repo_root, "packages", "conan", "settings", "profiles")
    recipes_root = os.path.join(repo_root, "packages", "conan", "recipes")
    refs: typing.Dict[typing.Tuple[str, str, str], None] = {}
    visited: typing.Set[str] = set()

    def scan(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        section = ""
        with open(os.path.join(profiles_dir, name), "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith(("#", "{%")):
                    continue
                if line.startswith("include(") and line.endswith(")"):
                    scan(line[len("include(") : -1].strip())
                elif line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                elif section in _REPLACE_SECTIONS and ":" in line:
                    add(line.split(":", 1)[1].strip())
                elif section == "conf" and "=" in line:
                    key, value = line.split("=", 1)
                    if key.strip().endswith("_ref"):
                        add(value.strip())

    def add(ref: str) -> None:
        match = _PROFILE_REF_RE.match(ref)
        if match and os.path.isdir(os.path.join(recipes_root, match["name"])):
            refs[(match["name"], match["version"], match["channel"])] = None

    scan(profile_name)
    return list(refs)


def _graph_script(
    repo_root: str,
    package_org: str,
    channel: str,
    members: typing.Sequence[typing.Tuple[str, str]],
    exports: typing.Sequence[typing.Tuple[str, str, str]],
    remote_args: typing.Sequence[str],
) -> str:
    to_export = list(exports)
    for name, version in members:
        if (name, version, channel) not in to_export:
            to_export.append((name, version, channel))
    entries = [
        _export_entry(repo_root, name, version, package_org, export_channel)
        for name, version, export_channel in to_export
    ]
    return (
        f"{_export_all_command(entries, remote_args)} && "
        f"{_build_order_command(members, package_org, channel, remote_args)}"
    )


def _bake_dict(
    docker_org: str,
    package_org: str,
    ci_common_version: str,
    channel: str,
    script: str,
    nonce: str,
) -> dict:
    args = utils.common_conan_bake_args(
        docker_org, package_org, ci_common_version, channel
    )
    args["ASWF_CONAN_GRAPH_SCRIPT"] = script
    # See ASWF_CONAN_NONCE in packages/common/Dockerfile: forces the graph RUN
    # to execute every time, without the no-cache/no-cache-filter side effect
    # of replacing the shared Conan cache mount with an empty one.
    args["ASWF_CONAN_NONCE"] = nonce
    return {
        "target": {
            BAKE_TARGET_NAME: {
                "context": ".",
                "dockerfile": constants.CONAN_PACKAGE_DOCKERFILE,
                "args": args,
            }
        }
    }


def _read_build_order_output(output_dir: str) -> dict:
    output_path = os.path.join(output_dir, BUILD_ORDER_FILENAME)
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise BuildOrderError(
            f"docker buildx bake did not produce {output_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise BuildOrderError(
            f"Could not parse conan graph build-order output as JSON: {exc}"
        ) from exc


def _run_bake_and_read_output(repo_root: str, bake_dict: dict, progress: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        bake_jsonfile = os.path.join(tmpdir, "docker-bake-graph.json")
        bake.write_bake_jsonfile(bake_jsonfile, bake_dict)
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir)

        cmd = bake.bake_command(
            bake_jsonfile,
            build_targets=[BAKE_TARGET_NAME],
            overrides={
                "target": "ci-conan-graph-output",
                "output": f"type=local,dest={output_dir}",
            },
            progress=progress,
            override_scope=BAKE_TARGET_NAME,
        )
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise BuildOrderError(
                f"Failed to compute build order (exit {result.returncode}):\n"
                f"{result.stderr}"
            )
        return _read_build_order_output(output_dir)


def compute_build_order(
    repo_root: str,
    docker_org: str,
    package_org: str,
    ci_common_version: str,
    channel: str,
    members: typing.Sequence[typing.Tuple[str, str]],
    conan_remote: str = constants.CONAN_REMOTE_AUTO,
    progress: str = "plain",
) -> BuildOrder:
    """Computes the build order for `members` (a sequence of (name, version)
    pairs, e.g. the flattened image list of a "vfx1" meta-group), via
    `docker buildx bake` against the same packages/common/Dockerfile real
    package builds use (see module docstring for why this has to be bake
    and not a plain `docker run`). Every package the `channel` profile can
    redirect a dependency to is exported first (see profile_refs()).
    `conan_remote` is the same remote policy real builds apply (see
    utils.resolve_conan_remote()), so "what needs building" is answered
    against the same cache/remote a real build would use.
    """
    if not members:
        return BuildOrder([])

    remote_args = utils.conan_remote_args(
        utils.resolve_conan_remote(conan_remote, package_org)
    )
    exports = profile_refs(repo_root, channel)
    script = _graph_script(
        repo_root, package_org, channel, members, exports, remote_args
    )
    bake_dict = _bake_dict(
        docker_org, package_org, ci_common_version, channel, script, uuid.uuid4().hex
    )
    data = _run_bake_and_read_output(repo_root, bake_dict, progress)
    return parse_build_order_json(data)
