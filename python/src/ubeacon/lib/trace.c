/** \file trace.c
 *  \brief Contains a Python "trace function" implementation compatible with PyEval_SetTrace().
 *
 *  The CPython tracing API is intended to allow users to implement Python debuggers, and it's used
 *  by the UBeacon library to keep track of what code the Python application currently is executing,
 *  and expose some simple (but vital for debuggers) information about the internals of the
 *  application, for example: line number, file name, stack depth, etc.
 *
 *  The CPython tracing API is an area of the interpreter under active development, and many of the
 *  most useful features are not availalble in all versions of Python. As such, this code doesn't
 *  use all the latest features in an attempt to maintain compatibility with a wide range of Python
 *  versions.
 */

#include <Python.h>
#include <frameobject.h>
#include <stdint.h>

#include "ceval.h"
#include "object.h"
#include "pyerrors.h"
#include "pystate.h"
#include "traceback.h"
#include "ubeacon.h"

static int s_trace_existing_threads(void);


/**
 *  \brief A simple FNV-1 hash function to converting a string to an uint64_t.
 *
 *  UDB's Python support relies heavily on conditional breakpoints which, unfortunately, do not
 *  support string comparisons. In order to work around this limitation we use a
 *  non-cyptographically secure FNV-1 hash to convert strings into uint64_t integers, which we can
 *  compare in conditional breakpoints.
 *
 *  \param data_str The string to be hashed.
 *  \return return An integer containing the resulting hash value.
 */
static uint64_t
s_ubeacon_simple_hash(const char *data_str)
{
    if (!data_str) return 0;

    /* This is an FNV-1 hash. It is not cryprographically secure. See:
     * https://en.wikipedia.org/wiki/Fowler%E2%80%93Noll%E2%80%93Vo_hash_function */
    uint64_t hash = 0xcbf29ce484222325;
    uint64_t prime = 0x100000001b3;

    for (size_t i = 0; data_str[i] != '\0'; i++)
    {
        hash ^= (uint64_t)data_str[i];
        hash *= prime;
    }

    return hash;
}


/**
 *  \brief Calculate the depth of the current Python stack.
 *
 *  \param top_level A Python frame object corresponding the top-most frame of the Python stack.
 *  \return An integer greater than zero corresponding to the depth of the stack.
 */
static int
s_calculate_stack_depth(PyFrameObject *top_level)
{
    uint64_t depth = 0;
    for (PyFrameObject *frame = top_level; frame != NULL; frame = PyFrame_GetBack(frame))
    {
        depth++;

        /* We don't want to decrement the reference count on the top level frame, as it was passed
         * into `s_trace_entry_point()` with it's reference count already incremented. The caller of
         * `s_trace_entry_point()` is responsible for decrementing it, and if we do it here it
         * causes all sorts of heisenbugs in CPython (hangs/SIGSEGVs/missing imports). */

        if (frame != top_level) Py_DECREF(frame);
    }
    assert(depth > 0);
    return depth;
}


static const char*
s_exception_type(PyObject *exc_info)
{
    if (!PyTuple_Check(exc_info))
    {
        PyErr_SetString(PyExc_TypeError, "exc_info is not a tuple.");
        return NULL;
    }

    if (PyTuple_Size(exc_info) != 3)
    {
        PyErr_SetString(PyExc_RuntimeError, "exc_info does not have 3 elements.");
        return NULL;
    }

    PyObject *exc_type = PyTuple_GetItem(exc_info, 0);
    PyObject *exc_type_name = PyObject_GetAttrString(exc_type, "__name__");
    if (!exc_type_name)
    {
        PyErr_SetString(PyExc_RuntimeError, "exc_type does not have '__name__'.");
        return NULL;
    }

    PyObject *exc_type_name_str = PyObject_Str(exc_type_name);
    const char* result =  PyUnicode_AsUTF8(exc_type_name_str);

    Py_DECREF(exc_type_name);
    Py_DECREF(exc_type_name_str);

    return result;
}


static bool
s_is_exception_origin(PyObject *exc_info, PyObject *current_frame)
{
    if (!PyTuple_Check(exc_info))
    {
        PyErr_SetString(PyExc_TypeError, "exc_info is not a tuple.");
        return NULL;
    }

    if (PyTuple_Size(exc_info) != 3)
    {
        PyErr_SetString(PyExc_RuntimeError, "exc_info does not have 3 elements.");
        return NULL;
    }

    PyObject *traceback = PyTuple_GetItem(exc_info, 2);
    PyObject *next = PyObject_GetAttrString(traceback, "tb_next");
    PyObject *frame = PyObject_GetAttrString(traceback, "tb_frame");
    
    while (next != NULL && next != Py_None)
    {
        Py_XDECREF(next);
        Py_XDECREF(frame);
        next = PyObject_GetAttrString(next, "tb_next");
        if (next && next != Py_None) // Python 3.12 can return null frames
        {
            frame = PyObject_GetAttrString(next, "tb_frame");
        }
        else
        {
            frame = NULL;
        }
    }


    bool is_origin = frame == current_frame;
    Py_XDECREF(next);
    Py_XDECREF(frame);
    return is_origin;
}


