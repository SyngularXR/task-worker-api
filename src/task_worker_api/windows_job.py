"""Windows process-tree budgets with optional AppContainer isolation.

Jobs alone are not a security boundary. An AppContainer additionally requires
explicit ACLs on staged paths; never grant access to supervisor state. Processes
join the job and security token atomically and stay suspended until recorded.
Calls for one job must be serialized by its owning supervisor.
"""
import ctypes as c
from ctypes import wintypes as w
import os
import re
from pathlib import Path
import subprocess
import time
from contextlib import ExitStack


class _BasicLimits(c.Structure):
    _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                ("min_working_set", c.c_size_t), ("max_working_set", c.c_size_t),
                ("process_limit", w.DWORD), ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class _Limits(c.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", c.c_uint64 * 6), ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t), ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]


class _Cpu(c.Structure):
    _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]


class _SecurityCapabilities(c.Structure):
    _fields_ = [("sid", c.c_void_p), ("capabilities", c.c_void_p), ("count", w.DWORD), ("reserved", w.DWORD)]


class _SidAndAttributes(c.Structure):
    _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]


class _CompletionPort(c.Structure):
    _fields_ = [("key", c.c_void_p), ("port", w.HANDLE)]


class _Accounting(c.Structure):
    _fields_ = [("times", c.c_int64 * 4), ("faults", w.DWORD), ("total", w.DWORD),
                ("active", w.DWORD), ("terminated", w.DWORD)]


