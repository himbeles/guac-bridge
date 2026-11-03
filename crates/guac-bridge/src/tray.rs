//! Native Windows notification icon and bridge manager. Polling never runs on the UI thread.
#[cfg(windows)]
pub(crate) fn launch() -> anyhow::Result<()> {
    use std::{
        os::windows::process::CommandExt,
        process::{Command, Stdio},
    };
    Command::new(std::env::current_exe()?)
        .args(["tray", "--background"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(0x08000000)
        .spawn()?;
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
fn bytes(n: f64) -> String {
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
fn rates(old: &crate::usage::Snapshot, new: &crate::usage::Snapshot) -> (f64, f64) {
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
    use super::*;
    use crate::background::{self, Bridge};
    use anyhow::{Result, ensure};
    use fs2::FileExt;
    use std::{collections::HashMap, fs::OpenOptions, sync::mpsc, time::Duration};
    use windows_sys::{
        Win32::{
            Foundation::*,
            Graphics::Gdi::*,
            System::{Console::FreeConsole, LibraryLoader::GetModuleHandleW},
            UI::{Shell::*, WindowsAndMessaging::*},
        },
        core::*,
    };
    // Native dialogs and control notifications can dispatch nested messages.
    // Never create a second mutable Ui reference during a callback.
    thread_local! { static IN_CALLBACK: std::cell::Cell<bool> = const { std::cell::Cell::new(false) }; }
    struct CallbackGuard;
    impl Drop for CallbackGuard {
        fn drop(&mut self) {
            IN_CALLBACK.with(|value| value.set(false));
        }
    }
    const ICON: u32 = WM_APP + 1;
    const SHOW: u32 = WM_APP + 2;
    const LIST: usize = 100;
    const STOP: usize = 101;
    const STOP_ALL: usize = 102;
    const LOG: usize = 103;
    const OPEN: usize = 104;
    const EXIT: usize = 105;
    enum Action {
        Stop(Vec<Bridge>),
    }
    struct Update {
        bridges: Vec<Bridge>,
        error: Option<String>,
    }
    struct Ui {
        list: HWND,
        details: HWND,
        status: HWND,
        bridges: Vec<Bridge>,
        previous: HashMap<String, crate::usage::Snapshot>,
        update: mpsc::Receiver<Update>,
        action: mpsc::Sender<Action>,
        taskbar: u32,
    }
    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(Some(0)).collect()
    }
    unsafe fn text(hwnd: HWND, s: &str) {
        unsafe {
            SetWindowTextW(hwnd, wide(s).as_ptr());
        }
    }
    unsafe fn icon(hwnd: HWND, operation: u32) -> bool {
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
    unsafe fn selected(ui: &Ui) -> Option<&Bridge> {
        let index = unsafe { SendMessageW(ui.list, LB_GETCURSEL, 0, 0) };
        if index < 0 {
            None
        } else {
            ui.bridges.get(index as usize)
        }
    }
    unsafe fn details(ui: &Ui) {
        unsafe {
            let Some(b) = selected(ui) else {
                text(
                    ui.details,
                    "No bridges registered. Start an SSH or LLM bridge to see it here.",
                );
                return;
            };
            let mut value = format!("{}\r\n{}\r\n\r\n", b.label, b.details);
            if let Some(s) = &b.usage {
                let (up, down) = ui
                    .previous
                    .get(&b.instance)
                    .map(|old| rates(old, s))
                    .unwrap_or_default();
                value.push_str(&format!("{}\r\nUptime: {} min {} sec\r\nConnections: {} active / {} since start\r\n\r\nUpload: {}/s\r\nDownload: {}/s\r\n\r\nTotal uploaded: {}\r\nTotal downloaded: {}\r\nLast payload activity: {}\r\n\r\nPayload bytes since this bridge started. Upload counts bytes read from local clients; download counts bytes written to local clients. Protocol overhead and retransmissions are excluded.",
                    if s.idle_ms.is_some_and(|ms| ms < 3000) { "Transferring" } else if s.connections > 0 { "Connected, idle" } else { "Listening, idle" },
                    s.uptime_ms / 60000, s.uptime_ms / 1000 % 60, s.connections, s.total_connections,
                    bytes(up),bytes(down),bytes(s.uploaded as f64),bytes(s.downloaded as f64),
                    s.idle_ms.map(|ms| format!("{} sec ago",ms/1000)).unwrap_or_else(|| "No data transferred yet".into())));
            } else {
                value.push_str(if b.online { "Running. Usage is unavailable with this bridge version." } else { "Unresponsive registration. The process may have exited or may be busy. No process has been terminated and this entry has been retained." });
            }
            text(ui.details, &value);
        }
    }
    unsafe fn refresh(ui: &mut Ui, update: Update) {
        unsafe {
            let key = selected(ui).map(|b| b.key.clone());
            ui.previous = ui
                .bridges
                .iter()
                .filter_map(|b| b.usage.clone().map(|s| (b.instance.clone(), s)))
                .collect();
            ui.bridges = update.bridges;
            SendMessageW(ui.list, WM_SETREDRAW, 0, 0);
            SendMessageW(ui.list, LB_RESETCONTENT, 0, 0);
            for b in &ui.bridges {
                let activity = b
                    .usage
                    .as_ref()
                    .map(|s| {
                        if s.idle_ms.is_some_and(|ms| ms < 3000) {
                            "Transferring"
                        } else {
                            "Idle"
                        }
                    })
                    .unwrap_or(if b.online { "Running" } else { "Unresponsive" });
                SendMessageW(
                    ui.list,
                    LB_ADDSTRING,
                    0,
                    wide(&format!("{}   · {activity}", b.label)).as_ptr() as isize,
                );
            }
            let index = key
                .and_then(|key| ui.bridges.iter().position(|b| b.key == key))
                .unwrap_or(0);
            SendMessageW(ui.list, LB_SETCURSEL, index, 0);
            SendMessageW(ui.list, WM_SETREDRAW, 1, 0);
            InvalidateRect(ui.list, std::ptr::null(), 1);
            text(ui.status, &update.error.unwrap_or_else(||format!("{} bridge(s) · updates every 2 seconds · closing this window keeps bridges running", ui.bridges.len())));
            details(ui);
        }
    }
    unsafe fn confirm(hwnd: HWND, message: &str) -> bool {
        unsafe {
            MessageBoxW(
                hwnd,
                wide(message).as_ptr(),
                w!("Guac Bridge"),
                MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
            ) == IDYES
        }
    }
    unsafe extern "system" fn window(hwnd: HWND, msg: u32, wp: usize, lp: isize) -> isize {
        unsafe {
            // DefWindowProc handles SC_CLOSE by sending WM_CLOSE synchronously.
            // Handle it before the reentrancy guard so X and Alt+F4 hide the
            // window rather than falling through to default destruction.
            if msg == WM_CLOSE {
                ShowWindow(hwnd, SW_HIDE);
                return 0;
            }
            if msg == WM_DESTROY {
                KillTimer(hwnd, 1);
                icon(hwnd, NIM_DELETE);
                PostQuitMessage(0);
                return 0;
            }
            // Native move/resize loops dispatch WM_SIZE and other messages
            // synchronously. Do not hold a Ui borrow or callback guard across them.
            if matches!(msg, WM_SYSCOMMAND | WM_NCLBUTTONDOWN | WM_NCLBUTTONDBLCLK) {
                return DefWindowProcW(hwnd, msg, wp, lp);
            }
            if IN_CALLBACK.with(|value| value.replace(true)) {
                return DefWindowProcW(hwnd, msg, wp, lp);
            }
            let _guard = CallbackGuard;
            let ptr = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *mut Ui;
            if ptr.is_null() {
                return DefWindowProcW(hwnd, msg, wp, lp);
            }
            let ui = &mut *ptr;
            if msg == ui.taskbar {
                icon(hwnd, NIM_ADD);
                return 0;
            }
            match msg {
                SHOW => {
                    ShowWindow(hwnd, SW_RESTORE);
                    SetForegroundWindow(hwnd);
                }
                WM_GETMINMAXINFO => {
                    let limits = &mut *(lp as *mut MINMAXINFO);
                    limits.ptMinTrackSize.x = 780;
                    limits.ptMinTrackSize.y = 420;
                }
                WM_TIMER => {
                    while let Ok(update) = ui.update.try_recv() {
                        refresh(ui, update);
                    }
                }
                WM_SIZE => {
                    let width = (lp as u32 & 0xffff) as i32;
                    let height = ((lp as u32 >> 16) & 0xffff) as i32;
                    MoveWindow(
                        ui.list,
                        12,
                        12,
                        (width * 2 / 5 - 18).max(100),
                        (height - 90).max(50),
                        1,
                    );
                    MoveWindow(
                        ui.details,
                        width * 2 / 5,
                        12,
                        (width * 3 / 5 - 12).max(100),
                        (height - 90).max(50),
                        1,
                    );
                    MoveWindow(
                        ui.status,
                        12,
                        (height - 28).max(0),
                        (width - 24).max(100),
                        22,
                        1,
                    );
                    for (id, x, w) in [(STOP, 12, 130), (STOP_ALL, 150, 130), (LOG, 288, 110)] {
                        MoveWindow(
                            GetDlgItem(hwnd, id as i32),
                            x,
                            (height - 68).max(0),
                            w,
                            30,
                            1,
                        );
                    }
                }
                ICON => {
                    if lp as u32 == WM_LBUTTONUP {
                        PostMessageW(hwnd, SHOW, 0, 0);
                    }
                    if lp as u32 == WM_RBUTTONUP {
                        let menu = CreatePopupMenu();
                        AppendMenuW(menu, MF_STRING, OPEN, w!("Open bridge manager"));
                        AppendMenuW(menu, MF_STRING, STOP_ALL, w!("Stop all bridges…"));
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
                        let selected = TrackPopupMenu(
                            menu,
                            TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                            point.x,
                            point.y,
                            0,
                            hwnd,
                            std::ptr::null(),
                        );
                        DestroyMenu(menu);
                        if selected != 0 {
                            PostMessageW(hwnd, WM_COMMAND, selected as usize, 0);
                        }
                        PostMessageW(hwnd, WM_NULL, 0, 0);
                    }
                }
                WM_COMMAND => match wp & 0xffff {
                    LIST => details(ui),
                    OPEN => {
                        PostMessageW(hwnd, SHOW, 0, 0);
                    }
                    EXIT => {
                        DestroyWindow(hwnd);
                    }
                    STOP | STOP_ALL => {
                        let bridges: Vec<_> = if wp & 0xffff == STOP_ALL {
                            ui.bridges.clone()
                        } else {
                            selected(ui).cloned().into_iter().collect()
                        };
                        if !bridges.is_empty()
                            && confirm(
                                hwnd,
                                &format!(
                                    "Stop {} bridge(s)? Active SSH sessions and LLM requests on these bridges will be disconnected.",
                                    bridges.len()
                                ),
                            )
                        {
                            let _ = ui.action.send(Action::Stop(bridges));
                            text(ui.status, "Sending authenticated stop request…");
                        }
                    }
                    LOG => {
                        if let Some(path) = selected(ui).and_then(|b| b.log_path())
                            && let Err(e) =
                                std::process::Command::new("notepad.exe").arg(path).spawn()
                        {
                            text(ui.status, &format!("Cannot open log: {e}"));
                        }
                    }
                    _ => {}
                },
                _ => return DefWindowProcW(hwnd, msg, wp, lp),
            }
            0
        }
    }
    pub fn shutdown() -> Result<()> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(background::directory()?.join("tray.lock"))?;
        use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
        use windows_sys::Win32::System::Threading::*;
        let mut process: Option<OwnedHandle> = None;
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        loop {
            unsafe {
                let hwnd = FindWindowW(w!("GuacBridgeManager"), std::ptr::null());
                if !hwnd.is_null() {
                    if process.is_none() {
                        let mut pid = 0;
                        GetWindowThreadProcessId(hwnd, &mut pid);
                        let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
                        ensure!(
                            !handle.is_null(),
                            "Cannot wait for the tray to exit: {}",
                            std::io::Error::last_os_error()
                        );
                        process = Some(OwnedHandle::from_raw_handle(handle));
                    }
                    PostMessageW(hwnd, WM_COMMAND, EXIT, 0);
                }
            }
            if lock.try_lock_exclusive().is_ok() {
                if process.as_ref().is_none_or(
                    |p| unsafe { WaitForSingleObject(p.as_raw_handle(), 0) } == WAIT_OBJECT_0,
                ) {
                    return Ok(());
                }
                FileExt::unlock(&lock)?;
            }
            ensure!(
                std::time::Instant::now() < deadline,
                "The tray did not exit; close its dialogs and retry the update"
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
        if let Err(e) = lock.try_lock_exclusive() {
            if e.raw_os_error() != fs2::lock_contended_error().raw_os_error() {
                return Err(e.into());
            }
            if !background {
                unsafe {
                    let existing = FindWindowW(w!("GuacBridgeManager"), std::ptr::null());
                    if !existing.is_null() {
                        PostMessageW(existing, SHOW, 0, 0);
                    }
                }
            }
            return Ok(());
        }
        let (send, update) = mpsc::channel();
        let (action, receive) = mpsc::channel();
        std::thread::spawn(move || {
            let mut error = None;
            loop {
                let current = background::bridges();
                let update = match current {
                    Ok(bridges) => Update {
                        bridges,
                        error: error.clone(),
                    },
                    Err(e) => Update {
                        bridges: Vec::new(),
                        error: Some(format!("Cannot read bridge registrations: {e:#}")),
                    },
                };
                if send.send(update).is_err() {
                    break;
                }
                match receive.recv_timeout(Duration::from_secs(2)) {
                    Ok(Action::Stop(bridges)) => {
                        error = None;
                        for bridge in bridges {
                            if let Err(e) = bridge.stop() {
                                error = Some(format!("Could not stop {}: {e:#}", bridge.label));
                            }
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
            }
        });
        unsafe {
            FreeConsole();
            windows_sys::Win32::UI::Controls::InitCommonControls();
            let instance = GetModuleHandleW(std::ptr::null());
            let mut class: WNDCLASSW = std::mem::zeroed();
            class.lpfnWndProc = Some(window);
            class.hInstance = instance;
            class.lpszClassName = w!("GuacBridgeManager");
            class.hIcon = LoadIconW(instance, 1usize as PCWSTR);
            class.hCursor = LoadCursorW(std::ptr::null_mut(), IDC_ARROW);
            class.hbrBackground = (COLOR_WINDOW + 1) as _;
            ensure!(RegisterClassW(&class) != 0, "Cannot register tray window");
            let hwnd = CreateWindowExW(
                0,
                class.lpszClassName,
                w!("Guac Bridge — running bridges"),
                WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                1040,
                560,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                instance,
                std::ptr::null(),
            );
            ensure!(!hwnd.is_null(), "Cannot create tray window");
            let font = CreateFontW(
                -16,
                0,
                0,
                0,
                FW_NORMAL as i32,
                0,
                0,
                0,
                DEFAULT_CHARSET as u32,
                OUT_DEFAULT_PRECIS as u32,
                CLIP_DEFAULT_PRECIS as u32,
                CLEARTYPE_QUALITY as u32,
                DEFAULT_PITCH as u32,
                w!("Segoe UI"),
            );
            let child = |class: PCWSTR, title: PCWSTR, style: u32, id: usize| {
                let handle = CreateWindowExW(
                    0,
                    class,
                    title,
                    WS_CHILD | WS_VISIBLE | style,
                    0,
                    0,
                    100,
                    30,
                    hwnd,
                    id as HMENU,
                    instance,
                    std::ptr::null(),
                );
                windows_sys::Win32::UI::Controls::SetWindowTheme(
                    handle,
                    w!("Explorer"),
                    std::ptr::null(),
                );
                SendMessageW(
                    handle,
                    WM_SETFONT,
                    if font.is_null() {
                        GetStockObject(DEFAULT_GUI_FONT) as usize
                    } else {
                        font as usize
                    },
                    1,
                );
                handle
            };
            let list = child(
                w!("LISTBOX"),
                w!(""),
                WS_TABSTOP | WS_VSCROLL | LBS_NOTIFY as u32 | LBS_NOINTEGRALHEIGHT as u32,
                LIST,
            );
            let details = child(
                w!("EDIT"),
                w!("Loading bridges…"),
                WS_TABSTOP
                    | WS_VSCROLL
                    | ES_MULTILINE as u32
                    | ES_READONLY as u32
                    | ES_AUTOVSCROLL as u32,
                110,
            );
            let status = child(w!("STATIC"), w!("Loading bridges…"), 0, 111);
            child(w!("BUTTON"), w!("Stop selected…"), WS_TABSTOP, STOP);
            child(w!("BUTTON"), w!("Stop all…"), WS_TABSTOP, STOP_ALL);
            child(w!("BUTTON"), w!("Open log"), WS_TABSTOP, LOG);
            let mut ui = Box::new(Ui {
                list,
                details,
                status,
                bridges: Vec::new(),
                previous: HashMap::new(),
                update,
                action,
                taskbar: RegisterWindowMessageW(w!("TaskbarCreated")),
            });
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, (&mut *ui) as *mut Ui as isize);
            if !icon(hwnd, NIM_ADD) {
                DestroyWindow(hwnd);
                anyhow::bail!("Cannot add tray icon");
            }
            SetTimer(hwnd, 1, 250, None);
            // Creation's WM_SIZE ran before UI state existed; apply the initial layout now.
            let mut rect = std::mem::zeroed();
            GetClientRect(hwnd, &mut rect);
            SendMessageW(
                hwnd,
                WM_SIZE,
                0,
                ((rect.bottom as u32) << 16 | rect.right as u32) as isize,
            );
            if !background {
                ShowWindow(hwnd, SW_SHOW);
            }
            let mut message = std::mem::zeroed();
            loop {
                let result = GetMessageW(&mut message, std::ptr::null_mut(), 0, 0);
                if result <= 0 {
                    break;
                }
                if IsDialogMessageW(hwnd, &message) == 0 {
                    TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            }
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            if !font.is_null() {
                DeleteObject(font);
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
