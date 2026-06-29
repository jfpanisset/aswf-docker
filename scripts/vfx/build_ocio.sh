#!/usr/bin/env bash
# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -ex

mkdir ocio
cd ocio

if [[ ! -f "$DOWNLOADS_DIR/ocio-${ASWF_OPENCOLORIO_VERSION}.tar.gz" ]]; then
    curl --location "https://github.com/AcademySoftwareFoundation/OpenColorIO/archive/v${ASWF_OPENCOLORIO_VERSION}.tar.gz" -o "$DOWNLOADS_DIR/ocio-${ASWF_OPENCOLORIO_VERSION}.tar.gz"
fi
tar -zxf "$DOWNLOADS_DIR/ocio-${ASWF_OPENCOLORIO_VERSION}.tar.gz"
cd "OpenColorIO-${ASWF_OPENCOLORIO_VERSION}"

if [[ $ASWF_OPENCOLORIO_VERSION == 2.2.1 ]]; then

cat << 'EOF' | patch -p1
diff --git a/share/cmake/modules/Findyaml-cpp.cmake b/share/cmake/modules/Findyaml-cpp.cmake
index d99dd79ac..bfda2778a 100644
--- a/share/cmake/modules/Findyaml-cpp.cmake
+++ b/share/cmake/modules/Findyaml-cpp.cmake
@@ -43,7 +43,8 @@
     endif()

     if(yaml-cpp_FOUND)
-        get_target_property(yaml-cpp_LIBRARY yaml-cpp LOCATION)
+        get_target_property(yaml-cpp_INCLUDE_DIR yaml-cpp::yaml-cpp INTERFACE_INCLUDE_DIRECTORIES)
+        get_target_property(yaml-cpp_LIBRARY yaml-cpp::yaml-cpp IMPORTED_LOCATION_RELEASE)
     else()

         # As yaml-cpp-config.cmake search fails, search an installed library
diff --git a/share/cmake/modules/FindExtPackages.cmake b/share/cmake/modules/FindExtPackages.cmake
index d99dd79ac..bfda2778a 100644
--- FindExtPackages.cmake
+++ FindExtPackages.cmake
@@ -178,7 +178,7 @@
     endif()

     # Python
-    find_package(Python ${OCIO_PYTHON_VERSION} REQUIRED
+    find_package(Python ${OCIO_PYTHON_VERSION} EXACT REQUIRED
                  COMPONENTS ${_Python_COMPONENTS})

     if(OCIO_BUILD_PYTHON)
diff --git a/src/OpenColorIO/OCIOZArchive.cpp b/src/OpenColorIO/OCIOZArchive.cpp
index 17e188d..91af0ec 100755
--- a/src/OpenColorIO/OCIOZArchive.cpp
+++ b/src/OpenColorIO/OCIOZArchive.cpp
@@ -24,7 +24,6 @@
 #include "mz_strm_mem.h"
 #include "mz_strm_os.h"
 #include "mz_strm_split.h"
-#include "mz_strm_zlib.h"
 #include "mz_zip.h"
 #include "mz_zip_rw.h"

@@ -225,7 +224,11 @@
     std::string configStr = ss.str();

     // Write zip to memory stream.
+#if MZ_VERSION_BUILD >= 040000
+    write_mem_stream = mz_stream_mem_create();
+#else
     mz_stream_mem_create(&write_mem_stream);
+#endif
     mz_stream_mem_set_grow_size(write_mem_stream, 128 * 1024);
     mz_stream_open(write_mem_stream, NULL, MZ_OPEN_MODE_CREATE);

@@ -237,7 +240,11 @@
     options.compress_level  = ArchiveCompressionLevels::BEST;

     // Create the writer handle.
+#if MZ_VERSION_BUILD >= 040000
+    archiver = mz_zip_writer_create();
+#else
     mz_zip_writer_create(&archiver);
+#endif

     // Archive options.
     // Compression method
@@ -332,7 +339,11 @@
     std::string outputDestination = pystring::os::path::normpath(destination);

     // Create zip reader.
+#if MZ_VERSION_BUILD >= 040000
+    extracter = mz_zip_reader_create();
+#else
     mz_zip_reader_create(&extracter);
+#endif

     MinizipNgHandlerGuard extracterGuard(extracter, false, false);

@@ -450,7 +461,11 @@
     std::vector<uint8_t> buffer;

     // Create the reader object.
+#if MZ_VERSION_BUILD >= 040000
+    reader = mz_zip_reader_create();
+#else
     mz_zip_reader_create(&reader);
+#endif

     MinizipNgHandlerGuard extracterGuard(reader, false, true);

@@ -510,7 +525,11 @@
     void *reader = NULL;

     // Create the reader object.
+#if MZ_VERSION_BUILD >= 040000
+    reader = mz_zip_reader_create();
+#else
     mz_zip_reader_create(&reader);
+#endif

     MinizipNgHandlerGuard extracterGuard(reader, false, false);

diff --git a/src/apps/ocioarchive/main.cpp b/src/apps/ocioarchive/main.cpp
index 17e188d..91af0ec 100755
--- a/src/apps/ocioarchive/main.cpp
+++ b/src/apps/ocioarchive/main.cpp
@@ -235,7 +235,11 @@
         }

         std::string path = args[0];
