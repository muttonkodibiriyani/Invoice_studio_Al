"""Process limits for the code that touches uploaded files (security review M4).

Children set their own limits first thing, before they open the file, so the limits hold even if a parser
bug takes over the child. POSIX only: Windows has no rlimits, and there the wall-clock timeouts remain.
"""
import ctypes
import sys


def limit(cpu_seconds,max_as_mb=None):
    try:import resource
    except ImportError:return
    wanted=[(resource.RLIMIT_CPU,cpu_seconds)]
    if max_as_mb:wanted.append((resource.RLIMIT_AS,max_as_mb*1024*1024))
    for name,value in wanted:
        hard=resource.getrlimit(name)[1]
        if hard!=resource.RLIM_INFINITY:value=min(value,hard)
        resource.setrlimit(name,(value,value))


def protect_process():
    """Linux: a same-uid child can no longer read this process's environment (the database URL and the vault
    key in the cloud) through /proc/<pid>/environ, or ptrace it. execve resets the flag, so children run as usual."""
    if sys.platform.startswith("linux"):
        ctypes.CDLL(None,use_errno=True).prctl(4,0,0,0,0)  # PR_SET_DUMPABLE, 0
