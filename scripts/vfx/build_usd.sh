#!/usr/bin/env bash
# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -ex

pip3 install jinja2 PyOpenGL

if [ ! -f "$DOWNLOADS_DIR/usd-${ASWF_USD_VERSION}.tar.gz" ]; then
    # For VFX Platform 2025, we need a somewhat newer release of USD than the latest 25.05a tag to
    # get MaterialX 1.39.3 compatibility.
    if [[ $ASWF_USD_VERSION == 25.02a.eae7e67 ]]; then
        curl --location "https://github.com/PixarAnimationStudios/OpenUSD/archive/eae7e678473eb78794a3a27287ff121af322d583.tar.gz" -o "$DOWNLOADS_DIR/usd-${ASWF_USD_VERSION}.tar.gz"
    else
        curl --location "https://github.com/PixarAnimationStudios/OpenUSD/archive/v${ASWF_USD_VERSION}.tar.gz" -o "$DOWNLOADS_DIR/usd-${ASWF_USD_VERSION}.tar.gz"
    fi
fi

tar -zxf "$DOWNLOADS_DIR/usd-${ASWF_USD_VERSION}.tar.gz"
if [[ $ASWF_USD_VERSION == 25.02a.eae7e67 ]]; then
mv "OpenUSD-eae7e678473eb78794a3a27287ff121af322d583" "OpenUSD-${ASWF_USD_VERSION}"
fi
cd "OpenUSD-${ASWF_USD_VERSION}"

if [[ $ASWF_USD_VERSION == 23.05 && $ASWF_MATERIALX_VERSION == 1.38.7 ]]; then
    # Apply patch from https://github.com/PixarAnimationStudios/USD/pull/2402
    curl --location "https://patch-diff.githubusercontent.com/raw/PixarAnimationStudios/USD/pull/2402.diff" | patch -p1
fi 

if [[ $ASWF_USD_VERSION == 24.08 && $ASWF_MATERIALX_VERSION == 1.39.1 ]]; then
    # Apply patch from https://github.com/PixarAnimationStudios/USD/pull/3159
    curl --location "https://patch-diff.githubusercontent.com/raw/PixarAnimationStudios/OpenUSD/pull/3159.diff" | patch -p1
fi

if [[ $ASWF_USD_VERSION == 19.* ]]; then
     VT_SRC_FOLDER=base/lib/vt
else
     VT_SRC_FOLDER=base/vt
fi

mkdir build
cd build

if [[ $ASWF_CPYTHON_VERSION == 2.7* ]]; then
    USD_EXTRA_ARGS=
else
    USD_EXTRA_ARGS=-DPXR_USE_PYTHON_3=ON
fi

# Our current PySide6 Conan build doesn't create
# the pyside6- prefixed # binaries.

if [[ $ASWF_PYSIDE_VERSION == 6* ]]; then
    USD_UIC_PATH=-DPYSIDEUICBINARY=/usr/local/bin/uic
else
    USD_UIC_PPATH=
fi

cmake \
     -DCMAKE_INSTALL_PREFIX="${ASWF_INSTALL_PREFIX}" \
     -DOPENEXR_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DCPPUNIT_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DBLOSC_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DTBB_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DILMBASE_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DGLEW_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DMATERIALX_LOCATION="${ASWF_INSTALL_PREFIX}" \
     -DMATERIALX_STDLIB_DIR="${ASWF_INSTALL_PREFIX}" \
     -DPXR_ENABLE_MATERIALX_SUPPORT=ON \
     -DPXR_BUILD_TESTS=OFF \
     -DUSD_ROOT_DIR="${ASWF_INSTALL_PREFIX}" \
     -DPXR_BUILD_ALEMBIC_PLUGIN=ON \
     -DPXR_BUILD_MAYA_PLUGIN=FALSE \
     ${USD_UIC_PATH} \
     ${USD_EXTRA_ARGS} \
     ..

make -j$(nproc)
make install

cd ../..

rm -rf "USD-${ASWF_USD_VERSION}"
