cmake_minimum_required(VERSION 3.20)
project(synth_rift_vessel)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# ── Dependencies ──────────────────────────────────────────────────────────────
# Requires CommonLibSSE (NG) — install via vcpkg or FetchContent:
#
#   vcpkg install commonlibsse-ng
#
# Or add as a submodule / FetchContent below.

find_package(CommonLibSSE REQUIRED)
find_package(spdlog REQUIRED)

# ── Source ────────────────────────────────────────────────────────────────────
file(GLOB_RECURSE SOURCES
    src/*.cpp
    src/*.h
)

# ── Target ────────────────────────────────────────────────────────────────────
add_library(${PROJECT_NAME} SHARED ${SOURCES})

target_include_directories(${PROJECT_NAME} PRIVATE src)
target_link_libraries(${PROJECT_NAME} PRIVATE CommonLibSSE::CommonLibSSE spdlog::spdlog)

set_target_properties(${PROJECT_NAME} PROPERTIES
    SUFFIX ".dll"
    PREFIX ""
    CXX_VISIBILITY_PRESET hidden
)

# ── Post-build: copy to Skyrim Data ───────────────────────────────────────────
if(WIN32)
    add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "$<TARGET_FILE:${PROJECT_NAME}>"
            "$ENV{SKYRIM_DATA_DIR}/SKSE/Plugins/${PROJECT_NAME}.dll"
        COMMENT "Copying SKSE plugin to Skyrim Data/SKSE/Plugins/"
    )
endif()