+#if MZ_VERSION_BUILD >= 040000
+        reader = mz_zip_reader_create();
+#else
         mz_zip_reader_create(&reader);
+#endif
         struct tm tmu_date;

         err = mz_zip_reader_open_file(reader, path.c_str());
EOF

fi

if [[ $ASWF_OPENCOLORIO_VERSION == 2.3.2 ]]; then

cat << 'EOF' | patch -p1
diff --git a/share/cmake/modules/FindExtPackages.cmake b/share/cmake/modules/FindExtPackages.cmake
index d99dd79ac..bfda2778a 100644
--- a/share/cmake/modules/FindExtPackages.cmake
+++ b/share/cmake/modules/FindExtPackages.cmake
@@ -170,6 +170,7 @@
     ocio_handle_dependency(  Python REQUIRED
                              COMPONENTS ${_Python_COMPONENTS}
                              MIN_VERSION ${OCIO_PYTHON_VERSION}
+                             MAX_VERSION ${OCIO_PYTHON_VERSION} EXACT
                              RECOMMENDED_VERSION ${OCIO_PYTHON_VERSION}
                              RECOMMENDED_VERSION_REASON "Latest version tested with OCIO")

EOF

fi

if [[ $ASWF_OPENCOLORIO_VERSION == 2.4.2 ]]; then

cat << 'EOF' | patch -p1
diff --git a/share/cmake/modules/FindExtPackages.cmake b/share/cmake/modules/FindExtPackages.cmake
index d99dd79ac..bfda2778a 100644
--- a/share/cmake/modules/FindExtPackages.cmake
+++ b/share/cmake/modules/FindExtPackages.cmake
@@ -170,6 +170,7 @@
     ocio_handle_dependency(  Python REQUIRED
                              COMPONENTS ${_Python_COMPONENTS}
                              MIN_VERSION ${OCIO_PYTHON_VERSION}
+                             MAX_VERSION ${OCIO_PYTHON_VERSION} EXACT
                              RECOMMENDED_VERSION ${OCIO_PYTHON_VERSION}
                              RECOMMENDED_VERSION_REASON "Latest version tested with OCIO")

EOF

fi

# We only support building against bundled libOpenImageIO when the version matches the VFX Platform year
# since libOpenImageIO itself is linked against libOpenColorIO.
OCIO_USE_OIIO=OFF
case "${ASWF_VFXPLATFORM_VERSION}:${ASWF_OPENCOLORIO_VERSION}" in
    2023:2.2.*|2024:2.3.*|2025:2.4.*|2026:2.5.*|2027:2.5.*) OCIO_USE_OIIO=ON ;;
esac

mkdir build
cd build
cmake \
    -DCMAKE_INSTALL_PREFIX="${ASWF_INSTALL_PREFIX}" \
    -DOCIO_USE_OIIO_FOR_APPS="${OCIO_USE_OIIO}" \
    -DOCIO_BUILD_STATIC=OFF \
    -DOCIO_BUILD_APPS=ON \
    -DOCIO_BUILD_NUKE=OFF \
    -DOCIO_BUILD_PYTHON=ON \
    -DOCIO_PYTHON_VERSION="${ASWF_CONAN_PYTHON_VERSION}" \
    -DOCIO_INSTALL_EXT_PACKAGES=MISSING \
    -Dpybind11_ROOT="${ASWF_INSTALL_PREFIX}" \
    -DGLEW_ROOT="${ASWF_INSTALL_PREFIX}" \
    -DCMAKE_CXX_FLAGS="-Wno-error=unused-function -Wno-error=deprecated-declarations"\
    ..
cmake --build . -j$(nproc)
cmake --install .

# As per the OCIO Slack #dev channel, we no longer need to download  OCIO configs
# separately, the 2.x configs are now built-in to the library.
# Legacy 1.x configs: https://github.com/colour-science/OpenColorIO-Configs
# 2.x config source: https://github.com/AcademySoftwareFoundation/OpenColorIO-Config-ACES

cd ../..
rm -rf ocio
