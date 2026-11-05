//! Native Windows notification icon and bridge manager. Polling never runs on the UI thread.
#[cfg(windows)]
pub(crate) fn launch() -> anyhow::Result<()> {
    let null = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("NUL")?;
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command.args(["tray", "--background"]);
    crate::child_process::spawn(&command, &null, &null, 0x08000000)?;
    Ok(())
}
pub(crate) fn run(background: bool) -> anyhow::Result<()> {
    #[cfg(windows)]
    {
        windows::run(background)
    }
    #[cfg(not(windows))]
    {
        let _ = background;
        anyhow::bail!("The tray UI is available on Windows")
    }
}

#[cfg(any(windows, test))]
pub(crate) fn bytes(n: f64) -> String {
    if n < 1024.0 {
        format!("{n:.0} B")
    } else if n < 1048576.0 {
        format!("{:.1} KiB", n / 1024.0)
    } else if n < 1073741824.0 {
        format!("{:.1} MiB", n / 1048576.0)
    } else {
        format!("{:.2} GiB", n / 1073741824.0)
    }
}
#[cfg(any(windows, test))]
pub(crate) fn rates(old: &crate::usage::Snapshot, new: &crate::usage::Snapshot) -> (f64, f64) {
    let elapsed = new.uptime_ms.saturating_sub(old.uptime_ms) as f64 / 1000.0;
    if elapsed <= 0.0 {
        return (0.0, 0.0);
    }
    (
        new.uploaded.saturating_sub(old.uploaded) as f64 / elapsed,
        new.downloaded.saturating_sub(old.downloaded) as f64 / elapsed,
    )
}

