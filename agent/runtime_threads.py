"""Thread-pool bounds and orphan-exit protection for embedded Python workers."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import os
import threading


_OPENCV_DLL_NAME = "opencv_world4_maa.dll"
_WATCHDOG_STARTED = False
_WATCHDOG_LOCK = threading.Lock()
_LOADED_OPENCV_DLL = None
_DLL_DIR_COOKIE = None


def configure_env_thread_limits(max_threads: int = 2) -> int:
    """Cap OpenMP / BLAS thread pools before importing native scientific libraries."""
    cpus = os.cpu_count() or max_threads
    limit = max(1, min(int(max_threads), int(cpus)))
    value = str(limit)
    os.environ.setdefault("OMP_NUM_THREADS", value)
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", value)
    os.environ.setdefault("MKL_NUM_THREADS", value)
    os.environ.setdefault("NUMEXPR_NUM_THREADS", value)
    return limit


def cap_loaded_maafw_opencv(max_threads: int = 2) -> bool:
    """Limit opencv_world4_maa.dll's ConcRT worker pool if present in MAAFW_BINARY_PATH or process."""
    global _LOADED_OPENCV_DLL, _DLL_DIR_COOKIE
    if os.name != "nt":
        return False
    cpus = os.cpu_count() or max_threads
    limit = max(1, min(int(max_threads), int(cpus)))
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        handle = kernel32.GetModuleHandleW(_OPENCV_DLL_NAME)
        if not handle:
            maafw_dir = os.environ.get("MAAFW_BINARY_PATH")
            if maafw_dir:
                dll_path = Path(maafw_dir) / _OPENCV_DLL_NAME
                if dll_path.is_file():
                    if _DLL_DIR_COOKIE is None and hasattr(os, "add_dll_directory"):
                        try:
                            _DLL_DIR_COOKIE = os.add_dll_directory(str(dll_path.parent))
                        except OSError:
                            pass
                    if _LOADED_OPENCV_DLL is None:
                        _LOADED_OPENCV_DLL = ctypes.CDLL(str(dll_path))
                    handle = getattr(_LOADED_OPENCV_DLL, "_handle", None)
        if not handle:
            return False
        kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
        kernel32.GetProcAddress.restype = ctypes.c_void_p
        proc = kernel32.GetProcAddress(handle, b"cvSetNumThreads")
        if not proc:
            return False
        setter = ctypes.CFUNCTYPE(None, ctypes.c_int)(proc)
        setter(limit)
        return True
    except Exception:
        return False


def configure_worker_threads(max_threads: int = 2) -> int:
    """Cap native OpenCV, OpenMP, and BLAS pools to avoid CONCRT140 wake storms."""
    limit = configure_env_thread_limits(max_threads)
    try:
        import cv2
        cv2.setNumThreads(limit)
    except Exception:
        pass
    cap_loaded_maafw_opencv(limit)
    return limit


def start_parent_exit_watchdog(on_parent_exit=None) -> bool:
    """Ensure orphaned child workers exit promptly if their parent host disappears unexpectedly."""
    global _WATCHDOG_STARTED
    if os.name != "nt":
        return False
    ppid = int(os.getppid() or 0)
    if ppid <= 4:
        return False
    with _WATCHDOG_LOCK:
        if _WATCHDOG_STARTED:
            return True
        _WATCHDOG_STARTED = True

    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    INFINITE = 0xFFFFFFFF
    STILL_ACTIVE = 259

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        h_parent = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, ppid)
        if not h_parent:
            return False
    except Exception:
        return False

    def _watch() -> None:
        try:
            code = wintypes.DWORD(0)
            if kernel32.GetExitCodeProcess(h_parent, ctypes.byref(code)) and code.value == STILL_ACTIVE:
                kernel32.WaitForSingleObject(h_parent, INFINITE)
        except Exception:
            return
        finally:
            try:
                kernel32.CloseHandle(h_parent)
            except Exception:
                pass
        if on_parent_exit is not None:
            def _force_exit():
                os._exit(0)
            # Bound teardown so a blocked native C++ join() cannot strand an orphan process.
            stopper = threading.Timer(3.0, _force_exit)
            stopper.daemon = True
            stopper.start()
            try:
                on_parent_exit()
            except Exception:
                pass
            finally:
                stopper.cancel()
        os._exit(0)

    thread = threading.Thread(target=_watch, name="parent-exit-watchdog", daemon=True)
    thread.start()
    return True
