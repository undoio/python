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
#include <stdlib.h>
#include <Python.h>
#include <frameobject.h> /* Warning, this isn't part of the Public API! */
#include <string.h>
#include <sys/types.h>

#include "cJSON.h"

#include "ubeacon.h"


/* --------------------------------------------------------------------------
 * CPython dict internals (not in public headers).
 *
 * These definitions mirror the internal structs so that we can locate the
 * exact address where a dict stores a value pointer, enabling hardware
 * watchpoints on individual dict entries.
 *
 * The layout changed significantly between 3.10 and 3.11.
 * -------------------------------------------------------------------------- */

#if PY_VERSION_HEX >= 0x030b0000

/* CPython 3.11+ dict key object layout (from internal/pycore_dict.h). */
typedef struct {
    Py_ssize_t dk_refcnt;
    uint8_t dk_log2_size;
    uint8_t dk_log2_index_bytes;
    uint8_t dk_kind;          /* 0 = general, 1 = unicode, 2 = split. */
    uint32_t dk_version;
    Py_ssize_t dk_usable;
    Py_ssize_t dk_nentries;
    char dk_indices[];
} compat_PyDictKeysObject;

/* General dict entries (dk_kind == 0): hash + key + value. */
typedef struct {
    Py_hash_t me_hash;
    PyObject *me_key;
    PyObject *me_value;
} compat_PyDictKeyEntry;

/* Unicode-only dict entries (dk_kind != 0): key + value, no hash. */
typedef struct {
    PyObject *me_key;
    PyObject *me_value;
} compat_PyDictUnicodeEntry;

/* PyDictValues wrapper for split-table dicts. */
#if PY_VERSION_HEX >= 0x030d0000
/* Python 3.13 added metadata fields before the values array. */
typedef struct {
    uint8_t capacity;
    uint8_t size;
    uint8_t embedded;
    uint8_t valid;
    PyObject *values[1];
} compat_PyDictValues;
#else
typedef struct {
    PyObject *values[1];
} compat_PyDictValues;
#endif

#define COMPAT_DK_IXSIZE(dk)  ((size_t)1 << (dk)->dk_log2_index_bytes)

#else /* Python 3.10 */

typedef PyObject *(*dict_lookup_func_t)(PyDictObject *, PyObject *, Py_hash_t, PyObject **);

typedef struct {
    Py_ssize_t dk_refcnt;
    Py_ssize_t dk_size;       /* Hash table size (always power of 2). */
    dict_lookup_func_t dk_lookup;
    Py_ssize_t dk_usable;
    Py_ssize_t dk_nentries;   /* Number of occupied entries. */
    char dk_indices[];         /* Variable-length, followed by dk_entries. */
} compat_PyDictKeysObject;

typedef struct {
    Py_hash_t me_hash;
    PyObject *me_key;
    PyObject *me_value;
} compat_PyDictKeyEntry;

#endif


/**
 *  \brief Find the address of the value slot for a string key in a dict.
 *
 *  Returns the address of the ``PyObject*`` that stores the value for
 *  \p key_str, or NULL if the key is not found.  This works for both
 *  combined and split dict tables.
 */
