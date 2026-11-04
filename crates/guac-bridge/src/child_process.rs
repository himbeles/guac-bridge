//! Windows background children inherit only their explicitly supplied stdio.
use std::{
    ffi::OsStr,
    io,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
        process::ExitStatusExt,
    },
    process::{Command, ExitStatus},
    ptr,
};
use windows_sys::Win32::{Foundation::*, System::Threading::*};

pub(crate) struct Child {
    process: OwnedHandle,
    pid: u32,
}

impl Child {
    pub(crate) fn id(&self) -> u32 {
        self.pid
    }

    pub(crate) fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        self.wait_for(0)
    }

    pub(crate) fn wait(&mut self) -> io::Result<ExitStatus> {
        self.wait_for(INFINITE)?
            .ok_or_else(|| io::Error::other("Process wait timed out"))
    }

    fn wait_for(&self, timeout: u32) -> io::Result<Option<ExitStatus>> {
        // SAFETY: process owns a live process handle for this entire call.
        unsafe {
            match WaitForSingleObject(self.process.as_raw_handle(), timeout) {
                WAIT_TIMEOUT => Ok(None),
                WAIT_OBJECT_0 => {
                    let mut code = 0;
                    check(GetExitCodeProcess(self.process.as_raw_handle(), &mut code))?;
                    Ok(Some(ExitStatus::from_raw(code)))
                }
                _ => Err(io::Error::last_os_error()),
            }
        }
    }

    pub(crate) fn kill(&mut self) -> io::Result<()> {
        // SAFETY: process owns a live process handle.
        unsafe { check(TerminateProcess(self.process.as_raw_handle(), 1)) }
    }
}

fn check(result: i32) -> io::Result<()> {
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn inheritable(handle: &impl AsRawHandle) -> io::Result<OwnedHandle> {
    let mut copy = ptr::null_mut();
    // SAFETY: the source is borrowed for the call; the successful duplicate is owned below.
    unsafe {
        let process = GetCurrentProcess();
        check(DuplicateHandle(
            process,
            handle.as_raw_handle(),
            process,
            &mut copy,
            0,
            1,
            DUPLICATE_SAME_ACCESS,
        ))?;
        Ok(OwnedHandle::from_raw_handle(copy))
    }
}

struct Attributes(Vec<usize>);
impl Drop for Attributes {
    fn drop(&mut self) {
        // SAFETY: constructed only after successful initialization; backing storage is still alive.
        unsafe { DeleteProcThreadAttributeList(self.0.as_mut_ptr().cast()) }
    }
}

// Quote UTF-16 directly, preserving non-Unicode paths and Microsoft's argv escaping rules.
fn quote(value: &OsStr) -> io::Result<Vec<u16>> {
    let mut result = vec![b'"' as u16];
    let mut slashes = 0;
    for ch in value.encode_wide() {
        if ch == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "NUL in process argument",
            ));
        }
        if ch == b'\\' as u16 {
            slashes += 1;
            continue;
        }
        result.extend(std::iter::repeat_n(
            b'\\' as u16,
            if ch == b'"' as u16 {
                slashes * 2 + 1
            } else {
                slashes
            },
        ));
        slashes = 0;
        result.push(ch);
    }
    result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
    result.push(b'"' as u16);
    Ok(result)
}

