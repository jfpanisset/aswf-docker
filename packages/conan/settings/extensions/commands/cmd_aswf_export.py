# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ASWF Conan custom command: `conan aswf-export '<json>'` exports many
recipes in a single Conan process.

Running `conan export` once per recipe costs a Python interpreter start and
a full Conan load each time (~0.4s), which dominated buildorder.py's
dependency-graph run when exporting every recipe a profile can reach. This
does the same per-recipe work as the built-in `conan export` command
(conan/cli/commands/export.py) -- conanfile path resolution, implicit
lockfile lookup, remotes for python_requires -- just in a loop.

Like aswf_deploy.py, this module must stay import-free of the `aswfdocker`
pip package: it runs inside the build container, where only Conan is
installed.

Informational output goes to stderr (ConanOutput), never stdout, so a
caller can still redirect stdout of a following command to a file.
"""

import json
import os

from conan.api.output import ConanOutput
from conan.cli.command import conan_command


def export_all(conan_api, exports, cwd, remotes):
    """Exports each (path, name, version, user, channel) entry, in order.
    `remotes` is only consulted for python_requires, as in `conan export`.

    Returns the exported RecipeReferences.
    """
    refs = []
    for path, name, version, user, channel in exports:
        conanfile_path = conan_api.local.get_conanfile_path(path, cwd, py=True)
        lockfile = conan_api.lockfile.get_lockfile(
            lockfile=None, conanfile_path=conanfile_path, cwd=cwd, partial=False
        )
        ref, _ = conan_api.export.export(
            path=conanfile_path,
            name=name,
            version=version,
            user=user,
            channel=channel,
            lockfile=lockfile,
            remotes=remotes,
        )
        ConanOutput().info(f"Exported: {ref.repr_notime()}")
        refs.append(ref)
    return refs


@conan_command(group="ASWF")
def aswf_export(conan_api, parser, *args):
    """
    Export many recipes to the Conan cache in one process.
    """
    parser.add_argument(
        "exports",
        help="JSON list of [path, name, version, user, channel] entries",
    )
    # Same remote options as the built-in `conan export`.
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-r",
        "--remote",
        action="append",
        default=None,
        help="Look in the specified remote or remotes server",
    )
    group.add_argument(
        "-nr",
        "--no-remote",
        action="store_true",
        help="Do not use remote, resolve exclusively in the cache",
    )
    args = parser.parse_args(*args)
    remotes = [] if args.no_remote else conan_api.remotes.list(args.remote)
    export_all(conan_api, json.loads(args.exports), os.getcwd(), remotes)