static PyObject **
s_dict_value_addr(PyDictObject *dict, const char *key_str)
{
    compat_PyDictKeysObject *dk =
        (compat_PyDictKeysObject *)dict->ma_keys;

#if PY_VERSION_HEX >= 0x030b0000

    size_t idx_bytes = COMPAT_DK_IXSIZE(dk);

    if (dk->dk_kind == 0) {
        /* DICT_KEYS_GENERAL: entries have hash + key + value. */
        compat_PyDictKeyEntry *entries =
            (compat_PyDictKeyEntry *)((char *)dk->dk_indices + idx_bytes);

        for (Py_ssize_t i = 0; i < dk->dk_nentries; i++) {
            PyObject *key = entries[i].me_key;
            if (!key || !PyUnicode_Check(key)) continue;
            const char *k = PyUnicode_AsUTF8(key);
            if (k && strcmp(k, key_str) == 0) {
                if (dict->ma_values) {
                    compat_PyDictValues *dv =
                        (compat_PyDictValues *)dict->ma_values;
                    return &dv->values[i];
                }
                return &entries[i].me_value;
            }
        }
    } else {
        /* DICT_KEYS_UNICODE / DICT_KEYS_SPLIT: compact entries (no hash). */
        compat_PyDictUnicodeEntry *entries =
            (compat_PyDictUnicodeEntry *)((char *)dk->dk_indices + idx_bytes);

        for (Py_ssize_t i = 0; i < dk->dk_nentries; i++) {
            PyObject *key = entries[i].me_key;
            if (!key || !PyUnicode_Check(key)) continue;
            const char *k = PyUnicode_AsUTF8(key);
            if (k && strcmp(k, key_str) == 0) {
                if (dict->ma_values) {
                    compat_PyDictValues *dv =
                        (compat_PyDictValues *)dict->ma_values;
                    return &dv->values[i];
                }
                return &entries[i].me_value;
            }
        }
    }

#else /* Python 3.10 */

    int ixsize;
    if (dk->dk_size <= 0xff)
        ixsize = 1;
    else if (dk->dk_size <= 0xffff)
        ixsize = 2;
    else if ((uint64_t)dk->dk_size <= 0xffffffffULL)
        ixsize = 4;
    else
        ixsize = 8;

    compat_PyDictKeyEntry *entries =
        (compat_PyDictKeyEntry *)((char *)dk->dk_indices
                                  + dk->dk_size * ixsize);

    for (Py_ssize_t i = 0; i < dk->dk_nentries; i++) {
        PyObject *key = entries[i].me_key;
        if (!key) continue;
        if (!PyUnicode_Check(key)) continue;
        const char *k = PyUnicode_AsUTF8(key);
        if (k && strcmp(k, key_str) == 0) {
            if (dict->ma_values) {
                /* Split table: values in separate array. */
                return &dict->ma_values[i];
            }
            /* Combined table: value inline in entry. */
            return &entries[i].me_value;
        }
    }

#endif

    return NULL;
}


/**
 *  \brief Write a JSON object describing the list of Python script files to a file.
 *
 *  The format of the JSON object must match the `FilesList` model in
 *  ubeacon/udb_extension/ubeacon.py.
 *
 *  \param path A path to which the files JSON object will be written.
 */
__attribute__((unused))
static void
s_ubeacon_interact_files_json(const char* path)
{
    if (path == NULL) return;

    FILE *file = fopen(path, "w");
    if (file == NULL) return;

    PyObject *sys      = PyImport_ImportModule("sys");
    if (!sys) goto fail_no_python;

    PyObject *modules  = PyObject_GetAttrString(sys, "modules");
    Py_DECREF(sys);
    if (!modules) goto fail_no_python;

    PyObject *values   = PyMapping_Values(modules);
    Py_DECREF(modules);
    if (!values) goto fail_no_python;

    cJSON *top_level = cJSON_CreateObject();
    if (top_level == NULL) goto fail_no_cjson;

    cJSON *files = cJSON_CreateArray();
    if (files == NULL) goto fail_files;
    cJSON_AddItemToObject(top_level, "files", files);

    Py_ssize_t n = PyList_Size(values);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *mod  = PyList_GetItem(values, i); // borrowed
        if (!PyObject_HasAttrString(mod, "__file__")) continue;

        PyObject *file = PyObject_GetAttrString(mod, "__file__");
        if (!file) { PyErr_Clear(); continue; }

        // Only include .py files (skip None, .so, .pyd, etc.)
        if (file == Py_None || !PyUnicode_Check(file)) {
            Py_DECREF(file);
            continue;
        }

        const char *path = PyUnicode_AsUTF8(file);
        if (path && (strstr(path, ".py") != NULL)) {
            cJSON_AddItemToArray(files, cJSON_CreateString(path));
        }
        Py_DECREF(file);
    }

    Py_DECREF(values);

fail_no_python:
    fprintf(file, "%s", cJSON_Print(top_level));
fail_files:
    cJSON_Delete(top_level);
fail_no_cjson:
    fclose(file);
}

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
    PyFrameObject *py_frame = ubeacon_get()->current_frame;

    while(py_frame != NULL)
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
        py_frame = PyFrame_GetBack(py_frame);
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