class _Startup(c.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                *[(name, w.DWORD) for name in ("x", "y", "width", "height", "chars_x", "chars_y", "fill", "flags")],
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_bytes", c.c_void_p),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [("startup", _Startup), ("attributes", c.c_void_p)]


class _ProcessInfo(c.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def _check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


def _creation_time(kernel, handle):
    creation, exited, system, user = (c.c_uint64() for _ in range(4))
    _check(kernel.GetProcessTimes(handle, c.byref(creation), c.byref(exited), c.byref(system), c.byref(user)))
    return creation.value


def reconcile_windows_process(*, host_id, boot_id, pid, creation_time, timeout_ms, terminate=False):
    """Wait for one journaled process identity, never infer whole-tree cleanup.

    A reused PID means the recorded process is gone; its replacement is untouched.
    Access/inspection errors are unknown, not proof of exit. The caller must also
    reconcile all descendants and storage before signing attempt cleanup.
    """
    from .host_reporter import physical_boot_id
    from .resources import AdmissionError

    if os.name != "nt":
        raise OSError("Windows process reconciliation requires Windows")
    if (type(pid) is not int or not 0 < pid <= 0xffffffff or type(creation_time) is not int
            or not 0 < creation_time < 2**64 or type(timeout_ms) is not int
            or not 0 <= timeout_ms <= 60000 or type(terminate) is not bool):
        raise ValueError("Invalid Windows process identity or wait")
    if physical_boot_id(host_id, Path("/proc")) != boot_id:
        raise AdmissionError("attempt_fenced")
    kernel = c.WinDLL("kernel32", use_last_error=True)
    for name, args, result in (
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("GetProcessTimes", [w.HANDLE, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], w.BOOL),
        ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
        ("TerminateProcess", [w.HANDLE, w.UINT], w.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    handle = kernel.OpenProcess(0x100000 | 0x1000 | (1 if terminate else 0), False, pid)
    if not handle:
        if c.get_last_error() == 87:  # Valid nonzero PID no longer exists.
            return True
        raise c.WinError(c.get_last_error())
    try:
        if _creation_time(kernel, handle) != creation_time:
            return True
        if terminate and kernel.WaitForSingleObject(handle, 0) == 258:
            if not kernel.TerminateProcess(handle, 1):
                error = c.get_last_error()
                if kernel.WaitForSingleObject(handle, 0) != 0:
                    raise c.WinError(error)
        result = kernel.WaitForSingleObject(handle, timeout_ms)
        if result not in (0, 258):
            raise c.WinError(c.get_last_error())
        return result == 0
    finally:
        _check(kernel.CloseHandle(handle))


class WindowsJob:
    def __init__(self, *, memory_bytes, cpu_rate, process_limit):
        if os.name != "nt":
            raise OSError("Windows jobs require Windows 10 or newer")
        if (type(memory_bytes) is not int or memory_bytes <= 0 or type(cpu_rate) is not int
                or not 1 <= cpu_rate <= 10000 or type(process_limit) is not int or not 1 <= process_limit <= 65535):
            raise ValueError("Invalid Windows job limits")
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "InitializeProcThreadAttributeList": ([c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
            "UpdateProcThreadAttribute": ([c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
            "DeleteProcThreadAttributeList": ([c.c_void_p], None),
            "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD,
                                c.c_void_p, w.LPCWSTR, c.POINTER(_StartupEx), c.POINTER(_ProcessInfo)], w.BOOL),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "GetProcessTimes": ([w.HANDLE, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            "CreateIoCompletionPort": ([w.HANDLE, w.HANDLE, c.c_size_t, w.DWORD], w.HANDLE),
            "GetQueuedCompletionStatus": ([w.HANDLE, c.POINTER(w.DWORD), c.POINTER(c.c_size_t),
                                            c.POINTER(c.c_void_p), w.DWORD], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "DuplicateHandle": ([w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE),
                                 w.DWORD, w.BOOL, w.DWORD], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = _check(self.kernel.CreateJobObjectW(None, None))
        self.processes = []
        self.port = None
        self.observed = {}
        self.stopping = False
        try:
            limits = _Limits()
            # No breakaway flags: descendants stay in this job.
            limits.basic.flags = 0x2000 | 0x200 | 0x400 | 0x8
            limits.basic.process_limit, limits.job_memory = process_limit, memory_bytes
            _check(self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
            cpu = _Cpu(0x1 | 0x4, cpu_rate)  # ENABLE | HARD_CAP; rate is relative to the parent job/system.
            _check(self.kernel.SetInformationJobObject(self.handle, 15, c.byref(cpu), c.sizeof(cpu)))
            self.port = _check(self.kernel.CreateIoCompletionPort(w.HANDLE(-1), None, 0, 1))
            port = _CompletionPort(1, self.port)
            _check(self.kernel.SetInformationJobObject(self.handle, 7, c.byref(port), c.sizeof(port)))
        except BaseException:
            self.close()
            raise

    def spawn(self, command, *, cwd, environment, log_path=None, app_container=None):
        """Create suspended; inherit only explicit log/NUL handles when requested.

        AppContainer launch requires LOCALAPPDATA in the selected environment;
        Windows redirects it to the profile. No parent variables are auto-copied.
        """
        if not self.handle:
            raise RuntimeError("Job is closed")
        if self.stopping:
            raise RuntimeError("Job is stopping; it cannot launch more processes")
        if app_container is not None and (not isinstance(app_container, WindowsAppContainer) or not app_container.sid):
            raise ValueError("AppContainer must be an open profile")
        if not command or any(not isinstance(value, str) or "\0" in value for value in command):
            raise ValueError("Invalid command")
        executable = Path(command[0]).resolve(strict=True)
        if not Path(command[0]).is_absolute() or not executable.is_file():
            raise ValueError("Executable must be an absolute file path")
        cwd = Path(cwd).resolve(strict=True)
        if not cwd.is_dir() or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                                  or not isinstance(value, str) or "\0" in value for key, value in environment.items()):
            raise ValueError("Invalid working directory or environment")
        block = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in
                                                  sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0")
        size = c.c_size_t()
        attribute_count = 1 + (log_path is not None) + (app_container is not None)
        self.kernel.InitializeProcThreadAttributeList(None, attribute_count, 0, c.byref(size))
        storage = c.create_string_buffer(size.value)
        _check(self.kernel.InitializeProcThreadAttributeList(storage, attribute_count, 0, c.byref(size)))
        info = _ProcessInfo()
        files = ExitStack()
        try:
            jobs = (w.HANDLE * 1)(self.handle)
            # PROC_THREAD_ATTRIBUTE_JOB_LIST: assignment occurs inside CreateProcess.
            _check(self.kernel.UpdateProcThreadAttribute(storage, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
            if app_container is not None:
                security = _SecurityCapabilities(app_container.sid,
                    c.cast(app_container.capabilities, c.c_void_p) if app_container.capabilities else None,
                    len(app_container.capabilities), 0)
                _check(self.kernel.UpdateProcThreadAttribute(storage, 0, 0x20009,
                    c.byref(security), c.sizeof(security), None, None))
            startup = _StartupEx()
            startup.startup.cb = c.sizeof(startup)
            startup.attributes = c.cast(storage, c.c_void_p)
            if log_path is not None:
                import msvcrt

                output = files.enter_context(Path(log_path).open("xb"))
                incoming = files.enter_context(open(os.devnull, "rb"))
                handles = (w.HANDLE * 2)(msvcrt.get_osfhandle(incoming.fileno()), msvcrt.get_osfhandle(output.fileno()))
                for handle in handles:
                    os.set_handle_inheritable(handle, True)
                _check(self.kernel.UpdateProcThreadAttribute(storage, 0, 0x20002, handles, c.sizeof(handles), None, None))
                startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
                startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = handles[0], handles[1], handles[1]
            line = c.create_unicode_buffer(subprocess.list2cmdline([str(executable), *command[1:]]))
            _check(self.kernel.CreateProcessW(str(executable), line, None, None, log_path is not None,
                0x4 | 0x400 | 0x80000 | 0x08000000, block, str(cwd), c.byref(startup), c.byref(info)))
            process = WindowsJobProcess(self, info)
            self.processes.append(process)
            return process
        except BaseException:
            self.close()
            for handle in (info.thread, info.process):
                if handle:
                    self.kernel.CloseHandle(handle)
            raise
        finally:
            self.kernel.DeleteProcThreadAttributeList(storage)
            files.close()

    def pids(self):
        if not self.handle:
            raise RuntimeError("Job is closed")
        count = 16
        while True:
            data = c.create_string_buffer(8 + count * c.sizeof(c.c_size_t))
            if self.kernel.QueryInformationJobObject(self.handle, 3, data, len(data), None):
                actual = w.DWORD.from_buffer(data, 4).value
                return list((c.c_size_t * actual).from_buffer(data, 8))
            if c.get_last_error() != 234:  # ERROR_MORE_DATA
                raise c.WinError(c.get_last_error())
            count = max(count * 2, w.DWORD.from_buffer(data).value)

    def limits(self):
        limits, cpu = _Limits(), _Cpu()
        _check(self.kernel.QueryInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits), None))
        _check(self.kernel.QueryInformationJobObject(self.handle, 15, c.byref(cpu), c.sizeof(cpu), None))
        return {"flags": limits.basic.flags, "memory_bytes": limits.job_memory,
                "peak_job_memory_bytes": limits.peak_job_memory, "process_limit": limits.basic.process_limit,
                "cpu_flags": cpu.flags, "cpu_rate": cpu.rate}

    def close(self):
        if self.handle:
            self.terminate()
            _check(self.kernel.CloseHandle(self.handle))
            self.handle = None
        for process in self.processes:
            process.close()
        self.processes.clear()
        for record in self.observed.values():
            if record['handle']:
                _check(self.kernel.CloseHandle(record['handle']))
        self.observed.clear()
        if self.port:
            _check(self.kernel.CloseHandle(self.port))
            self.port = None

    def terminate(self):
        """Request tree termination; callers must verify emptiness before release."""
        if not self.handle:
            raise RuntimeError("Job is closed")
        self.stopping = True
        _check(self.kernel.TerminateJobObject(self.handle, 1))

    def _observe_process(self, pid):
        record = self.observed.setdefault(pid, dict(handle=None, creation_time=None, exited=False))
        if record['handle'] or record['exited']:
            return
        root = next((process for process in self.processes if process.pid == pid and process.handle), None)
        if root:
            handle = w.HANDLE()
            _check(self.kernel.DuplicateHandle(w.HANDLE(-1), root.handle, w.HANDLE(-1),
                                               c.byref(handle), 0, False, 2))
            record['handle'] = handle.value
        else:
            record['handle'] = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
            if not record['handle']:
                if c.get_last_error() == 87:
                    record['exited'] = True
                    return
                raise c.WinError(c.get_last_error())
        record['creation_time'] = _creation_time(self.kernel, record['handle'])

    def _collect_processes(self, deadline):
        while time.monotonic() < deadline:
            message, key, pid = w.DWORD(), c.c_size_t(), c.c_void_p()
            if not self.kernel.GetQueuedCompletionStatus(self.port, c.byref(message), c.byref(key), c.byref(pid), 0):
                if c.get_last_error() == 258:
                    return True
                raise c.WinError(c.get_last_error())
            if key.value != 1:
                raise RuntimeError('Unexpected job notification key')
            if message.value == 6:  # JOB_OBJECT_MSG_NEW_PROCESS
                if not pid.value:
                    raise RuntimeError('Missing process identity in job notification')
                self._observe_process(pid.value)
        return False

    def wait_stopped(self, timeout_ms):
        """Stop and verify observed process handles against kernel lifetime count.

        Missing notifications or PID reuse can undercount and keep cleanup unknown.
        Never use an EXIT/ACTIVE_PROCESS_ZERO message or empty membership as proof.
        Only this object's retained completion port and handles can establish it.
        """
        if type(timeout_ms) is not int or not 0 < timeout_ms <= 60000:
            raise ValueError('Invalid job cleanup timeout')
        self.terminate()
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if not self._collect_processes(deadline):
                return False
            for pid, record in self.observed.items():
                self._observe_process(pid)
                if not record['exited']:
                    remaining = max(0, int((deadline - time.monotonic()) * 1000))
                    result = self.kernel.WaitForSingleObject(record['handle'], remaining)
                    if result == 258:
                        return False
                    if result != 0:
                        raise c.WinError(c.get_last_error())
                    record['exited'] = True
            accounting = _Accounting()
            _check(self.kernel.QueryInformationJobObject(self.handle, 1, c.byref(accounting), c.sizeof(accounting), None))
            if accounting.active == 0 and accounting.total == len(self.observed) and not self.pids():
                return True
            time.sleep(0.01)
        return False

    def wait_empty(self, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        while self.pids():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WindowsAppContainer:
    """Fresh profile, offline by default. Close only after its job has stopped.

    The caller records the name durably before creation and grants the returned
    SID access only to staged runtime/input/scratch paths. Existing profiles are
    rejected, never silently adopted. network=True grants Internet-client and
    private-network access, not a destination allowlist or a loopback exemption.
    """
    def __init__(self, name, *, network=False):
        if os.name != "nt":
            raise OSError("AppContainer requires Windows")
        if not isinstance(name, str) or not name.startswith("surgiclaw.") or not name[10:].isalnum() or len(name) > 64:
            raise ValueError("Invalid worker AppContainer name")
        if type(network) is not bool:
            raise ValueError("network must be a boolean")
        self.name, self.sid = name, c.c_void_p()
        self.userenv = c.WinDLL("userenv", use_last_error=True)
        self.userenv.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR,
            c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]
        self.userenv.CreateAppContainerProfile.restype = c.c_long
        self.advapi = c.WinDLL("advapi32", use_last_error=True)
        self.advapi.FreeSid.argtypes = [c.c_void_p]
        self.advapi.FreeSid.restype = c.c_void_p
        self.advapi.CreateWellKnownSid.argtypes = [c.c_int, c.c_void_p, c.c_void_p, c.POINTER(w.DWORD)]
        self.advapi.CreateWellKnownSid.restype = w.BOOL
        self.capability_storage = []
        self.capabilities = (_SidAndAttributes * (2 if network else 0))()
        for index, kind in enumerate((85, 87) if network else ()):
            # WinCapabilityInternetClientSid / WinCapabilityPrivateNetworkClientServerSid.
            storage, size = c.create_string_buffer(68), w.DWORD(68)  # SECURITY_MAX_SID_SIZE
            _check(self.advapi.CreateWellKnownSid(kind, None, storage, c.byref(size)))
            self.capability_storage.append(storage)
            self.capabilities[index] = _SidAndAttributes(c.cast(storage, c.c_void_p), 4)  # SE_GROUP_ENABLED
        result = self.userenv.CreateAppContainerProfile(name, name, "Isolated task worker",
            self.capabilities if self.capabilities else None, len(self.capabilities), c.byref(self.sid))
        if result < 0:
            raise OSError(f"CreateAppContainerProfile failed: 0x{result & 0xffffffff:08x}")

    def close(self):
        if self.sid:
            self.delete_profile(self.name)
            self.advapi.FreeSid(self.sid)
            self.sid = c.c_void_p()

    def sid_string(self):
        if not self.sid:
            raise ValueError('AppContainer is closed')
        value = w.LPWSTR()
        self.advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
        self.advapi.ConvertSidToStringSidW.restype = w.BOOL
        _check(self.advapi.ConvertSidToStringSidW(self.sid, c.byref(value)))
        try:
            return value.value
        finally:
            kernel = c.WinDLL('kernel32', use_last_error=True)
            kernel.LocalFree.argtypes = [c.c_void_p]
            kernel.LocalFree.restype = c.c_void_p
            kernel.LocalFree(value)

    @staticmethod
    def path_access(path, sid, access):
        """Change only an attempt SID's explicit grant; descendants inherit it."""
        if access not in ('RX', 'M', None) or not re.fullmatch(r'S-1-15-2(?:-[0-9]+){7}', sid):
            raise ValueError('Invalid AppContainer path grant')
        path = Path(path).absolute()
        if path.resolve(strict=True) != path or path == Path(path.anchor):
            raise ValueError('Unsafe AppContainer grant path')
        permissions = ('(OI)(CI)' if path.is_dir() else '') + f'({access})'
        args = ['/remove:g', '*' + sid] if access is None else ['/grant:r', f'*{sid}:{permissions}']
        # No /T: never create explicit grants throughout a shared runtime tree.
        subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/icacls.exe'), str(path),
            *args, '/L', '/Q'], check=True, capture_output=True, timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)

    @staticmethod
    def delete_profile(name):
        """Retry deletion of an owned profile after its process tree has exited."""
        if not isinstance(name, str) or not name.startswith("surgiclaw.") or not name[10:].isalnum() or len(name) > 64:
            raise ValueError("Invalid worker AppContainer name")
        userenv = c.WinDLL("userenv", use_last_error=True)
        userenv.DeleteAppContainerProfile.argtypes = [w.LPCWSTR]
        userenv.DeleteAppContainerProfile.restype = c.c_long
        result = userenv.DeleteAppContainerProfile(name)
        if result < 0:
            raise OSError(f"DeleteAppContainerProfile failed: 0x{result & 0xffffffff:08x}")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class WindowsJobProcess:
    def __init__(self, job, info):
        self.job, self.handle, self.thread, self.pid = job, info.process, info.thread, info.pid
        self.creation_time = _creation_time(job.kernel, self.handle)

    def resume(self):
        if not self.thread:
            raise RuntimeError("Process already resumed or closed")
        if self.job.kernel.ResumeThread(self.thread) == 0xFFFFFFFF:
            raise c.WinError(c.get_last_error())
        _check(self.job.kernel.CloseHandle(self.thread))
        self.thread = None

    def wait(self, timeout_ms):
        result = self.job.kernel.WaitForSingleObject(self.handle, timeout_ms)
        if result == 258:
            return None
        if result != 0:
            raise c.WinError(c.get_last_error())
        code = w.DWORD()
        _check(self.job.kernel.GetExitCodeProcess(self.handle, c.byref(code)))
        return code.value

    def close(self):
        for name in ("thread", "handle"):
            handle = getattr(self, name)
            if handle:
                _check(self.job.kernel.CloseHandle(handle))
                setattr(self, name, None)
