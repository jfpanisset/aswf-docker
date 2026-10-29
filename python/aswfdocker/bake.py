# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Shared helpers for constructing and serializing `docker buildx bake`
invocations. Used by both builder.py (real image/package builds) and
buildorder.py (the read-only Conan dependency-graph grapher) -- a leaf
module with no dependency on either, so neither creates a circular import
by sharing this.

Execution itself (dry-run support, live vs captured output, error
handling) stays with each caller: builder.py's bake calls are interactive
commands a human watches build real artifacts, while buildorder.py's is a
headless metadata query that always runs and inspects stderr on failure.
Only the part that's actually the same -- how a bake command line is put
together -- lives here.
"""

import json
import typing


def write_bake_jsonfile(path: str, bake_dict: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bake_dict, f, indent=4, sort_keys=True)


def bake_command(
    bake_jsonfile: str,
    build_targets: typing.Sequence[str],
    overrides: typing.Mapping[str, str],
    progress: str,
    override_scope: str = "*",
) -> typing.List[str]:
    """Builds a `docker buildx bake` argv list.

    `overrides` is a {field: value} mapping applied as
    `--set=<override_scope>.<field>=<value>` (in iteration order), e.g.
    {"output": "type=cacheonly", "target": "ci-conan-package-builder"}.
    `override_scope` defaults to "*" (every target in the bake file);
    pass a specific target name to scope it to just that one.

    `build_targets` is what gets passed positionally -- the bake group(s)
    or target(s) to actually build. An empty sequence builds whatever the
    bake file's own "default" group names.
    """
    cmd = ["docker", "buildx", "bake", "-f", bake_jsonfile]
    for field, value in overrides.items():
        cmd.append(f"--set={override_scope}.{field}={value}")
    cmd += ["--progress", progress]
    cmd.extend(build_targets)
    return cmd
