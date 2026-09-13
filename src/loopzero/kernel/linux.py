"""Stable process descriptors on Python builds without Linux pidfd bindings.

The libc fallback uses the same kernel operations, never PID-based signalling.
"""
import ctypes
import os
import signal


def _libc_call(name, argtypes, *args):
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(libc, name)
    except AttributeError as exc:
        raise OSError(38, f"{name} is unavailable") from exc
    function.argtypes = argtypes
    function.restype = ctypes.c_int
    result = function(*args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def pidfd_open(pid: int) -> int:
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _libc_call("pidfd_open", (ctypes.c_int, ctypes.c_uint), pid, 0)


def pidfd_send_signal(descriptor: int, signum: int) -> None:
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, signum)
        return
    _libc_call("pidfd_send_signal", (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint),
               descriptor, signum, None, 0)