/**
 *  \brief Read the entire contents of a file into a heap-allocated string.
 *
 *  The caller is responsible for freeing the returned string.
 *
 *  \param path The path to the file to read.
 *  \return A null-terminated string containing the file contents, or NULL on failure.
 */
static char *
s_read_file(const char *path)
{
    FILE *f = fopen(path, "r");
    if (!f) return NULL;

    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);

    char *buf = malloc(len + 1);
    if (!buf) { fclose(f); return NULL; }

    fread(buf, 1, len, f);
    buf[len] = '\0';
    fclose(f);
    return buf;
}


/**
 *  \brief Create a cJSON link object describing one resolved step in a watch chain.
 *
 *  \param storage_addr  Address where the PyObject* pointer is stored, or 0 if unknown.
 *  \param current_value The current PyObject* value at that step.
 *  \param link_type     A string describing the link type (e.g. "local", "list_item").
 *  \param guard_addr    Address of a guard value to watch for container changes, or 0 if none.
 *  \return A cJSON object, or NULL on failure.
 */
static cJSON *
s_make_link(uintptr_t storage_addr, uintptr_t current_value,
            const char *link_type, uintptr_t guard_addr)
{
    cJSON *link = cJSON_CreateObject();
    if (!link) return NULL;

    char buf[32];

    if (storage_addr) {
        snprintf(buf, sizeof(buf), "0x%lx", (unsigned long)storage_addr);
        cJSON_AddItemToObject(link, "storage_addr", cJSON_CreateString(buf));
    } else {
        cJSON_AddItemToObject(link, "storage_addr", cJSON_CreateNull());
    }

    snprintf(buf, sizeof(buf), "0x%lx", (unsigned long)current_value);
    cJSON_AddItemToObject(link, "current_value", cJSON_CreateString(buf));

    cJSON_AddItemToObject(link, "link_type", cJSON_CreateString(link_type));

    if (guard_addr) {
        snprintf(buf, sizeof(buf), "0x%lx", (unsigned long)guard_addr);
        cJSON_AddItemToObject(link, "guard_addr", cJSON_CreateString(buf));
    } else {
        cJSON_AddItemToObject(link, "guard_addr", cJSON_CreateNull());
    }

    return link;
}


#if PY_VERSION_HEX >= 0x030e0000

/* In CPython 3.14, _PyInterpreterFrame replaced the int stacktop field with
   a _PyStackRef *stackpointer (8 bytes) and added a visited field, moving
   localsplus from byte offset 72 to 80 on 64-bit.  _PyStackRef is a union
   holding a uintptr_t on standard GIL-enabled builds, so reading localsplus
   elements as PyObject* is binary-compatible.
   See cpython/internal/pycore_interpframe_structs.h. */

typedef struct {
    PyObject *f_executable;
    void *previous;
    PyObject *f_funcobj;
    PyObject *f_globals;
    PyObject *f_builtins;
    PyObject *f_locals;
    PyFrameObject *frame_obj;
    void *instr_ptr;
    void *stackpointer;
    uint16_t return_offset;
    char owner;
    uint8_t visited;
    PyObject *localsplus[1];
} compat_PyInterpreterFrame;

typedef struct {
    PyObject ob_base;
    PyFrameObject *f_back;
    compat_PyInterpreterFrame *f_frame;
} compat_PyFrameObject;

#elif PY_VERSION_HEX >= 0x030b0000

/* In CPython 3.11+, PyFrameObject is opaque and the fast locals array moved
   to the internal _PyInterpreterFrame.localsplus. We mirror just enough of
   the internal layout to find fast local addresses for watchpoints.
   These must be kept in sync with cpython/internal/pycore_frame.h.

   Note: CPython 3.12 reordered the fields of _PyInterpreterFrame, and 3.13
   renamed f_code to f_executable and prev_instr to instr_ptr, but the
   localsplus flexible array member remains at the same byte offset (72 bytes
   on 64-bit), so this struct works for 3.11, 3.12, and 3.13.
   For 3.14+, see the separate struct above. */

