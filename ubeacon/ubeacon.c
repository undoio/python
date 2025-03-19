/** \file ubeacon.c
 *  \brief A Python module for helping UDB to find out where it is in Python code.
 */

#include <Python.h>
#include <assert.h>
#include <frameobject.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "ceval.h"
#include "object.h"
#include "unicodeobject.h"

#include "ubeacon.h"
#include "trace.h"

static undo_beacon_t s_ubeacon;

undo_beacon_t *
ubeacon_get()
{
    return &s_ubeacon;
}

static PyObject *
s_ubeacon_start(PyObject *self, PyObject *args)
{
    int e = ubeacon_trace_setup();
    if (e) return NULL;
    Py_RETURN_NONE;
}

static PyObject *
s_ubeacon_stop(PyObject *self, PyObject *args)
{
    PyEval_SetTrace(NULL, NULL);
    Py_RETURN_NONE;
}

static PyMethodDef s_ubeacon_methods[] = {
    { "start", s_ubeacon_start, METH_NOARGS, "Start tracing function calls." },
    { "stop", s_ubeacon_stop, METH_NOARGS, "Stop tracing function calls." },
    { NULL, NULL, 0, NULL }
};

static struct PyModuleDef tracemodule = {
    PyModuleDef_HEAD_INIT,
    "ubeacon",
    "Undo module for providing Python interpreter state to the debugger.",
    -1,
    s_ubeacon_methods
};

PyMODINIT_FUNC
PyInit_ubeacon(void)
{
    return PyModule_Create(&tracemodule);
}
