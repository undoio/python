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
static void
s_ubeacon_interact_frame_json(FILE *file, PyFrameObject *frame, unsigned frame_no)
{
    PyCodeObject *code = PyFrame_GetCode(frame);

    /* co_name and co_filename are not part of the public API so we need to be careful here! */
    const char *func_name = PyUnicode_AsUTF8(code->co_name);
    const char *file_name = PyUnicode_AsUTF8(code->co_filename);
    int line = PyFrame_GetLineNumber(frame);

    fprintf(file, "{\"func_name\": \"%s\", \"file_name\": \"%s\", \"line\": %d, \"frame_no\": %u}",
            func_name, file_name, line, frame_no);
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

    FILE *file = NULL;
    file = fopen(path, "w");
    assert(file != NULL);
    fprintf(file, "{\"frames\": [");

    if (Py_IsInitialized())
    {
        PyGILState_STATE state = PyGILState_Ensure();
        unsigned frame_no = 0;
        for (PyFrameObject *frame = ubeacon_get()->current_frame;
             frame != NULL;
             frame = PyFrame_GetBack(frame))
        {
            s_ubeacon_interact_frame_json(file, frame, frame_no);
            bool is_top_frame = PyFrame_GetBack(frame) == NULL;
            if (!is_top_frame) fprintf(file, ",");
            Py_DECREF(frame);
            frame_no++;
        }
        PyGILState_Release(state);
    }

    fprintf(file, "]}");
    fclose(file);
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
    FILE *file = NULL;
    file = fopen(path, "w");
    assert(file != NULL);
    fprintf(file, "{\"locals\": [");

    if (Py_IsInitialized())
    {
        PyGILState_STATE state = PyGILState_Ensure();
        PyObject *locals = PyEval_GetLocals();
        PyObject *key, *value;
        Py_ssize_t pos = 0;
        Py_ssize_t len = PyDict_Size(locals);

        while (locals && PyDict_Next(locals, &pos, &key, &value)) {
            PyObject *key_str = PyObject_Str(key);
            PyObject *value_str = PyObject_Repr(value);

            if (key_str && value_str) {
                fprintf(file, "{\"name\": \"%s\", \"value\": \"%s\"}",
                        PyUnicode_AsUTF8(key_str),
                        PyUnicode_AsUTF8(value_str));
            }

            Py_XDECREF(key_str);
            Py_XDECREF(value_str);

            bool is_last_local = pos == len;
            if (!is_last_local) fprintf(file, ",");
        }
        PyGILState_Release(state);
    }

    fprintf(file, "]}");
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
