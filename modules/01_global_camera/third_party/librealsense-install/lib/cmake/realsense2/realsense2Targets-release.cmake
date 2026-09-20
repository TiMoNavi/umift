#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "realsense2::rsutils" for configuration "Release"
set_property(TARGET realsense2::rsutils APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(realsense2::rsutils PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/librsutils.a"
  )

list(APPEND _cmake_import_check_targets realsense2::rsutils )
list(APPEND _cmake_import_check_files_for_realsense2::rsutils "${_IMPORT_PREFIX}/lib/librsutils.a" )

# Import target "realsense2::realsense-file" for configuration "Release"
set_property(TARGET realsense2::realsense-file APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(realsense2::realsense-file PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "C;CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/librealsense-file.a"
  )

list(APPEND _cmake_import_check_targets realsense2::realsense-file )
list(APPEND _cmake_import_check_files_for_realsense2::realsense-file "${_IMPORT_PREFIX}/lib/librealsense-file.a" )

# Import target "realsense2::rs_lz4" for configuration "Release"
set_property(TARGET realsense2::rs_lz4 APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(realsense2::rs_lz4 PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "C"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/librs_lz4.a"
  )

list(APPEND _cmake_import_check_targets realsense2::rs_lz4 )
list(APPEND _cmake_import_check_files_for_realsense2::rs_lz4 "${_IMPORT_PREFIX}/lib/librs_lz4.a" )

# Import target "realsense2::sqlite3_lib" for configuration "Release"
set_property(TARGET realsense2::sqlite3_lib APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(realsense2::sqlite3_lib PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "C"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libsqlite3_lib.a"
  )

list(APPEND _cmake_import_check_targets realsense2::sqlite3_lib )
list(APPEND _cmake_import_check_files_for_realsense2::sqlite3_lib "${_IMPORT_PREFIX}/lib/libsqlite3_lib.a" )

# Import target "realsense2::realsense2" for configuration "Release"
set_property(TARGET realsense2::realsense2 APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(realsense2::realsense2 PROPERTIES
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/librealsense2.2.58.1.dylib"
  IMPORTED_SONAME_RELEASE "@rpath/librealsense2.2.58.dylib"
  )

list(APPEND _cmake_import_check_targets realsense2::realsense2 )
list(APPEND _cmake_import_check_files_for_realsense2::realsense2 "${_IMPORT_PREFIX}/lib/librealsense2.2.58.1.dylib" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