typedef struct {
    PyObject *f_func;
    PyObject *f_globals;
    PyObject *f_builtins;
    PyObject *f_locals;
    PyCodeObject *f_code;
    PyFrameObject *frame_obj;
    void *previous;
    void *prev_instr;
    int stacktop;
    bool is_entry;
    char owner;
    PyObject *localsplus[1];
} compat_PyInterpreterFrame;

typedef struct {
    PyObject ob_base;
    PyFrameObject *f_back;
    compat_PyInterpreterFrame *f_frame;
} compat_PyFrameObject;

#endif


/**
 *  \brief Resolve a watch chain: for each step in a Python expression, find the memory
 *         address where the relevant PyObject* pointer is stored.
 *
 *  The chain is described by a JSON array read from \p input_path, where each element is
 *  an object with a "type" field ("name", "index", or "attr") and associated data.
 *
 *  The result is written to \p output_path as a JSON object with a "links" array.
 *  Each link contains:
 *    - storage_addr: hex address where the PyObject* is stored (null if unknown)
 *    - current_value: hex address of the current PyObject* value
 *    - link_type: "local", "global", "list_item", "dict_attr", or "slot_attr"
 *    - guard_addr: hex address of a guard to detect container changes (null if stable)
 *
 *  \param output_path Path to which the resolved chain JSON will be written.
 *  \param input_path  Path from which the chain description JSON will be read.
 */
