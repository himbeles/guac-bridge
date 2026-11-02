"""Small real-ConPTY test fixture. No emulated terminal transformations."""
import ctypes as c
from ctypes import wintypes as w
import os
import subprocess
import threading

if os.name != 'nt':
    raise RuntimeError('ConPTY requires Windows')
k = c.WinDLL('kernel32',use_last_error=True)
class COORD(c.Structure):
    _fields_=[('X',c.c_short),('Y',c.c_short)]
class STARTUPINFO(c.Structure):
    _fields_=[('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),('dwFillAttribute',w.DWORD),('dwFlags',w.DWORD),('wShowWindow',w.WORD),('cbReserved2',w.WORD),('lpReserved2',c.c_void_p),('hStdInput',w.HANDLE),('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]
class STARTUPINFOEX(c.Structure):
    _fields_=[('StartupInfo',STARTUPINFO),('lpAttributeList',c.c_void_p)]
class PROCESS_INFORMATION(c.Structure):
    _fields_=[('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]
k.CreatePipe.argtypes=[c.POINTER(w.HANDLE),c.POINTER(w.HANDLE),c.c_void_p,w.DWORD]
k.CreatePipe.restype=w.BOOL
k.CloseHandle.argtypes=[w.HANDLE]
k.CreatePseudoConsole.argtypes=[COORD,w.HANDLE,w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
k.CreatePseudoConsole.restype=c.c_long
k.ClosePseudoConsole.argtypes=[w.HANDLE]
k.InitializeProcThreadAttributeList.argtypes=[c.c_void_p,w.DWORD,w.DWORD,c.POINTER(c.c_size_t)]
k.UpdateProcThreadAttribute.argtypes=[c.c_void_p,w.DWORD,c.c_size_t,c.c_void_p,c.c_size_t,c.c_void_p,c.c_void_p]
k.DeleteProcThreadAttributeList.argtypes=[c.c_void_p]
k.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,w.LPCWSTR,c.POINTER(STARTUPINFOEX),c.POINTER(PROCESS_INFORMATION)]
k.CreateProcessW.restype=w.BOOL
k.ReadFile.argtypes=[w.HANDLE,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
k.ReadFile.restype=w.BOOL
k.WriteFile.argtypes=[w.HANDLE,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p]
k.WriteFile.restype=w.BOOL
k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD]
k.GetExitCodeProcess.argtypes=[w.HANDLE,c.POINTER(w.DWORD)]
k.TerminateProcess.argtypes=[w.HANDLE,w.UINT]
def check(ok):
    if not ok:raise c.WinError(c.get_last_error())
class Terminal:
    def __init__(self,argv):
        self.data=bytearray()
        self.closed=False
        read_in,write_in,read_out,write_out=(w.HANDLE() for _ in range(4))
        check(k.CreatePipe(c.byref(read_in),c.byref(write_in),None,0))
        check(k.CreatePipe(c.byref(read_out),c.byref(write_out),None,0))
        self.input,self.output=write_in,read_out
        self.hpc=w.HANDLE()
        hr=k.CreatePseudoConsole(COORD(120,40),read_in,write_out,0,c.byref(self.hpc))
        k.CloseHandle(read_in);k.CloseHandle(write_out)
        if hr<0:raise OSError(f'CreatePseudoConsole HRESULT {hr:#x}')
        size=c.c_size_t()
        k.InitializeProcThreadAttributeList(None,1,0,c.byref(size))
        attrs=c.create_string_buffer(size.value)
        check(k.InitializeProcThreadAttributeList(attrs,1,0,c.byref(size)))
        try:
            check(k.UpdateProcThreadAttribute(attrs,0,0x00020016,self.hpc,c.sizeof(w.HANDLE),None,None))
            si=STARTUPINFOEX();si.StartupInfo.cb=c.sizeof(si);si.lpAttributeList=c.cast(attrs,c.c_void_p)
            self.process=PROCESS_INFORMATION()
            check(k.CreateProcessW(None,c.create_unicode_buffer(subprocess.list2cmdline(argv)),None,None,False,0x00080000|0x00000400,None,None,c.byref(si),c.byref(self.process)))
            k.CloseHandle(self.process.hThread)
        finally:k.DeleteProcThreadAttributeList(attrs)
        self.thread=threading.Thread(target=self._read,daemon=True);self.thread.start()
    def _read(self):
        buf=c.create_string_buffer(16384);n=w.DWORD()
        while k.ReadFile(self.output,buf,len(buf),c.byref(n),None) and n.value:
            self.data.extend(buf.raw[:n.value])
    def write(self,data):
        n=w.DWORD();offset=0
        while offset<len(data):
            part=data[offset:offset+4096]
            check(k.WriteFile(self.input,part,len(part),c.byref(n),None))
            offset+=n.value
    def wait(self,seconds=20):
        if k.WaitForSingleObject(self.process.hProcess,int(seconds*1000))!=0:
            raise TimeoutError('ConPTY child did not exit')
        code=w.DWORD();check(k.GetExitCodeProcess(self.process.hProcess,c.byref(code)))
        return code.value
    def close(self):
        if self.closed:return
        self.closed=True
        if k.WaitForSingleObject(self.process.hProcess,0)!=0:k.TerminateProcess(self.process.hProcess,1)
        k.ClosePseudoConsole(self.hpc)
        k.CloseHandle(self.input)
        self.thread.join(10)
        k.CloseHandle(self.output);k.CloseHandle(self.process.hProcess)