#[cfg(windows)]
mod windows {
    use crate::background;
    use anyhow::{Result, ensure};
    use std::{
        fs::OpenOptions,
        process::Command,
        time::{Duration, Instant},
    };
    use windows_sys::{
        Win32::{
            Foundation::*,
            System::{Console::FreeConsole, LibraryLoader::GetModuleHandleW, Threading::*},
            UI::{Shell::*, WindowsAndMessaging::*},
        },
        core::*,
    };
    const ICON: u32 = WM_APP + 1;
    const SHOW: u32 = WM_APP + 2;
    const LICENSES: usize = 106;
    const OPEN: usize = 104;
    const EXIT: usize = 105;
    const STOP_ALL: usize = 102;
    struct Host {
        manager: Option<crate::child_process::Child>,
        taskbar: u32,
        retries: u32,
    }
    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(Some(0)).collect()
    }
    unsafe fn notify(hwnd: HWND, operation: u32) -> bool {
        unsafe {
            let mut data: NOTIFYICONDATAW = std::mem::zeroed();
            data.cbSize = std::mem::size_of_val(&data) as u32;
            data.hWnd = hwnd;
            data.uID = 1;
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
            data.uCallbackMessage = ICON;
            data.hIcon = LoadIconW(GetModuleHandleW(std::ptr::null()), 1usize as PCWSTR);
            let tip = wide("Guac Bridge — manage running bridges");
            data.szTip[..tip.len()].copy_from_slice(&tip);
            Shell_NotifyIconW(operation, &data) != 0
        }
    }
    struct Search {
        pid: u32,
        hwnd: HWND,
    }
    unsafe extern "system" fn find_manager(hwnd: HWND, data: isize) -> i32 {
        unsafe {
            let search = &mut *(data as *mut Search);
            let mut pid = 0;
            GetWindowThreadProcessId(hwnd, &mut pid);
            if pid == search.pid {
                let mut title = [0u16; 128];
                let count = GetWindowTextW(hwnd, title.as_mut_ptr(), title.len() as i32);
                if String::from_utf16_lossy(&title[..count.max(0) as usize])
                    == crate::manager::TITLE
                {
                    search.hwnd = hwnd;
                    return 0;
                }
                // Bootstrap prerequisite dialogs belong to the manager too.
                // Raise them on another click and dismiss them during shutdown.
                if search.hwnd.is_null() && IsWindowVisible(hwnd) != 0 {
                    search.hwnd = hwnd;
                }
            }
            1
        }
    }
    fn manager_window(pid: u32) -> HWND {
        let mut search = Search {
            pid,
            hwnd: std::ptr::null_mut(),
        };
        unsafe {
            EnumWindows(Some(find_manager), (&mut search) as *mut Search as isize);
        }
        search.hwnd
    }
    fn open_manager(host: &mut Host, window: HWND) -> Result<()> {
        if let Some(child) = &mut host.manager
            && child.try_wait()?.is_none()
        {
            let hwnd = manager_window(child.id());
            if !hwnd.is_null() {
                unsafe {
                    ShowWindow(hwnd, SW_RESTORE);
                    SetForegroundWindow(hwnd);
                }
            } else if host.retries > 0 {
                // Closing the last WinUI window precedes process exit. Preserve
                // an immediate reopen request without blocking the tray loop.
                host.retries -= 1;
                unsafe {
                    SetTimer(window, 1, 100, None);
                }
            }
            return Ok(());
        }
        let log = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(background::directory()?.join("manager.log"))?;
        let mut command = Command::new(std::env::current_exe()?);
        command.arg("manager");
        let null = OpenOptions::new().read(true).write(true).open("NUL")?;
        host.manager = Some(crate::child_process::spawn(
            &command, &null, &log, 0x08000000,
        )?);
        Ok(())
    }
    fn message(text: &str, error: bool) {
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                wide(text).as_ptr(),
                w!("Guac Bridge"),
                MB_OK
                    | if error {
                        MB_ICONERROR
                    } else {
                        MB_ICONINFORMATION
                    },
            );
        }
    }
    unsafe extern "system" fn window(hwnd: HWND, msg: u32, wp: usize, lp: isize) -> isize {
        unsafe {
            if msg == WM_CLOSE {
                return 0;
            } // The tray host never owns a visible manager window.
            if msg == WM_DESTROY {
                notify(hwnd, NIM_DELETE);
                PostQuitMessage(0);
                return 0;
            }
            let ptr = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut Host;
            if ptr.is_null() {
                return DefWindowProcW(hwnd, msg, wp, lp);
            }
            if msg == (*ptr).taskbar {
                notify(hwnd, NIM_ADD);
                return 0;
            }
            match msg {
                SHOW => {
                    (*ptr).retries = 30;
                    if let Err(e) = open_manager(&mut *ptr, hwnd) {
                        message(&format!("Cannot open manager: {e:#}"), true);
                    }
                }
                WM_TIMER if wp == 1 => {
                    KillTimer(hwnd, 1);
                    if let Err(e) = open_manager(&mut *ptr, hwnd) {
                        message(&format!("Cannot reopen manager: {e:#}"), true);
                    }
                }
                WM_COMMAND => match wp & 0xffff {
                    OPEN => {
                        PostMessageW(hwnd, SHOW, 0, 0);
                    }
                    LICENSES => {
                        if let Err(error) = crate::licensing::open() {
                            message(&format!("Cannot open licenses: {error:#}"), true);
                        }
                    }
                    EXIT => {
                        DestroyWindow(hwnd);
                    }
                    STOP_ALL
                        if MessageBoxW(
                            hwnd,
                            w!("Stop all bridges? All active connections will disconnect."),
                            w!("Guac Bridge"),
                            MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
                        ) == IDYES =>
                    {
                        std::thread::spawn(|| {
                            let result = background::bridges().and_then(|bridges| {
                                for bridge in bridges {
                                    bridge.stop()?;
                                }
                                Ok(())
                            });
                            if let Err(e) = result {
                                message(&format!("Could not stop all bridges: {e:#}"), true);
                            }
                        });
                    }
                    _ => {}
                },
                ICON if lp as u32 == WM_LBUTTONUP => {
                    PostMessageW(hwnd, SHOW, 0, 0);
                }
                ICON if lp as u32 == WM_RBUTTONUP => {
                    let menu = CreatePopupMenu();
                    AppendMenuW(menu, MF_STRING, OPEN, w!("Open bridge manager"));
                    AppendMenuW(menu, MF_STRING, STOP_ALL, w!("Stop all bridges…"));
                    AppendMenuW(menu, MF_STRING, LICENSES, w!("Licenses…"));
                    AppendMenuW(menu, MF_SEPARATOR, 0, std::ptr::null());
                    AppendMenuW(
                        menu,
                        MF_STRING,
                        EXIT,
                        w!("Exit tray (keep bridges running)"),
                    );
                    let mut point = std::mem::zeroed();
                    GetCursorPos(&mut point);
                    SetForegroundWindow(hwnd);
                    let command = TrackPopupMenu(
                        menu,
                        TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                        point.x,
                        point.y,
                        0,
                        hwnd,
                        std::ptr::null(),
                    );
                    DestroyMenu(menu);
                    if command != 0 {
                        PostMessageW(hwnd, WM_COMMAND, command as usize, 0);
                    }
                    PostMessageW(hwnd, WM_NULL, 0, 0);
                }
                _ => return DefWindowProcW(hwnd, msg, wp, lp),
            }
            0
        }
    }
    pub fn shutdown() -> Result<()> {
        use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(background::directory()?.join("tray.lock"))?;
        let manager_lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(background::directory()?.join("manager.lock"))?;
        let deadline = Instant::now() + Duration::from_secs(20);
        let mut process: Option<OwnedHandle> = None;
        loop {
            unsafe {
                let hwnd = FindWindowW(w!("GuacBridgeManager"), std::ptr::null());
                if !hwnd.is_null() {
                    if process.is_none() {
                        let mut pid = 0;
                        GetWindowThreadProcessId(hwnd, &mut pid);
                        let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
                        ensure!(!handle.is_null(), "Cannot wait for tray shutdown");
                        process = Some(OwnedHandle::from_raw_handle(handle));
                    }
                    PostMessageW(hwnd, WM_COMMAND, EXIT, 0);
                }
            }
            if lock.try_lock().is_ok() {
                if process.as_ref().is_none_or(
                    |p| unsafe { WaitForSingleObject(p.as_raw_handle(), 0) } == WAIT_OBJECT_0,
                ) && manager_lock.try_lock().is_ok()
                {
                    return Ok(());
                }
                lock.unlock()?;
            }
            ensure!(
                Instant::now() < deadline,
                "Tray or manager did not exit; close its dialogs and retry"
            );
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    pub fn run(background: bool) -> Result<()> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(background::directory()?.join("tray.lock"))?;
        if let Err(e) = lock.try_lock() {
            if let std::fs::TryLockError::Error(error) = e {
                return Err(error.into());
            }
            if !background {
                unsafe {
                    let hwnd = FindWindowW(w!("GuacBridgeManager"), std::ptr::null());
                    if !hwnd.is_null() {
                        PostMessageW(hwnd, SHOW, 0, 0);
                    }
                }
            }
            return Ok(());
        }
        let mut host = Box::new(Host {
            manager: None,
            taskbar: 0,
            retries: 0,
        });
        unsafe {
            FreeConsole();
            let instance = GetModuleHandleW(std::ptr::null());
            let mut class: WNDCLASSW = std::mem::zeroed();
            class.lpfnWndProc = Some(window);
            class.hInstance = instance;
            class.lpszClassName = w!("GuacBridgeManager");
            class.hIcon = LoadIconW(instance, 1usize as PCWSTR);
            ensure!(RegisterClassW(&class) != 0, "Cannot register tray host");
            let hwnd = CreateWindowExW(
                0,
                class.lpszClassName,
                w!("Guac Bridge tray host"),
                WS_OVERLAPPEDWINDOW,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                320,
                200,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                instance,
                std::ptr::null(),
            );
            ensure!(!hwnd.is_null(), "Cannot create tray host");
            host.taskbar = RegisterWindowMessageW(w!("TaskbarCreated"));
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, (&mut *host) as *mut Host as isize);
            if !notify(hwnd, NIM_ADD) {
                DestroyWindow(hwnd);
                anyhow::bail!("Cannot add tray icon");
            }
            if !background {
                PostMessageW(hwnd, SHOW, 0, 0);
            }
            let mut msg = std::mem::zeroed();
            while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
                TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
        // Releasing the tray executable for updates also requires closing its manager child.
        if let Some(mut child) = host.manager.take() {
            let deadline = Instant::now() + Duration::from_secs(15);
            while child.try_wait()?.is_none() {
                let hwnd = manager_window(child.id());
                if !hwnd.is_null() {
                    unsafe {
                        PostMessageW(hwnd, WM_CLOSE, 0, 0);
                    }
                }
                ensure!(
                    Instant::now() < deadline,
                    "Manager did not close; close its runtime or confirmation dialogs and retry"
                );
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        Ok(())
    }
}

pub(crate) fn shutdown() -> anyhow::Result<()> {
    #[cfg(windows)]
    {
        windows::shutdown()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn sampled_rates_and_counter_resets() {
        let a = crate::usage::Snapshot {
            uptime_ms: 1000,
            uploaded: 100,
            downloaded: 200,
            ..Default::default()
        };
        let b = crate::usage::Snapshot {
            uptime_ms: 3000,
            uploaded: 500,
            downloaded: 1200,
            ..Default::default()
        };
        assert_eq!(rates(&a, &b), (200.0, 500.0));
        assert_eq!(rates(&b, &a), (0.0, 0.0));
        assert_eq!(bytes(2048.0), "2.0 KiB");
    }
}