/**
 *  \brief Empty function for setting breakpoints on specific Python events.
 *
 *  This function, and it's counterparts are completely empty, but we don't want the compiler to
 *  optimize them away. As such, it's vital that the -O0 option is passed to the compiler. The
 *  `optimize` attribute was initially used to try and ensure that only these functions were
 *  unoptimized, however a compiler bug causes incorrect debuginfo in this case, breaking the
 *  libraries functionality.
 *
 *  \see s_ubeacon_trace_ret() s_ubeacon_trace_line() s_ubeacon_trace_exception()
 */
static void s_ubeacon_trace_call() {}
static void s_ubeacon_trace_ret() {}
static void s_ubeacon_trace_line() {}
static void s_ubeacon_trace_exception() {}


/**
 *  \brief Main trace function of the UBeacon library
 *
 *  This function is the "core" of the reversible Python debugging implementation. Most of the
 *  surrounding code in this file is helper code to ensure that this function is called at the
 *  correct moment. It has two main responsibilities:
 *
 *  1. Calling the correct empty trace function so that UDB can break on the relevant event.
 *  2. Updating global state to allow UDB to set appropriate conditional breakpoints.
 *
 *  The signature of this function is defined by CPython. See
 *  https://docs.python.org/3/c-api/init.html#c.Py_tracefunc for more information on the signature
 *  and the specific values that can be passed to it's arguments.
 *
 */
static int
s_trace_entry_point(PyObject *obj, PyFrameObject *frame, int what, PyObject *arg)
{
    uint64_t depth = s_calculate_stack_depth(frame);

    PyCodeObject *code = PyFrame_GetCode(frame);
    assert(code != NULL);

    const char *func_name = PyUnicode_AsUTF8(code->co_name);
    const char *filename = PyUnicode_AsUTF8(code->co_filename);
    assert(func_name != NULL);
    assert(filename != NULL);
    Py_DECREF(code);

    Py_INCREF(frame); /* We don't want this frame to dissapear once we're done here. */
    ubeacon_get()->current_depth = depth;
    ubeacon_get()->current_file = filename;
    ubeacon_get()->current_func = func_name;
    ubeacon_get()->current_file_id = s_ubeacon_simple_hash(filename);
    ubeacon_get()->current_line = PyFrame_GetLineNumber(frame);
    Py_XDECREF(ubeacon_get()->current_frame);
    ubeacon_get()->current_frame = frame;
    ubeacon_get()->current_func_id = s_ubeacon_simple_hash(func_name);
    ubeacon_get()->exception_origin = false;
    Py_XDECREF(ubeacon_get()->exception_info);
    ubeacon_get()->exception_info = NULL;
    ubeacon_get()->exception_type = NULL;
    ubeacon_get()->exception_type_id = 0;

    switch (what)
    {
        case PyTrace_LINE:
            s_ubeacon_trace_line();
            ubeacon_get()->first_line = false;
            break;
        case PyTrace_RETURN:
            s_ubeacon_trace_ret();
            Py_XDECREF(ubeacon_get()->current_frame);
            ubeacon_get()->current_frame = NULL;
            break;
        case PyTrace_CALL:
            s_ubeacon_trace_call();
            ubeacon_get()->first_line = true;
            break;
        case PyTrace_EXCEPTION:
            if (ubeacon_get()->exception_info != NULL) Py_DECREF(ubeacon_get()->exception_info);
            Py_INCREF(arg);
            ubeacon_get()->exception_origin = s_is_exception_origin(arg, (PyObject*)frame);
            ubeacon_get()->exception_info = arg;
            ubeacon_get()->exception_type = s_exception_type(arg);
            ubeacon_get()->exception_type_id = s_ubeacon_simple_hash(ubeacon_get()->exception_type);
            s_ubeacon_trace_exception();
            break;
        default:
            break;
    }

    return 0;
}


/**
 *  \brief A function to set up full tracing when called from Python
 *
 *  CPython's `PyEval_SetTrace()` function can only be used to switch tracing on for existing
 *  threads. In the event that new thread are created, Python's threading module supplies the
 *  `threading.settrace()` function. Unfortunately, lines don't get traced when using
 *  `threading.settrace()`, only call/ret/exceptions. This function gets called for every new thread
 *  created using `threading.Thread()`, and it sets up full tracing on all threads using the C API.
 *
 *  See the CPython API documentation for detailed information on the arguments and return value of
 *  Python functions written in C.
 */
