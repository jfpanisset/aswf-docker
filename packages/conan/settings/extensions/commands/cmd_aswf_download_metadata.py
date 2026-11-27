# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ASWF Conan custom command: `conan aswf-download-metadata <graph.json>
--remote <remote> --metadata <pattern>` downloads, from the remote, the
packages a graph would download, together with their metadata.

`conan install` never downloads package metadata, so an image built from
packages downloaded from Artifactory would get an SBOM without the build
provenance hook_aswf_provenance.py records in each package's metadata
(aswf_provenance.json). Run between `conan graph info --format=json` and
`conan install`, this downloads those packages with their metadata first,
leaving `conan install` to find them in the cache.

The built-in `conan download --list`, from `conan list --graph`, can't be
used for this: its package list for a remote holds every package of the
graph available there, including those `conan install` skips (e.g. tool
requirements of packages it doesn't build), which would be downloaded for
nothing. This only downloads the binaries the graph marks for download.

Like aswf_deploy.py, this module must stay import-free of the `aswfdocker`
pip package: it runs inside the build container, where only Conan is
installed.

Informational output goes to stderr (ConanOutput), never stdout.
"""

import json
import typing

from conan.api.model import PkgReference, RecipeReference
from conan.api.output import ConanOutput
from conan.cli.command import conan_command

# The graph's "binary" status of packages `conan install` downloads.
_BINARY_DOWNLOAD = "Download"


def packages_to_download(graph: dict, remote_name: str) -> typing.List[PkgReference]:
    """The packages (with their recipe and package revisions) that a
    `conan graph info --format=json` graph would download from
    `remote_name`."""
    prefs = []
    for node in graph["graph"]["nodes"].values():
        if node.get("binary") != _BINARY_DOWNLOAD:
            continue
        if node.get("binary_remote") != remote_name:
            continue
        ref = RecipeReference.loads(node["ref"])
        ref.timestamp = node.get("rrev_timestamp")
        prefs.append(
            PkgReference(
                ref,
                node["package_id"],
                node.get("prev"),
                node.get("prev_timestamp"),
            )
        )
    return prefs


def download_with_metadata(conan_api, prefs, remote, metadata) -> None:
    """Downloads each package (and its recipe, if missing from the cache)
    with the metadata files matching the `metadata` patterns. For a package
    already in the cache, only its metadata is downloaded."""
    for pref in prefs:
        conan_api.download.recipe(pref.ref, remote)
        conan_api.download.package(pref, remote, metadata)
    ConanOutput().info(
        f"Downloaded {len(prefs)} package(s) with their {', '.join(metadata)} "
        f"metadata from {remote.name}"
    )


@conan_command(group="ASWF")
def aswf_download_metadata(conan_api, parser, *args):
    """
    Download the packages a graph would download, with their metadata.
    """
    parser.add_argument(
        "graph", help="Graph json file, from `conan graph info --format=json`"
    )
    parser.add_argument(
        "-r",
        "--remote",
        required=True,
        help="The remote to download from",
    )
    parser.add_argument(
        "-m",
        "--metadata",
        action="append",
        required=True,
        help="Download the metadata matching the pattern",
    )
    args = parser.parse_args(*args)
    with open(args.graph, "r", encoding="utf-8") as f:
        graph = json.load(f)
    remote = conan_api.remotes.get(args.remote)
    download_with_metadata(
        conan_api,
        packages_to_download(graph, remote.name),
        remote,
        args.metadata,
    )
