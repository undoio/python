/** \file interact.c
 *  \brief Functions for querying and interacting with the Python interpreter from a debugger.
 *
 *  In order to debug the Python interpreter various bits of interpreter state must be accessible to
 *  the debugger. This file contains functions to expose that information in an easy to consume
 *  manner. As the functions in this file are not typically called from anywhere in the normal
 *  execution paths of the application they are marked with the `unused` attribute to keep the
 *  compiler happy. These functions will be called from the `udb_extension` Python module that is
 *  sourced into UDB, so it's important that they remain in the UBeacon record time library.
 */

#include <stdio.h>
#include <Python.h>
#include <frameobject.h> /* Warning, this isn't part of the Public API! */
#include <string.h>
#include <sys/types.h>

#include "cJSON.h"

#include "ubeacon.h"


/**
 *  \brief Write a JSON object describing a Python frame to the provided open file object.
 *
 *  The format of the frame object must match the `Frame` Pydantic model in
 *  ubeacon/udb_extension/ubeacon.py.
 *
 *  \param file An open file object to which the JSON will be written.
 *  \param frame The Python frame object to be dumped.
 *  \param frame_no An integer describing the position of the proviced frame object in the stack.
 */
__attribute__((unused))
static cJSON*
s_frame_to_cJSON(PyFrameObject *py_frame, unsigned raw_frame_no)
{
    PyCodeObject *code = PyFrame_GetCode(py_frame);
    if (!code) goto fail;

    /* co_name and co_filename are not part of the public API so we need to be careful here! */

    cJSON *frame = cJSON_CreateObject();
    if (frame == NULL) goto fail_frame;

    cJSON *func_name = cJSON_CreateString(PyUnicode_AsUTF8(code->co_name));
    if (func_name == NULL) goto fail_func_name;

    cJSON *file_name = cJSON_CreateString(PyUnicode_AsUTF8(code->co_filename));
    if (file_name == NULL) goto fail_file_name;

    cJSON *line = cJSON_CreateNumber(PyFrame_GetLineNumber(py_frame));
    if (line == NULL) goto fail_line;

    cJSON *frame_no = cJSON_CreateNumber(raw_frame_no);
    if (frame_no == NULL) goto fail_frame_no;

    cJSON_AddItemToObject(frame, "func_name", func_name);
    cJSON_AddItemToObject(frame, "file_name", file_name);
    cJSON_AddItemToObject(frame, "line", line);
    cJSON_AddItemToObject(frame, "frame_no", frame_no);

    Py_DECREF(code);
    return frame;

fail_frame_no:
    cJSON_Delete(line);
fail_line:
    cJSON_Delete(file_name);
fail_file_name:
    cJSON_Delete(func_name);
fail_func_name:
    cJSON_Delete(frame);
fail_frame:
    Py_DECREF(code);
fail:
    return NULL;
}


/**
 *  \brief Write a JSON object describing a Python backtrace to a file.
 *
 *  The format of the backtrace object must match the `Backtrace` model in
 *  ubeacon/udb_extension/ubeacon.py.
 *
 *  \param path A path to which the backtrace JSON object will be written.
 */
__attribute__((unused))
static void
s_ubeacon_interact_backtrace_json(const char* path)
{
    if (path == NULL) return;

    FILE *file = fopen(path, "w");
    if (file == NULL) return;

    cJSON *top_level = cJSON_CreateObject();
    if (top_level == NULL) goto fail_no_cjson;

    cJSON *frames = cJSON_CreateArray();
    if (frames == NULL) goto fail_frames;
    cJSON_AddItemToObject(top_level, "frames", frames);
    
    if (!Py_IsInitialized()) goto fail_no_python;

    PyGILState_STATE state = PyGILState_Ensure();
    unsigned frame_no = 0;
    for (PyFrameObject *py_frame = ubeacon_get()->current_frame;
         py_frame != NULL;
         py_frame = PyFrame_GetBack(py_frame))
    {
        cJSON *frame = s_frame_to_cJSON(py_frame, frame_no);
        if (frame == NULL)
        {
            Py_DECREF(py_frame);
            goto fail_no_python;
        }
        frame_no++;
        cJSON_AddItemToArray(frames, frame);
        Py_DECREF(py_frame);
    }
    PyGILState_Release(state);

fail_no_python:
    fprintf(file, "%s", cJSON_Print(top_level));
fail_frames:
    cJSON_Delete(top_level);
fail_no_cjson:
    fclose(file);
}


__attribute__((unused))
static cJSON *
s_local_to_cJSON(PyObject *py_name, PyObject *py_value)
{
    PyObject *name_str = PyObject_Str(py_name);
    if (name_str == NULL) goto fail_py_name;

    PyObject *value_str = PyObject_Repr(py_value);
    if (value_str == NULL) goto fail_py_value;

    cJSON *local = cJSON_CreateObject();
    if (local == NULL) goto fail_local;

    cJSON *name = cJSON_CreateString(PyUnicode_AsUTF8(name_str));
    if (name == NULL) goto fail_name;

    cJSON *value = cJSON_CreateString(PyUnicode_AsUTF8(value_str));
    if (value == NULL) goto fail_value;

    cJSON_AddItemToObject(local, "name", name);
    cJSON_AddItemToObject(local, "value", value);

    Py_DECREF(value_str);
    Py_DECREF(name_str);
    return local;

fail_value:
    cJSON_Delete(name);
fail_name:
    cJSON_Delete(local);
fail_local:
    Py_DECREF(value_str);
fail_py_value:
    Py_DECREF(name_str);
fail_py_name:
    return NULL;
}