static PyObject *
s_trace_python_entry_point(PyObject *self __attribute__((unused)), PyObject *args __attribute__((unused)))
{
    s_trace_existing_threads(); /* This is a somewhat nuclear option! */
    Py_RETURN_NONE;
}


/**
 *  \brief Call Python's `threading.settrace()` to switch on tracing for future threads.
 *
 *  In a similar fashion to s_trace_current_threads(), we want to make sure that any threads
 *  started in the future by the Python interpreter are properly recorded. This function is roughly
 *  equivalent to running the python code:
 *
 *  ```python
 *  import threading
 *  threading.settrace(ubeacon_trace_fn)
 *  ```
 *
 *  Where `ubeacon_trace_fn` is a Python function implemented in C (see
 *  s_trace_python_entry_point()). It is important to be aware that this will only cause threads
 *  started with the Python `threading` module to be started with tracing. If a thread is created
 *  through some other means (for example, using the clone() system call directly) then it will not
 *  be traced, and as such it will not have Python debugging support in UDB.
 *
 *  \return 0 on success, else -1 with a Python exception set.
 */
static int
s_trace_future_threads(void)
{
    PyObject *threading_module = PyImport_ImportModule("threading");
    if (!threading_module)
    {
        PyErr_Print();
        PyErr_SetString(PyExc_RuntimeError, "Couldn't import threading module");
        return -1;
    }

    PyObject *settrace_func = PyObject_GetAttrString(threading_module, "settrace");
    Py_DECREF(threading_module);
    if (!settrace_func)
    {
        PyErr_SetString(PyExc_RuntimeError, "threading.settrace not found");
        return -1;
    }

    if (!PyCallable_Check(settrace_func))
    {
        Py_XDECREF(settrace_func);
        PyErr_SetString(PyExc_RuntimeError, "threading.settrace not callable");
        return -1;
    }

    static PyMethodDef trace_def = {
        "ubeacon_trace_fn",
        (PyCFunction)s_trace_python_entry_point,
        METH_VARARGS,
        "Undo UBeacon library trace callback. For internal UDB use only."
    };

    PyObject *entry_point = PyCFunction_New(&trace_def, NULL);
    if (!entry_point)
    {
        Py_DECREF(settrace_func);
        PyErr_SetString(PyExc_RuntimeError, "Couldn't wrap tracing function.");
        return -1;
    }

    PyObject *result = PyObject_CallFunctionObjArgs(settrace_func, entry_point, NULL);
    Py_DECREF(settrace_func);
    Py_DECREF(entry_point);

    if (!result)
    {
        PyErr_SetString(PyExc_RuntimeError, "Couldn't insert tracing function.");
        return -1;
    }

    Py_DECREF(result);
    return 0;
}


/**
 *  \brief Attach UBeacon's tracing functionality to currently existing Python threads.
 *
 *  This function loops over every Python thread, in every running interpreter and inserts our trace
 *  handling. This only applies to 
 */
static int
s_trace_existing_threads(void)
{
    for (PyInterpreterState *interpreter_state = PyInterpreterState_Head();
         interpreter_state != NULL;
         interpreter_state = PyInterpreterState_Next(interpreter_state))
    {
        for (PyThreadState *thread_state = PyInterpreterState_ThreadHead(interpreter_state);
             thread_state != NULL;
             thread_state = PyThreadState_Next(thread_state))
        {
            PyThreadState *prev_thread_state = PyThreadState_Swap(thread_state);
            PyEval_SetTrace(s_trace_entry_point, NULL);
            PyThreadState_Swap(prev_thread_state);
        }
    }

    return 0;
}


/**
 *  \brief Set up tracing on all current and future threads.
 *
 *  It is important that we setup tracing on future threads before doing so on existing threads. If
 *  the order is switched, the Python standard library code that runs to set up tracing on future
 *  threads is being traced, which means:
 *
 *  1. The UBeacon library can potentially reenter itself, which it is not designed to do, causing
 *     all sorts of undefined behaviour.
 *
 *  2. The code that sets up future thread tracing will be recorded, and exposed to the end user as
 *     Python code that can be debugged.
 *
 *  \return Zero on success, negative error code on failure.
 */
int
ubeacon_trace_setup()
{
    PyGILState_STATE state = PyGILState_Ensure();

    int e = s_trace_future_threads();
    if (e) goto out;

    e = s_trace_existing_threads();
    if (e) goto out;

out:
    PyGILState_Release(state);
    return 0;
}
