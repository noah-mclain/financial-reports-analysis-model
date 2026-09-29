"""This process's lifetime peak memory (spec 10, Peak memory).

On Apple silicon, memory PyTorch allocates through MPS is not all counted in RSS, so on macOS
the figure is the lifetime peak physical footprint, the one Activity Monitor shows. The child
reads it for itself just before writing: once it exits the figure is gone (R26).
"""

from __future__ import annotations

import ctypes
import os
import resource
import sys
from typing import Any, ClassVar

_RUSAGE_INFO_V4 = 4
_GIB = 1024**3
_V4_FIELDS = (
    "ri_user_time",
    "ri_system_time",
    "ri_pkg_idle_wkups",
    "ri_interrupt_wkups",
    "ri_pageins",
    "ri_wired_size",
    "ri_resident_size",
    "ri_phys_footprint",
    "ri_proc_start_abstime",
    "ri_proc_exit_abstime",
    "ri_child_user_time",
    "ri_child_system_time",
    "ri_child_pkg_idle_wkups",
    "ri_child_interrupt_wkups",
    "ri_child_pageins",
    "ri_child_elapsed_abstime",
    "ri_diskio_bytesread",
    "ri_diskio_byteswritten",
    "ri_cpu_time_qos_default",
    "ri_cpu_time_qos_maintenance",
    "ri_cpu_time_qos_background",
    "ri_cpu_time_qos_utility",
    "ri_cpu_time_qos_legacy",
    "ri_cpu_time_qos_user_initiated",
    "ri_cpu_time_qos_user_interactive",
    "ri_billed_system_time",
    "ri_serviced_system_time",
    "ri_logical_writes",
    "ri_lifetime_max_phys_footprint",
    "ri_instructions",
    "ri_cycles",
    "ri_billed_energy",
    "ri_serviced_energy",
    "ri_interval_max_phys_footprint",
    "ri_runnable_time",
)


class _RusageInfoV4(ctypes.Structure):
    """``struct rusage_info_v4`` from <sys/resource.h>."""

    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        *((name, ctypes.c_uint64) for name in _V4_FIELDS),
    ]


def peak_footprint_reading() -> tuple[float, bool]:
    """The current figure, and whether it came from the RSS fallback rather than the footprint
    read. Only macOS has a footprint read to fall back from; elsewhere ``ru_maxrss`` is always
    the answer, so the flag is always False there."""
    if sys.platform == "darwin":
        info = _RusageInfoV4()
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if libsystem.proc_pid_rusage(os.getpid(), _RUSAGE_INFO_V4, ctypes.byref(info)) == 0:
            return float(info.ri_lifetime_max_phys_footprint) / _GIB, False
    # Reached on Linux, or on macOS when proc_pid_rusage failed: there, this undercounts MPS
    # memory, so the caller needs to know it came from the fallback (R26).
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    on_macos = sys.platform == "darwin"
    return (peak / _GIB if on_macos else peak * 1024 / _GIB), on_macos


def peak_footprint_gb() -> float:
    return peak_footprint_reading()[0]
