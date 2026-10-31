# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ASWF Conan hook: records which git state each package was built from.

Writes aswf_provenance.json into the package's metadata folder after
package(). Conan keeps package metadata outside both the package_id and the
package's content hash, and uploads/downloads it with the package, so this
never causes a rebuild and travels through Artifactory with the binary.
aswf_deploy.py reads it back to annotate the SBOM it writes into each image.

The values come from build ARGs packages/common/Dockerfile exposes to the
`conan create` RUN (set by aswfdocker's utils.git_provenance()). Outside an
aswfdocker package build ASWF_GIT_SHA is unset and nothing is written.

Like aswf_deploy.py, this module must stay import-free of the `aswfdocker`
pip package: it runs inside the build container, where only Conan is
installed.
"""

import json
import os

PROVENANCE_FILE = "aswf_provenance.json"


def post_package(conanfile):
    sha = os.environ.get("ASWF_GIT_SHA")
    if not sha:
        return
    provenance = {
        "git_sha": sha,
        "git_dirty": os.environ.get("ASWF_GIT_DIRTY") == "true",
    }
    diff_hash = os.environ.get("ASWF_GIT_DIFF_HASH")
    if diff_hash:
        provenance["git_diff_hash"] = diff_hash
    folder = conanfile.package_metadata_folder
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, PROVENANCE_FILE), "w", encoding="utf-8") as f:
        json.dump(provenance, f, indent=2, sort_keys=True)
        f.write("\n")
    conanfile.output.info(f"Recorded build provenance: {provenance}")
