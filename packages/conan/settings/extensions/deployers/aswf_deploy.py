# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ASWF Conan 2 deployer: copies every dependency directly from the Conan
cache into output_folder, replacing the old full_deploy + rsync two-step
that scripts/common/install_conanpackages.sh used to perform.

This module must remain import-free of the `aswfdocker` pip package: nothing
guarantees it is installed inside the CI image being built, only on the host
driving `docker buildx bake`.

Invoked as: conan install <path> --deployer=aswf_deploy --deployer-folder=<dest>
with the package to exclude (if any) passed as -c user.aswf:exclude_package=<name>,
and the SBOM's subject as -c user.aswf:sbom_name=<name>/<version> (the image
name and its ASWF_VERSION; the version is optional).

Also writes a CycloneDX SBOM of the packages it deployed to
<dest>/aswf/<sbom_name>.cdx.json. Images are built in layers (ci-common,
ci-base, ...) that each run their own deploy, so naming the SBOM per image
keeps every layer's SBOM instead of each overwriting the last. All the
layers' SBOMs found there are then merged into <dest>/aswf/sbom.cdx.json,
which describes the whole image without needing to know its layering (and
is what gets attached to the image's GitHub release); each layer rewrites it.
"""

import fnmatch
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import uuid

from conan.tools.sbom import cyclonedx_1_6

_CONAN_METADATA_FILES = {"conaninfo.txt", "conanmanifest.txt"}
_LICENSES_DIR_NAME = "licenses"
_MD5_CHUNK_SIZE = 1024 * 1024  # 1 MiB -- files like libclang-cpp.so are 100+MB
_SBOM_DIR_NAME = "aswf"
_DEFAULT_SBOM_NAME = "conan-sbom"
_MERGED_SBOM_FILE = "sbom.cdx.json"
# Written by the hook_aswf_provenance Conan hook (extensions/hooks/), which
# can't share this module's code.
_PROVENANCE_FILE = "aswf_provenance.json"

# SPDX expression operators (https://spdx.github.io/spdx-spec/v2.3/SPDX-license-expressions/)
_SPDX_OPERATORS = re.compile(r" (WITH|AND|OR) ")

_PACKAGE_HOOKS = {}


def deploy(graph, output_folder):
    conanfile = graph.root.conanfile
    exclude_name = conanfile.conf.get("user.aswf:exclude_package")
    symlinks = conanfile.conf.get(
        "tools.deployer:symlinks", check_type=bool, default=True
    )
    cache_root = _cache_root(os.environ["CONAN_HOME"])

    os.makedirs(output_folder, exist_ok=True)
    report = _DeployReport()
    in_image = []
    for require, dep in conanfile.dependencies.items():
        if _should_skip_scope(require):
            continue
        if exclude_name and dep.ref.name == exclude_name:
            continue
        # In the SBOM even when not copied, e.g. "system" wrappers, which the
        # image gets from the OS instead.
        in_image.append(dep)
        if _should_skip(dep, exclude_name):
            continue
        _deploy_package(dep, output_folder, cache_root, symlinks, report)
    report.print_report()
    report.raise_if_errors()
    _write_sbom(conanfile, in_image, output_folder)


def _write_sbom(conanfile, deps, output_folder) -> None:
    """Writes Conan's CycloneDX 1.6 SBOM for the graph, limited to `deps`
    (what this deploy considers part of the image) and annotated with each
    package's ASWF build provenance."""
    # One "name/version" value: Conan eval()s conf values, which would turn a
    # bare version such as 2027.10 into the number 2027.1.
    sbom_name = conanfile.conf.get("user.aswf:sbom_name", check_type=str)
    name, _, version = (sbom_name or _DEFAULT_SBOM_NAME).partition("/")
    version = version or None
    # Host context only, like the deploy itself: what's in the image and what
    # it links/runs against. A package's own tool_requires (e.g. clang's
    # cmake/ninja) would otherwise show up as untyped dependsOn edges -- and,
    # since bom-refs don't include the context, as duplicate components.
    sbom = cyclonedx_1_6(conanfile, name=name, add_build=False)
    properties = {
        (dep.ref.name, str(dep.ref.version)): _component_properties(
            dep, _is_system_wrapper(dep)
        )
        for dep in deps
    }
    sbom = _set_sbom_root(_filter_and_annotate_sbom(sbom, properties), name, version)
    sbom_dir = os.path.join(output_folder, _SBOM_DIR_NAME)
    path = os.path.join(sbom_dir, f"{name}.cdx.json")
    _write_json(path, sbom)
    print(
        f"aswf_deploy: wrote SBOM {path} ({len(sbom.get('components', []))} components)"
    )
    layer_sboms = _read_layer_sboms(sbom_dir)
    merged = _merge_sboms(layer_sboms, sbom, name, version)
    merged_path = os.path.join(sbom_dir, _MERGED_SBOM_FILE)
    _write_json(merged_path, merged)
    print(
        f"aswf_deploy: wrote image SBOM {merged_path} from "
        f"{len(layer_sboms)} layer(s) ({len(merged.get('components', []))} components)"
    )


def _read_layer_sboms(sbom_dir) -> list:
    """Every layer's SBOM in `sbom_dir`, base image first (lower layers
    were built first)."""
    layer_sboms = []
    for path in glob.glob(os.path.join(sbom_dir, "*.cdx.json")):
        if os.path.basename(path) == _MERGED_SBOM_FILE:
            continue
        with open(path, "r", encoding="utf-8") as f:
            layer_sboms.append(json.load(f))
    layer_sboms.sort(
        key=lambda s: (
            s["metadata"].get("timestamp", ""),
            s["metadata"]["component"]["name"],
        )
    )
    return layer_sboms


def _union_dependencies(entries, rename=None) -> dict:
    """CycloneDX dependencies entries as {ref: [dependsOn...]}, merging the
    entries of a ref that appears more than once (Conan writes one per graph
    node, and several nodes can share a bom-ref; merged layers share refs
    too). `rename` maps refs to replace, e.g. layer roots to the image's."""
    rename = rename or {}
    depends_on: dict = {}
    for entry in entries:
        merged = depends_on.setdefault(rename.get(entry["ref"], entry["ref"]), [])
        merged.extend(d for d in entry.get("dependsOn", []) if d not in merged)
    return depends_on


def _warn_about_different_builds(components) -> None:
    refs_by_name: dict = {}
    for component in components:
        refs_by_name.setdefault(component["name"], []).append(component["bom-ref"])
    for package, refs in sorted(refs_by_name.items()):
        if len(refs) > 1:
            print(
                f"aswf_deploy: WARNING: image layers ship different builds of "
                f"{package}: {', '.join(refs)}"
            )


def _write_json(path, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def _root_ref(name, version) -> str:
    return f"{name}@{version}" if version else name


def _set_sbom_root(sbom: dict, name, version) -> dict:
    """Replaces the SBOM's root component, which Conan gives a random
    bom-ref for any consumer conanfile (it has no recipe revision), with
    the image itself under a stable bom-ref."""
    old_ref = sbom["metadata"]["component"]["bom-ref"]
    new_ref = _root_ref(name, version)
    root = {"bom-ref": new_ref, "name": name, "type": "container"}
    if version:
        root["version"] = version
    dependencies = [
        {**e, "ref": new_ref} if e["ref"] == old_ref else e
        for e in sbom.get("dependencies", [])
    ]
    result = {**sbom, "metadata": {**sbom["metadata"], "component": root}}
    if dependencies:
        result["dependencies"] = dependencies
    return result


def _merge_sboms(layer_sboms, current: dict, name, version) -> dict:
    """Merges the image's per-layer SBOMs (each as written by _write_sbom)
    into one rooted at the image: components are deduplicated by bom-ref,
    which for Conan packages includes the recipe revision, and the image
    root depends on everything any layer's root depended on. Its metadata
    (tools, timestamp) comes from `current`, this deploy's own SBOM."""
    root_ref = _root_ref(name, version)
    components: dict = {}
    for sbom in layer_sboms:
        for component in sbom.get("components", []):
            components.setdefault(component["bom-ref"], component)
    _warn_about_different_builds(components.values())
    depends_on = _union_dependencies(
        [{"ref": root_ref}]
        + [e for s in layer_sboms for e in s.get("dependencies", [])],
        rename={s["metadata"]["component"]["bom-ref"]: root_ref for s in layer_sboms},
    )
    root = {**current["metadata"]["component"]}
    root["properties"] = [
        {
            "name": "aswf:layers",
            "value": ",".join(s["metadata"]["component"]["name"] for s in layer_sboms),
        }
    ]
    result = {
        k: v for k, v in current.items() if k not in ("components", "dependencies")
    }
    result["serialNumber"] = f"urn:uuid:{uuid.uuid4()}"
    result["metadata"] = {**current["metadata"], "component": root}
    if components:
        result["components"] = list(components.values())
    result["dependencies"] = [
        {"ref": ref, **({"dependsOn": deps} if deps else {})}
        for ref, deps in depends_on.items()
    ]
    return result


def _component_properties(dep, system=False) -> dict:
    """CycloneDX component properties for one package: its Conan package_id,
    the git state it was built from (if recorded at build time), and whether
    it's a wrapper whose files the image gets from OS packages instead."""
    properties = {"aswf:package_id": dep.pref.package_id}
    if system:
        properties["aswf:provided_by"] = "system"
    metadata_folder = dep.package_metadata_folder
    provenance_path = (
        os.path.join(metadata_folder, _PROVENANCE_FILE) if metadata_folder else None
    )
    if provenance_path and os.path.isfile(provenance_path):
        with open(provenance_path, "r", encoding="utf-8") as f:
            provenance = json.load(f)
        properties["aswf:git_sha"] = provenance["git_sha"]
        properties["aswf:git_dirty"] = str(bool(provenance.get("git_dirty"))).lower()
        if provenance.get("git_diff_hash"):
            properties["aswf:git_diff_hash"] = provenance["git_diff_hash"]
    return properties


def _filter_and_annotate_sbom(sbom: dict, properties: dict) -> dict:
    """Keeps only the components whose (name, version) is in `properties`,
    adds those properties to them (CycloneDX's name/value extension point),
    and rewires the dependency graph past any dropped component, so a kept
    package still shows what it (transitively) depends on in the image."""
    kept = []
    kept_refs = set()
    for component in sbom.get("components", []):
        key = (component["name"], component["version"])
        if key not in properties or component["bom-ref"] in kept_refs:
            continue
        kept_refs.add(component["bom-ref"])
        props = [{"name": n, "value": v} for n, v in sorted(properties[key].items())]
        component = {**component, **({"properties": props} if props else {})}
        if "licenses" in component:
            component["licenses"] = _spdx_licenses(component["licenses"])
        kept.append(component)
    root_ref = sbom["metadata"]["component"]["bom-ref"]
    depends_on = _union_dependencies(sbom.get("dependencies", []))

    def kept_dependencies(ref):
        result, pending, seen = [], list(depends_on.get(ref, [])), set()
        while pending:
            dep = pending.pop(0)
            if dep in seen:
                continue
            seen.add(dep)
            if dep in kept_refs:
                result.append(dep)
            else:
                pending.extend(depends_on.get(dep, []))
        return result

    dependencies = []
    for ref in depends_on:
        if ref != root_ref and ref not in kept_refs:
            continue
        entry = {"ref": ref}
        deps = kept_dependencies(ref)
        if deps:
            entry["dependsOn"] = deps
        dependencies.append(entry)
    result = {k: v for k, v in sbom.items() if k not in ("components", "dependencies")}
    if kept:
        result["components"] = kept
    if dependencies:
        result["dependencies"] = dependencies
    return result


def _spdx_licenses(licenses: list) -> list:
    """Conan only recognizes single SPDX license ids, and writes anything else
    (e.g. clang's "Apache-2.0 WITH LLVM-exception") as a free-text license
    name. CycloneDX has a dedicated form for an SPDX expression, which is what
    license scanners check; it must be the licenses array's only entry."""
    if len(licenses) == 1:
        name = licenses[0].get("license", {}).get("name", "")
        if _SPDX_OPERATORS.search(name):
            return [{"expression": name}]
    return licenses


def _should_skip_scope(require) -> bool:
    """Only host-context dependencies (any depth) and directly-required
    build-context (tool_requires) dependencies are in scope: a transitive
    tool_requires of some other dependency (e.g. a package's own cmake/ninja)
    is skipped since we aren't rebuilding that dependency ourselves. clang is
    further restricted to direct-only regardless of context: a clang pulled
    in transitively (e.g. osl's runtime requires() on clang for its JIT
    backend) is expected to collide with whatever clang version is already
    baked into the base image, so it must never be auto-deployed here --
    only when a Dockerfile's own conanfile.txt lists it directly (ci-common).
    """
    if require.build and not require.direct:
        return True
    if require.ref.name == "clang" and not require.direct:
        return True
    return False


def _is_system_wrapper(dep) -> bool:
    """A recipe that only points at OS-provided files: its package has
    nothing in it. Decided from the package itself, not its version: wrappers
    (the "system" class in conan-center-index-upstream.json, plus CCI's own
    opengl/glu system recipes) are pinned at either "system" or the distro
    package's version (e.g. openssl/3.5.8), and full recipes can check it."""
    return dep.package_folder is not None and not any(
        entry.is_dir() for entry in os.scandir(dep.package_folder)
    )


def _should_skip(dep, exclude_name) -> bool:
    if dep.package_folder is None:
        return True
    if exclude_name and dep.ref.name == exclude_name:
        return True
    if str(dep.ref.version) == "system":
        return True
    return _is_system_wrapper(dep)


class _DeployReport:
    """Accumulates non-fatal warnings (destination already matched, kept
    as-is) and fatal errors (destination collision with different content,
    existing content kept, colliding copy skipped) encountered while
    deploying, so a real collision degrades gracefully instead of crashing
    conan install outright."""

    def __init__(self):
        self.warnings = []
        self.errors = []

    def add_warning(self, package_name, dest_path, reason) -> None:
        self.warnings.append(f"{package_name}: {dest_path}: {reason}")

    def add_error(self, package_name, dest_path, reason) -> None:
        self.errors.append(f"{package_name}: {dest_path}: {reason}")

    def print_report(self) -> None:
        print(
            f"aswf_deploy: {len(self.warnings)} warning(s), {len(self.errors)} error(s)"
        )
        if self.warnings:
            print("aswf_deploy: WARNINGS (destination already matched, kept as-is):")
            for line in self.warnings:
                print(f"  - {line}")
        if self.errors:
            print(
                "aswf_deploy: ERRORS (destination collision, existing content "
                "kept, colliding copy skipped):"
            )
            for line in self.errors:
                print(f"  - {line}")

    def raise_if_errors(self) -> None:
        if self.errors:
            raise RuntimeError(
                f"aswf_deploy: {len(self.errors)} unresolved file/symlink "
                "collision(s); see report above"
            )


def _cache_root(conan_home: str) -> str:
    """Mirrors how Conan itself resolves the cache storage root
    (conan/internal/cache/cache.py): core.cache:storage_path from
    global.conf, falling back to <CONAN_HOME>/p. conanfile.conf can't be
    used for this: core.* values are stripped before reaching any
    conanfile/deployer (Conf.filter_core())."""
    global_conf_path = os.path.join(conan_home, "global.conf")
    if os.path.isfile(global_conf_path):
        with open(global_conf_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                if sep and key.strip() == "core.cache:storage_path":
                    return value.strip()
    return os.path.join(conan_home, "p")


def _deploy_package(dep, output_folder, cache_root, symlinks, report) -> None:
    _copy_package_tree(dep, output_folder, symlinks, report)
    _deploy_licenses(dep, output_folder)
    _rewrite_cache_paths(dep, output_folder, cache_root)
    hook = _PACKAGE_HOOKS.get(dep.ref.name)
    if hook is not None:
        hook(dep, output_folder, cache_root)


def _copy_package_tree(dep, output_folder, symlinks, report) -> None:
    package_folder = dep.package_folder
    skip_at_root = {_LICENSES_DIR_NAME} | _CONAN_METADATA_FILES

    def _ignore(directory, names):
        if os.path.normpath(directory) == os.path.normpath(package_folder):
            return {n for n in names if n in skip_at_root}
        return set()

    copy_function = functools.partial(_copy2_or_skip, dep=dep, report=report)

    # dirs_exist_ok=True merges into whatever other packages already placed
    # in output_folder (e.g. shared include/, lib/, bin/) instead of the old
    # full_deploy-then-rsync two-step.
    try:
        shutil.copytree(
            package_folder,
            output_folder,
            dirs_exist_ok=True,
            symlinks=symlinks,
            ignore=_ignore,
            copy_function=copy_function,
        )
    except shutil.Error as exc:
        # symlink sources bypass copy_function entirely (copytree creates
        # them via a direct os.symlink() call), so a pre-existing
        # destination can only surface here, as an aggregated failure list,
        # rather than proactively like the regular-file case above.
        unhandled = [
            entry
            for entry in exc.args[0]
            if not _classify_symlink_collision(dep, entry[0], entry[1], report)
        ]
        if unhandled:
            raise shutil.Error(unhandled) from exc


def _classify_symlink_collision(dep, srcname, dstname, report) -> bool:
    """Reclassifies one (srcname, dstname, message) entry from a caught
    shutil.Error as a symlink-destination-already-exists collision, if
    that's what it actually is. Returns True if handled (recorded into
    report), False if unrelated and must propagate (e.g. a genuine
    permissions error) -- the original exception object is gone by the
    time shutil.copytree aggregates its errors, so this has to be done via
    filesystem introspection rather than inspecting the caught exception."""
    if not os.path.islink(srcname):
        return False
    if not os.path.lexists(dstname):
        return False
    if os.path.islink(dstname):
        existing_target = os.readlink(dstname)
        new_target = os.readlink(srcname)
        if existing_target == new_target:
            report.add_warning(
                dep.ref.name,
                dstname,
                f"symlink already exists pointing at same target {existing_target!r}, kept",
            )
        else:
            report.add_error(
                dep.ref.name,
                dstname,
                f"symlink collision: existing target {existing_target!r} != "
                f"this package's target {new_target!r}, kept existing (first wins)",
            )
        return True
    kind = "directory" if os.path.isdir(dstname) else "file"
    new_target = os.readlink(srcname)
    report.add_error(
        dep.ref.name,
        dstname,
        f"destination exists as a {kind} (not a symlink) where this package "
        f"would place a symlink to {new_target!r}, kept existing",
    )
    return True


def _copy2_or_skip(srcname, dstname, *, dep, report) -> None:
    if os.path.lexists(dstname) and not os.path.isdir(dstname):
        existing_md5 = _md5_of_file(dstname)
        new_md5 = _md5_of_file(srcname)
        if existing_md5 == new_md5:
            report.add_warning(
                dep.ref.name,
                dstname,
                "destination file already exists with identical content (md5 match), skipped copy",
            )
        else:
            report.add_error(
                dep.ref.name,
                dstname,
                f"destination file already exists with DIFFERENT content "
                f"(md5 {existing_md5} != {new_md5}), kept existing, skipped copy",
            )
        return
    shutil.copy2(srcname, dstname)


def _md5_of_file(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_MD5_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deploy_licenses(dep, output_folder) -> None:
    licenses_dir = os.path.join(dep.package_folder, _LICENSES_DIR_NAME)
    if not os.path.isdir(licenses_dir):
        return
    output_licenses_dir = os.path.join(output_folder, _LICENSES_DIR_NAME)
    # Transitional: some recipes still have a local ASWF modification that
    # nests their own licenses/<name>/ (to avoid collisions before this
    # deployer existed). Once a recipe already does that, deploy as-is
    # instead of adding a second <name> level; only recipes that don't
    # self-nest need us to add the namespacing ourselves.
    if os.path.isdir(os.path.join(licenses_dir, dep.ref.name)):
        dst = output_licenses_dir
    else:
        dst = os.path.join(output_licenses_dir, dep.ref.name)
    shutil.copytree(licenses_dir, dst, dirs_exist_ok=True)


def _cache_path_pattern(cache_root: str) -> str:
    """Matches an absolute Conan-cache package-folder path baked into some
    installed files at package-build/upload time. Its shape depends on how
    the package ended up in the cache: <cache_root>/b/<hash>/p for a package
    built locally in this cache (PkgCache.create_build_pkg_layout(), a
    random-salted hash under a literal 'b' build-tracking folder), or
    <cache_root>/<hash>/p for one downloaded as a prebuilt binary from a
    remote (PkgCache.create_pkg_layout(), a deterministic hash with no 'b'
    segment at all) -- so the 'b/' component has to be optional to catch
    both. Either way this is a different shape than the current
    dep.package_folder, so it can't be derived from that path directly."""
    return re.escape(cache_root) + r"(?:/b)?/[^/]+/p"


def _iter_destination_files(dep, output_folder, name_pattern=None):
    """Discovers files by walking the (read-only) source package_folder,
    yields the corresponding already-copied destination path. Never mutates
    the Conan cache itself, and never re-scans files copied by a different
    package."""
    package_folder = dep.package_folder
    for dirpath, _dirnames, filenames in os.walk(package_folder):
        rel_dir = os.path.relpath(dirpath, package_folder)
        for filename in filenames:
            if name_pattern and not fnmatch.fnmatch(filename, name_pattern):
                continue
            dest_dir = (
                output_folder
                if rel_dir == "."
                else os.path.join(output_folder, rel_dir)
            )
            yield os.path.join(dest_dir, filename)


def _rewrite_file_pattern(path: str, pattern_re, replacement: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        return
    new_content = pattern_re.sub(replacement, content)
    if new_content != content:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)


def _rewrite_cache_paths(dep, output_folder, cache_root) -> None:
    pattern_re = re.compile(_cache_path_pattern(cache_root))
    for dest_path in _iter_destination_files(dep, output_folder, "*.cmake"):
        _rewrite_file_pattern(dest_path, pattern_re, output_folder)


def _shebang_pattern(cache_root: str):
    # 'b/' is optional here too -- see _cache_path_pattern().
    return re.compile(r"^#!" + re.escape(cache_root) + r"(?:/b)?/[^/]+/p/bin/(\S+)\s*$")


def _fix_shebang(path: str, shebang_re) -> None:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return
    if not lines:
        return
    match = shebang_re.match(lines[0].rstrip("\n"))
    if not match:
        return
    lines[0] = f"#!/usr/bin/env {match.group(1)}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _cpython_build_dir_pattern(cache_root: str) -> str:
    """cpython also bakes in its own *build* directory (never deployed,
    unlike its package folder) -- <cache_root>(/b)?/<hash>/b/build-<type> --
    into a handful of Makefile/_sysconfigdata variables (RUNSHARED,
    TESTPYTHON, TESTRUNNER, COVERAGE_INFO, COVERAGE_REPORT, abs_builddir)
    used only by cpython's own 'make test'/'make coverage' targets, plus the
    archival CONFIG_ARGS record of its own configure invocation (which
    itself embeds a PKG_CONFIG_PATH pointing there). None of these are read
    by sysconfig/distutils when compiling a downstream C extension, and
    there's no deployed equivalent to redirect them to, so they're stripped
    (see _fixup_cpython) rather than rewritten like the package-folder
    pattern above."""
    return re.escape(cache_root) + r"(?:/b)?/[^/]+/b/build-[a-z]+(?:/[^\s:'\"]*)?"


def _fixup_cpython(dep, output_folder, cache_root) -> None:
    # _sysconfigdata__*.py and the installed build Makefile (read by
    # sysconfig/distutils, e.g. when pip needs to compile a C extension)
    # both bake in absolute build-time cache paths
    pattern_re = re.compile(_cache_path_pattern(cache_root))
    build_dir_re = re.compile(_cpython_build_dir_pattern(cache_root))
    for name_pattern in ("_sysconfigdata__*.py", "Makefile"):
        for dest_path in _iter_destination_files(dep, output_folder, name_pattern):
            _rewrite_file_pattern(dest_path, pattern_re, output_folder)
            _rewrite_file_pattern(dest_path, build_dir_re, "")

    # shebangs: any lib/**/*.py, and any bin/* file, that points at the
    # cache-cached python interpreter
    shebang_re = _shebang_pattern(cache_root)
    for dest_path in _iter_destination_files(dep, output_folder, "*.py"):
        rel = os.path.relpath(dest_path, output_folder)
        if rel.split(os.sep)[0] == "lib":
            _fix_shebang(dest_path, shebang_re)

    source_bin_dir = os.path.join(dep.package_folder, "bin")
    if os.path.isdir(source_bin_dir):
        dest_bin_dir = os.path.join(output_folder, "bin")
        for entry in os.scandir(source_bin_dir):
            if entry.is_file():
                _fix_shebang(os.path.join(dest_bin_dir, entry.name), shebang_re)


def _fixup_pybind11(
    dep, output_folder, cache_root
) -> None:  # pylint: disable=unused-argument
    cmake_dir = os.path.join(output_folder, "lib", "cmake", "pybind11")
    stashed = os.path.join(cmake_dir, "pybind11Common.cmake_NO_CONAN")
    target = os.path.join(cmake_dir, "pybind11Common.cmake")
    if os.path.isfile(stashed):
        os.replace(stashed, target)


def _fixup_qt(dep, output_folder, cache_root) -> None:
    pattern_re = re.compile(_cache_path_pattern(cache_root))
    for name in ("qt-cmake", "qt-cmake-create"):
        path = os.path.join(output_folder, "bin", name)
        if os.path.isfile(path):
            _rewrite_file_pattern(path, pattern_re, output_folder)

    # mkspecs/qmodule.pri and mkspecs/modules/qt_lib_*_private.pri record
    # each third-party dependency's own lib/include dirs (QMAKE_LIBDIR_ZLIB,
    # QMAKE_INCDIR_FREETYPE, etc.) as absolute paths into whatever package
    # cache folder qmake found them in at build time -- not just Qt's own.
    for dest_path in _iter_destination_files(dep, output_folder, "*.pri"):
        _rewrite_file_pattern(dest_path, pattern_re, output_folder)


def _fixup_pkgconfig(dep, output_folder, cache_root) -> None:
    # Generated *.pc files bake in absolute build-time cache paths for
    # prefix/bindir/libdir/includedir/etc, whether from the package's own
    # cache folder (lensfun) or another dependency's (shiboken6.pc's
    # python_interpreter/python_include_dir point at cpython's).
    pattern_re = re.compile(_cache_path_pattern(cache_root))
    for dest_path in _iter_destination_files(dep, output_folder, "*.pc"):
        _rewrite_file_pattern(dest_path, pattern_re, output_folder)


def _pyside_install_dir_pattern(cache_root: str) -> str:
    """PySide/shiboken's own build tooling (not Conan's normal package
    layout) stages its output at
    <cache_root>/<hash>/s/pyside_src/build/<venv_name>/install
    (conanfile.py's _installDir property) before package() copies it
    verbatim into self.package_folder (copy(pattern="*", src=self._installDir,
    dst=self.package_folder)) -- so unlike a transient build-only directory,
    this really is a 1:1 stand-in for the deployed package folder and can be
    substituted with output_folder the same way."""
    return re.escape(cache_root) + r"/[^/]+/s/pyside_src/build/[^/]+/install"


def _fixup_pyside(dep, output_folder, cache_root) -> None:
    _fixup_pkgconfig(dep, output_folder, cache_root)
    install_dir_re = re.compile(_pyside_install_dir_pattern(cache_root))
    for dest_path in _iter_destination_files(dep, output_folder, "*.pc"):
        _rewrite_file_pattern(dest_path, install_dir_re, output_folder)


_PACKAGE_HOOKS.update(
    {
        "cpython": _fixup_cpython,
        "pybind11": _fixup_pybind11,
        "qt": _fixup_qt,
        "lensfun": _fixup_pkgconfig,
        "pyside": _fixup_pyside,
    }
)