/// Spawn an internal executable with inherited environment/cwd and explicit stdio.
/// The Command is used only for its program/arguments; other settings are rejected.
pub(crate) fn spawn(
    command: &Command,
    stdin: &impl AsRawHandle,
    output: &impl AsRawHandle,
    flags: u32,
) -> io::Result<Child> {
    if command.get_envs().next().is_some() || command.get_current_dir().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Internal child must inherit environment and cwd",
        ));
    }
    let program = command.get_program();
    let mut line = quote(program)?;
    for arg in command.get_args() {
        line.push(b' ' as u16);
        line.extend(quote(arg)?);
    }
    line.push(0);
    let program: Vec<u16> = program.encode_wide().chain(Some(0)).collect();
    let input = inheritable(stdin)?;
    let output = inheritable(output)?;
    let handles = [input.as_raw_handle(), output.as_raw_handle()];
    // SAFETY: all pointers reference live, correctly aligned buffers. The handle list contains
    // only valid inheritable duplicates owned until CreateProcessW finishes. Its stdio handles
    // and attribute backing storage outlive that call; RAII releases all temporary resources.
    unsafe {
        let mut size = 0;
        InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut size);
        if size == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut storage = vec![0usize; size.div_ceil(size_of::<usize>())];
        check(InitializeProcThreadAttributeList(
            storage.as_mut_ptr().cast(),
            1,
            0,
            &mut size,
        ))?;
        let mut attributes = Attributes(storage);
        let list = attributes.0.as_mut_ptr().cast();
        check(UpdateProcThreadAttribute(
            list,
            0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
            handles.as_ptr().cast(),
            size_of_val(&handles),
            ptr::null_mut(),
            ptr::null(),
        ))?;
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = handles[0];
        startup.StartupInfo.hStdOutput = handles[1];
        startup.StartupInfo.hStdError = handles[1];
        startup.lpAttributeList = list;
        let mut info = PROCESS_INFORMATION::default();
        check(CreateProcessW(
            program.as_ptr(),
            line.as_mut_ptr(),
            ptr::null(),
            ptr::null(),
            1,
            flags | EXTENDED_STARTUPINFO_PRESENT,
            ptr::null(),
            ptr::null(),
            &startup.StartupInfo,
            &mut info,
        ))?;
        let _thread = OwnedHandle::from_raw_handle(info.hThread);
        Ok(Child {
            process: OwnedHandle::from_raw_handle(info.hProcess),
            pid: info.dwProcessId,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, Write};

    // A real child that stays alive until its parent releases stdin. Run only via the test below.
    #[test]
    #[ignore]
    fn isolated_child_probe() {
        let mut input = io::stdin().lock();
        let mut line = String::new();
        input.read_line(&mut line).unwrap();
        assert_eq!(line.trim_end(), "password pipe works");
        println!("ISOLATED_CHILD_READY");
        io::stdout().flush().unwrap();
        line.clear();
        input.read_line(&mut line).unwrap();
    }

    #[test]
    fn child_does_not_retain_inheritable_files() {
        use std::os::windows::fs::OpenOptionsExt;
        let path =
            std::env::temp_dir().join(format!("guac-handle-{}.config", uuid::Uuid::new_v4()));
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .share_mode(3) // FILE_SHARE_READ | FILE_SHARE_WRITE, deliberately excluding DELETE.
            .open(&path)
            .unwrap();
        // SAFETY: file owns this handle; reproduce a file inherited from the SSH Match caller.
        unsafe {
            check(SetHandleInformation(
                file.as_raw_handle(),
                HANDLE_FLAG_INHERIT,
                HANDLE_FLAG_INHERIT,
            ))
            .unwrap();
        }
        let (input, mut writer) = io::pipe().unwrap();
        let (reader, output) = io::pipe().unwrap();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command.args([
            "--ignored",
            "--exact",
            "child_process::tests::isolated_child_probe",
            "--nocapture",
        ]);
        let mut child = spawn(&command, &input, &output, CREATE_NO_WINDOW).unwrap();
        drop(input);
        drop(output);
        writeln!(writer, "password pipe works").unwrap();
        let mut reader = io::BufReader::new(reader);
        let mut line = String::new();
        loop {
            line.clear();
            assert_ne!(
                reader.read_line(&mut line).unwrap(),
                0,
                "Probe exited before readiness"
            );
            if line.trim_end() == "ISOLATED_CHILD_READY" {
                break;
            }
        }
        assert!(child.try_wait().unwrap().is_none());
        drop(file);
        let removed = std::fs::remove_file(&path);
        drop(writer);
        assert!(child.wait().unwrap().success());
        removed.expect("Background child retained the caller's config file handle");
    }

    #[test]
    fn argument_quoting_preserves_quotes_and_trailing_backslashes() {
        let text = |value: &str| String::from_utf16(&quote(OsStr::new(value)).unwrap()).unwrap();
        assert_eq!(text(""), "\"\"");
        assert_eq!(text("a b"), "\"a b\"");
        assert_eq!(text("a\"b"), "\"a\\\"b\"");
        assert_eq!(text("a\\"), "\"a\\\\\"");
        assert!(quote(OsStr::new("a\0b")).is_err());
    }
}