__attribute__((unused))
static void
s_ubeacon_interact_resolve_watch_chain(const char *output_path, const char *input_path)
{
    if (!output_path || !input_path) return;

    FILE *outfile = fopen(output_path, "w");
    if (!outfile) return;

    cJSON *top_level = cJSON_CreateObject();
    if (!top_level) goto fail_close;

    cJSON *links_array = cJSON_CreateArray();
    if (!links_array) goto fail_top;
    cJSON_AddItemToObject(top_level, "links", links_array);

    if (!Py_IsInitialized()) goto done;

    char *input_str = s_read_file(input_path);
    if (!input_str) goto done;

    cJSON *chain = cJSON_Parse(input_str);
    free(input_str);
    if (!chain) goto done;

    PyGILState_STATE gil = PyGILState_Ensure();

    PyFrameObject *frame = ubeacon_get()->current_frame;
    if (!frame) goto fail_gil;

    PyCodeObject *code = PyFrame_GetCode(frame);
    if (!code) goto fail_gil;

    /* Walk the chain, resolving each step. `current_obj` tracks the object being traversed. */
    PyObject *current_obj = NULL;
    int n = cJSON_GetArraySize(chain);

    for (int i = 0; i < n; i++) {
        cJSON *step = cJSON_GetArrayItem(chain, i);
        cJSON *type_item = cJSON_GetObjectItem(step, "type");
        if (!type_item || !cJSON_IsString(type_item)) continue;
        const char *type = type_item->valuestring;

        cJSON *link = NULL;

        if (strcmp(type, "name") == 0) {
            cJSON *name_item = cJSON_GetObjectItem(step, "name");
            if (!name_item || !cJSON_IsString(name_item)) continue;
            const char *name = name_item->valuestring;

            /* Try local variables first. */
#if PY_VERSION_HEX >= 0x030b0000
            PyObject *varnames = PyCode_GetVarnames(code); /* strong ref */
#else
            PyObject *varnames = code->co_varnames; /* borrowed */
#endif
            Py_ssize_t idx = -1;
            for (Py_ssize_t j = 0; j < PyTuple_Size(varnames); j++) {
                const char *vname = PyUnicode_AsUTF8(PyTuple_GetItem(varnames, j));
                if (vname && strcmp(vname, name) == 0) {
                    idx = j;
                    break;
                }
            }
#if PY_VERSION_HEX >= 0x030b0000
            Py_DECREF(varnames);
#endif

            if (idx >= 0) {
#if PY_VERSION_HEX >= 0x030b0000
                /* In 3.11+ the fast locals are in the internal interpreter
                   frame, accessed via the compat struct. */
                compat_PyFrameObject *cf = (compat_PyFrameObject *)frame;
                uintptr_t storage = (uintptr_t)&cf->f_frame->localsplus[idx];
                PyObject *value = cf->f_frame->localsplus[idx];
#else
                uintptr_t storage = (uintptr_t)&frame->f_localsplus[idx];
                PyObject *value = frame->f_localsplus[idx];
#endif
                link = s_make_link(storage, (uintptr_t)value, "local", 0);
                current_obj = value;
            } else {
                /* Fall back to globals dict. */
                PyObject *globals = PyEval_GetGlobals();
                if (globals) {
                    PyObject *value = PyDict_GetItemString(globals, name); /* borrowed */
                    if (value) {
                        PyObject **vaddr =
                            s_dict_value_addr((PyDictObject *)globals, name);
                        uintptr_t storage = vaddr ? (uintptr_t)vaddr : 0;
                        link = s_make_link(storage, (uintptr_t)value, "global", 0);
                        current_obj = value;
                    }
                    Py_DECREF(globals);
                }
            }
        } else if (strcmp(type, "index") == 0) {
            cJSON *idx_item = cJSON_GetObjectItem(step, "index");
            if (!idx_item || !cJSON_IsNumber(idx_item) || !current_obj) continue;
            Py_ssize_t index = (Py_ssize_t)idx_item->valueint;

            if (PyList_Check(current_obj) && index >= 0
                && index < Py_SIZE(current_obj)) {
                PyListObject *list = (PyListObject *)current_obj;
                uintptr_t storage = (uintptr_t)&list->ob_item[index];
                uintptr_t guard = (uintptr_t)&list->ob_item;
                PyObject *value = list->ob_item[index];
                link = s_make_link(storage, (uintptr_t)value, "list_item", guard);
                current_obj = value;
            }
        } else if (strcmp(type, "key") == 0) {
            cJSON *key_item = cJSON_GetObjectItem(step, "key");
            if (!key_item || !cJSON_IsString(key_item) || !current_obj) continue;
            const char *key = key_item->valuestring;

            if (PyDict_Check(current_obj)) {
                PyObject *value = PyDict_GetItemString(current_obj, key);
                if (value) {
                    PyObject **vaddr =
                        s_dict_value_addr((PyDictObject *)current_obj, key);
                    uintptr_t storage = vaddr ? (uintptr_t)vaddr : 0;
                    link = s_make_link(
                        storage, (uintptr_t)value, "dict_key", 0);
                    current_obj = value;
                }
            }
        } else if (strcmp(type, "attr") == 0) {
            cJSON *name_item = cJSON_GetObjectItem(step, "name");
            if (!name_item || !cJSON_IsString(name_item) || !current_obj) continue;
            const char *attr_name = name_item->valuestring;

            PyObject *value = PyObject_GetAttrString(current_obj, attr_name);
            if (!value) { PyErr_Clear(); continue; }

            /* Check whether the attribute is stored in the instance __dict__. */
            PyObject *obj_dict = NULL;
            if (PyObject_HasAttrString(current_obj, "__dict__")) {
                obj_dict = PyObject_GetAttrString(current_obj, "__dict__");
            }

            if (obj_dict && PyDict_Check(obj_dict)
                && PyDict_GetItemString(obj_dict, attr_name)) {
                PyObject **vaddr =
                    s_dict_value_addr((PyDictObject *)obj_dict, attr_name);
                uintptr_t storage = vaddr ? (uintptr_t)vaddr : 0;
                link = s_make_link(storage, (uintptr_t)value, "dict_attr", 0);
            } else {
                /* Slot attribute or computed property. */
                link = s_make_link(0, (uintptr_t)value, "slot_attr", 0);
            }

            Py_XDECREF(obj_dict);
            current_obj = value;
            /* Note: we intentionally do not DECREF value so current_obj stays valid
               for subsequent chain steps. The objects are owned by the debuggee and
               this function runs in a forked process, so leaking is harmless. */
        }

        if (link) {
            cJSON_AddItemToArray(links_array, link);
        }
    }

    Py_DECREF(code);

fail_gil:
    PyGILState_Release(gil);
    cJSON_Delete(chain);

done:
    fprintf(outfile, "%s", cJSON_Print(top_level));
fail_top:
    cJSON_Delete(top_level);
fail_close:
    fclose(outfile);
}
