#pragma once

#include <stdbool.h>
#include <stdint.h>

#include <Python.h>


typedef struct
{
    const char *current_file;
    const char *current_func;

    uint64_t current_file_id;
    uint64_t current_line;
    uint64_t current_func_id;

    PyFrameObject *current_frame;
    uint64_t current_depth;
    bool first_line;

    bool exception_origin;
    PyObject* exception_info;
    const char *exception_type;
    uint64_t exception_type_id;
} undo_beacon_t;


/**
 * \brief Returns a pointer to the global `undo_ubeacon_t` instance.
 */
undo_beacon_t*
ubeacon_get();
