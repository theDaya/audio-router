"""Capture what every app plays, except this one: Windows' process loopback (Windows 10 2004+ / Windows 11).

No driver is needed. Because our own process is excluded, what the router itself plays (a mic it sends to the
headphones, say) never comes back in as "PC audio".

Raw COM through ctypes: ActivateAudioInterfaceAsync on the virtual process-loopback device gives an IAudioClient,
which is read like any WASAPI capture client.
"""
import ctypes
import os
import threading
from ctypes import POINTER, byref, c_void_p, wintypes

import numpy as np

RATE = 48000
CH = 2

_ole32 = ctypes.OleDLL("ole32")
_mmdevapi = ctypes.WinDLL("Mmdevapi")
_kernel32 = ctypes.WinDLL("kernel32")


class GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        _ole32.CLSIDFromString(ctypes.c_wchar_p("{" + s + "}"), byref(self))

    def __eq__(self, other):
        return bytes(self) == bytes(other)


IID_IUnknown = GUID("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90")
IID_IActivateCompletionHandler = GUID("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = GUID("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID("C8ADBD64-E71E-48A0-A4DE-185C395CD317")

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65
WAVE_FORMAT_IEEE_FLOAT = 3


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", ctypes.c_int)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.WORD), ("r2", wintypes.WORD), ("r3", wintypes.WORD),
                ("blob", BLOB)]


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


def _call(obj, index, argtypes, *args):
    """Call method `index` of a COM interface pointer; a failed HRESULT raises OSError."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtbl[index])(obj, *args)


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)(vtbl[2])(obj)


# The completion handler is a minimal COM object built by hand: a vtable of ctypes callbacks. It must report
# IAgileObject, or Windows refuses to call it from its worker thread.
_QI = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_REF = ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)
_DONE = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_void_p)


class _HandlerVtbl(ctypes.Structure):
    _fields_ = [("QueryInterface", _QI), ("AddRef", _REF), ("Release", _REF), ("ActivateCompleted", _DONE)]


class _Handler(ctypes.Structure):
    _fields_ = [("vtbl", POINTER(_HandlerVtbl))]


class _Completion:
    def __init__(self):
        self.done = threading.Event()

        def qi(this, riid, out):
            if riid.contents in (IID_IUnknown, IID_IAgileObject, IID_IActivateCompletionHandler):
                out[0] = this
                return 0
            out[0] = None
            return -2147467262  # E_NOINTERFACE

        def completed(this, op):
            self.done.set()
            return 0

        # the callbacks and structures must outlive the activation, so they hang off self
        self.vtbl = _HandlerVtbl(_QI(qi), _REF(lambda this: 1), _REF(lambda this: 1), _DONE(completed))
        self.obj = _Handler(ctypes.pointer(self.vtbl))
        self.ptr = ctypes.cast(ctypes.pointer(self.obj), c_void_p)


def supported():
    """True if this Windows has process loopback (it needs Windows 10 2004 or later)."""
    return hasattr(_mmdevapi, "ActivateAudioInterfaceAsync") and _activate_client(probe=True) is not None


def _activate_client(probe=False):
    """An IAudioClient on the process-loopback device, excluding this process tree; None if Windows refuses."""
    params = AUDIOCLIENT_ACTIVATION_PARAMS(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, os.getpid(),
                                           PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
    pv = PROPVARIANT(vt=VT_BLOB, blob=BLOB(ctypes.sizeof(params), ctypes.cast(ctypes.pointer(params), c_void_p)))
    handler = _Completion()
    op = c_void_p()
    try:
        _mmdevapi.ActivateAudioInterfaceAsync.restype = ctypes.HRESULT
        _mmdevapi.ActivateAudioInterfaceAsync(ctypes.c_wchar_p(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK),
                                              byref(IID_IAudioClient), byref(pv), handler.ptr, byref(op))
        if not handler.done.wait(5):
            return None
        hr, unk = ctypes.HRESULT(), c_void_p()
        _call(op, 3, (POINTER(ctypes.HRESULT), POINTER(c_void_p)), byref(hr), byref(unk))   # GetActivateResult
        if hr.value < 0 or not unk:
            return None
        client = c_void_p()
        _call(unk, 0, (POINTER(GUID), POINTER(c_void_p)), byref(IID_IAudioClient), byref(client))
        _release(unk)
        if probe:
            _release(client)
            return True
        return client
    except OSError:
        return None
    finally:
        _release(op)


class ProcessLoopback:
    """Calls callback(frames: float32 array (n, 2)) with everything other apps play, at 48 kHz, from its own thread.
    Like an endpoint loopback, it sends nothing while nothing plays."""

    def __init__(self, callback):
        self.callback = callback
        self.stop = threading.Event()
        self.error = None
        self.silent_packets = self.data_packets = 0   # diagnostics: Windows flags silent packets instead of sending data
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.ready.wait(6)
        if self.error:
            raise OSError(self.error)

    def _run(self):
        _ole32.CoInitializeEx(None, 0)   # this thread joins the multithreaded apartment
        client = capture = None
        event = _kernel32.CreateEventW(None, False, False, None)
        try:
            client = _activate_client()
            if not client:
                raise OSError("process loopback unavailable (needs Windows 10 2004 or later)")
            fmt = WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, CH, RATE, RATE * CH * 4, CH * 4, 32, 0)
            _call(client, 3, (ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                              POINTER(WAVEFORMATEX), c_void_p),
                  AUDCLNT_SHAREMODE_SHARED,
                  AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                  200000, 0, byref(fmt), None)                                       # Initialize, 20 ms buffer
            _call(client, 13, (wintypes.HANDLE,), event)                            # SetEventHandle
            capture = c_void_p()
            _call(client, 14, (POINTER(GUID), POINTER(c_void_p)), byref(IID_IAudioCaptureClient), byref(capture))
            _call(client, 10, ())                                                   # Start
        except OSError as e:
            self.error = str(e)
            self.ready.set()
            _release(capture)
            _release(client)
            _kernel32.CloseHandle(event)
            return
        self.ready.set()
        data, frames, flags, size = c_void_p(), wintypes.UINT(), wintypes.DWORD(), wintypes.UINT()
        try:
            while not self.stop.is_set():
                _kernel32.WaitForSingleObject(event, 100)
                while True:
                    _call(capture, 5, (POINTER(wintypes.UINT),), byref(size))          # GetNextPacketSize
                    if not size.value:
                        break
                    _call(capture, 3, (POINTER(c_void_p), POINTER(wintypes.UINT), POINTER(wintypes.DWORD),
                                       c_void_p, c_void_p),
                          byref(data), byref(frames), byref(flags), None, None)         # GetBuffer
                    n = frames.value
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data:
                        block = np.zeros((n, CH), np.float32)
                        self.silent_packets += 1
                    else:
                        self.data_packets += 1
                        block = np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_float)),
                                                      (n * CH,)).reshape(n, CH).copy()
                    _call(capture, 4, (wintypes.UINT,), n)                              # ReleaseBuffer
                    try:
                        self.callback(block)
                    except Exception:
                        pass
        except OSError as e:
            self.error = str(e)
        finally:
            try:
                _call(client, 11, ())                                                   # Stop
            except OSError:
                pass
            _release(capture)
            _release(client)
            _kernel32.CloseHandle(event)

    def is_active(self):
        return self.thread.is_alive()

    def close(self):
        self.stop.set()
        self.thread.join(2)
