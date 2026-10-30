#!/usr/bin/env bash
# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -ex

python3 -m ensurepip --upgrade

# There is a package called python3-html5lib through yum/dnf but it installs
# for the system Python - which is not the same one that it used by downstream projects.
python3 -m pip install html5lib
