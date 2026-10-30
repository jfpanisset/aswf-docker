/*
# Copyright (c) Contributors to the conan-center-index Project. All rights reserved.
# Copyright (c) Contributors to the aswf-docker Project. All rights reserved.
# SPDX-License-Identifier: MIT
*/
#include <stdio.h>

#ifdef __APPLE__
# include <OpenGL/glu.h>
#else
# ifdef _WIN32
#  include <windows.h>
# endif
# include <GL/glu.h>
#endif

int main()
{
    printf("GLU %s\n", gluGetString(GLU_VERSION));

    return 0;
}