/**
 *  \brief Write a JSON object describing the current Python locals to a file.
 *
 *  The format of the locals object must match the `Locals` model in
 *  ubeacon/udb_extension/ubeacon.py.
 *
 *  \param path A path to which the locals JSON object will be written.
 */
__attribute__((unused))
static void
s_ubeacon_interact_locals_json(const char *path)
{
    if (path == NULL) return;

    FILE *file = fopen(path, "w");
    if (file == NULL) return;

    cJSON *top_level = cJSON_CreateObject();
    if (top_level == NULL) goto fail_no_cjson;

    cJSON *locals = cJSON_CreateArray();
    if (locals == NULL) goto fail_locals;
    cJSON_AddItemToObject(top_level, "locals", locals);

    if (!Py_IsInitialized()) goto fail_no_python;

    PyGILState_STATE state = PyGILState_Ensure();
    PyObject *py_locals = PyEval_GetLocals();
    PyObject *key, *value;
    Py_ssize_t pos = 0;
    while (locals && PyDict_Next(py_locals, &pos, &key, &value))
    {
        cJSON *local = s_local_to_cJSON(key, value);
        if (local == NULL) continue;
        cJSON_AddItemToArray(locals, local);
    }
    PyGILState_Release(state);

fail_no_python:
    fprintf(file, "%s", cJSON_Print(top_level));
fail_locals:
    cJSON_Delete(top_level);
fail_no_cjson:
    fclose(file);
}


/**
 *  \brief Print a Python error to a FILE object.
 *
 *  When we are running Python code through s_ubeacon_interact_eval, we don't want to output
 *  information to the debuggee's stdout, as that might not be visible to the debugger (for example,
 *  in the case of attach). This function lets us write an error to a file instead.
 *
 *  \param FILE a file object created with fopen() or equivalent
 */
static void
s_err_to_file(FILE *fp)
{
    // TODO: PyErr_Fetch is deprecated since 3.12.
    PyObject *ptype, *pvalue, *ptraceback;
    PyErr_Fetch(&ptype, &pvalue, &ptraceback);
    if (ptype == NULL) return;

    PyErr_NormalizeException(&ptype, &pvalue, &ptraceback);
    PyObject *error_string = PyObject_Str(pvalue);
    if (error_string)
    {
        fprintf(fp, "Python error: %s\n", PyUnicode_AsUTF8(error_string));
        Py_DECREF(error_string);
    }
    else
    {
        fprintf(fp, "Python error: (failed to convert error to string)\n");
    }

    Py_XDECREF(ptype);
    Py_XDECREF(pvalue);
    Py_XDECREF(ptraceback);
}


/**
 *  \brief Execute some Python code and print the result.
 *
 *  This method runs some arbitrary Python code in the same context as the current frame, printing
 *  the result to stdout. Note that this method performs no sandboxing whatsoever and can modify any
 *  process state. This method is intended to be called from the UDB command line, which will
 *  execute this function only in an ephemoral `fork()`ed copy of the original process.
 *
 *  \param path A path to which the resulting expression will be written.
 *  \param code The code to be evaluated.
 */
__attribute__((unused))
static void
s_ubeacon_interact_eval(const char* path, const char *code)
{
    FILE *file = NULL;
    file = fopen(path, "w");
    assert(file != NULL);

    if (Py_IsInitialized())
    {
        PyGILState_STATE state = PyGILState_Ensure();
        PyObject *locals = PyEval_GetLocals();
        PyObject *globals = PyEval_GetGlobals();


        /* TODO need to think about exception handling a bit more here. */
        PyObject *result = PyRun_String(code, Py_eval_input, globals, locals);
        if (result)
        {
            PyObject *result_str = PyObject_Repr(result);
            fprintf(file, "%s", PyUnicode_AsUTF8(result_str));
            Py_XDECREF(result_str);
        }
        else
        {
            s_err_to_file(file);
        }

        Py_DECREF(globals);
        Py_DECREF(locals);
        PyGILState_Release(state);
    }

    fclose(file);
}

__attribute__((unused))
static void
s_ubeacon_interact_exception_type(PyObject *exc_info)
{
    if (!PyTuple_Check(exc_info))
    {
        fprintf(stderr, "Error: exc_info is not a tuple.\n");
        return;
    }

    if (PyTuple_Size(exc_info) != 3)
    {
        fprintf(stderr, "Error: exc_info does not have 3 elements.\n");
        return;
    }

    PyObject *exc_type = PyTuple_GetItem(exc_info, 0);
    PyObject *exc_type_name = PyObject_GetAttrString(exc_type, "__name__");
    PyObject *exc_type_name_str = PyObject_Str(exc_type_name);
    printf("exceptionntype: %s\n", PyUnicode_AsUTF8(exc_type_name_str));

    Py_DECREF(exc_type);
    Py_DECREF(exc_type_name);
    Py_DECREF(exc_type_name_str);
}
