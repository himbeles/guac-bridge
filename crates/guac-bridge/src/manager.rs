//! Declarative WinUI 3 manager. Uses the installed Windows App Runtime; never stages a runtime.
use crate::{
    background::{self, Bridge},
    usage::Snapshot,
};
use anyhow::{Context, Result};
use std::{collections::HashMap, fs::OpenOptions, time::Duration};
use windows_reactor::*;

pub(crate) const TITLE: &str = "Guac Bridge — running bridges";

#[derive(Clone)]
enum Message {
    Poll(Result<Vec<Bridge>, String>),
    Select(String),
    AskStop(bool),
    Cancel,
    Confirm,
    Stopped(Result<(), String>),
    Log,
}
struct Manager {
    bridges: Vec<Bridge>,
    previous: HashMap<String, Snapshot>,
    selected: Option<String>,
    pending: Option<Vec<Bridge>>,
    busy: bool,
    loading: bool,
    error: Option<String>,
}
impl Manager {
    fn poll(context: &ComponentContext<Self>, delay: bool) {
        let _ = context.spawn_background(move |cancel| {
            if delay {
                std::thread::sleep(Duration::from_secs(2));
            }
            if cancel.is_cancelled() {
                return Message::Poll(Ok(Vec::new()));
            }
            Message::Poll(background::bridges().map_err(|e| format!("Cannot read bridges: {e:#}")))
        });
    }
    fn selected(&self) -> Option<&Bridge> {
        self.bridges
            .iter()
            .find(|b| Some(&b.instance) == self.selected.as_ref())
    }
    fn detail(&self, context: &ViewContext<Self>) -> View {
        let Some(bridge) = self.selected() else {
            return StackPanel::new().spacing(12.0).children((
                heading("No bridge selected", 22.0),
                label("Start an SSH or HTTP bridge. It appears here when ready."),
            ));
        };
        let mut sections = vec![
            ("title", heading(endpoint(bridge), 22.0)),
            (
                "gateway",
                label(format!("Gateway Type: {}", bridge.gateway())),
            ),
            ("user", label(format!("Gateway user: {}", bridge.user()))),
            ("route", label(bridge.route())),
            ("status", label(activity(bridge))),
        ];
        if let Some(s) = &bridge.usage {
            let (up, down) = self
                .previous
                .get(&bridge.instance)
                .map(|old| crate::tray::rates(old, s))
                .unwrap_or_default();
            sections.push((
                "rates",
                Grid::new()
                    .columns([GridLength::STAR, GridLength::STAR])
                    .rows([GridLength::Auto, GridLength::Auto])
                    .row_spacing(12.0)
                    .column_spacing(12.0)
                    .children((
                        metric("Upload", format!("{}/s", crate::tray::bytes(up)), 0, 0),
                        metric("Download", format!("{}/s", crate::tray::bytes(down)), 0, 1),
                        metric(
                            "Total uploaded",
                            crate::tray::bytes(s.uploaded as f64),
                            1,
                            0,
                        ),
                        metric(
                            "Total downloaded",
                            crate::tray::bytes(s.downloaded as f64),
                            1,
                            1,
                        ),
                    )),
            ));
            sections.push((
                "connections",
                label(format!(
                    "{} active connection{} · {} since start",
                    s.connections,
                    if s.connections == 1 { "" } else { "s" },
                    s.total_connections
                )),
            ));
            sections.push((
                "uptime",
                label(format!(
                    "Uptime: {} min {} sec",
                    s.uptime_ms / 60000,
                    s.uptime_ms / 1000 % 60
                )),
            ));
            sections.push((
                "activity",
                label(
                    s.idle_ms
                        .map(|ms| format!("Last payload activity: {} sec ago", ms / 1000))
                        .unwrap_or_else(|| "No data transferred yet".into()),
                ),
            ));
        } else {
            sections.push((
                "unresponsive",
                InfoBar::new()
                    .is_open(true)
                    .severity(InfoBarSeverity::Warning)
                    .title("Bridge not responding")
                    .message("Its registration is retained. No process has been terminated.")
                    .into(),
            ));
        }
        sections.push((
            "buttons",
            StackPanel::new()
                .orientation(Orientation::Horizontal)
                .spacing(8.0)
                .children((
                    Button::new()
                        .is_enabled(!self.busy)
                        .on_click(context.message(Message::AskStop(false)))
                        .content("Stop selected…"),
                    Button::new()
                        .is_enabled(bridge.log_path().is_some())
                        .on_click(context.message(Message::Log))
                        .content("Open log"),
                )),
        ));
        StackPanel::new().spacing(16.0).keyed_children(sections)
    }
}
impl Component for Manager {
    type Input = &'static str;
    type Message = Message;
    fn create(_: &Self::Input, context: &ComponentContext<Self>) -> Self {
        Self::poll(context, false);
        Self {
            bridges: Vec::new(),
            previous: HashMap::new(),
            selected: None,
            pending: None,
            busy: false,
            loading: true,
            error: None,
        }
    }
    fn update(&mut self, message: Message, context: &ComponentContext<Self>) {
        match message {
            Message::Poll(result) => {
                self.loading = false;
                match result {
                    Ok(bridges) => {
                        self.previous = self
                            .bridges
                            .iter()
                            .filter_map(|b| b.usage.clone().map(|s| (b.instance.clone(), s)))
                            .collect();
                        self.bridges = bridges;
                        if self.selected().is_none() {
                            self.selected = self.bridges.first().map(|b| b.instance.clone());
                        }
                    }
                    Err(error) => self.error = Some(error),
                }
                Self::poll(context, true);
            }
            Message::Select(key) => self.selected = Some(key),
            Message::AskStop(all) => {
                self.pending = Some(if all {
                    self.bridges.clone()
                } else {
                    self.selected().cloned().into_iter().collect()
                });
            }
            Message::Cancel => self.pending = None,
            Message::Confirm => {
                if let Some(bridges) = self.pending.take() {
                    self.busy = true;
                    let _ = context.spawn_background(move |_| {
                        let mut errors = Vec::new();
                        for bridge in bridges {
                            if let Err(e) = bridge.stop() {
                                errors.push(format!("{}: {e:#}", bridge.label));
                            }
                        }
                        Message::Stopped(if errors.is_empty() {
                            Ok(())
                        } else {
                            Err(errors.join("\n"))
                        })
                    });
                }
            }
            Message::Stopped(result) => {
                self.busy = false;
                self.error = result.err();
            }
            Message::Log => {
                if let Some(path) = self.selected().and_then(|b| b.log_path())
                    && let Err(e) = std::process::Command::new("notepad.exe").arg(path).spawn()
                {
                    self.error = Some(format!("Cannot open log: {e}"));
                }
            }
        }
    }
    fn view(&self, icon: &Self::Input, context: &mut ViewContext<Self>) -> View {
        context.window_title(TITLE);
        context.window_visuals(
            WindowVisuals::new()
                .client_size(1040.0, 680.0)
                .constraints(WindowConstraints {
                    min_width: Some(780.0),
                    min_height: Some(460.0),
                    ..Default::default()
                })
                .backdrop(WindowBackdrop::Mica)
                .icon(icon),
        );
        let rows = self.bridges.iter().map(|bridge| {
            let selected = self.selected.as_ref() == Some(&bridge.instance);
            (
                bridge.instance.clone(),
                Border::new()
                    .corner_radius(8.0)
                    .border_thickness(Thickness::new(3.0, 0.0, 0.0, 0.0))
                    .border_brush(if selected {
                        Brush::Solid(Color::rgb(76, 132, 73))
                    } else {
                        Brush::Solid(Color::transparent())
                    })
                    .background(if selected {
                        Brush::Solid(Color::argb(28, 96, 160, 80))
                    } else {
                        Brush::Solid(Color::transparent())
                    })
                    .content(
                        Button::new()
                            .horizontal_alignment(HorizontalAlignment::Stretch)
                            .horizontal_content_alignment(HorizontalAlignment::Left)
                            .style(ButtonStyle::Subtle)
                            .on_click(context.message(Message::Select(bridge.instance.clone())))
                            .content(
                                StackPanel::new().spacing(6.0).children((
                                    StackPanel::new()
                                        .orientation(Orientation::Horizontal)
                                        .spacing(8.0)
                                        .children((
                                            Border::new()
                                                .corner_radius(4.0)
                                                .padding(Thickness::new(8.0, 2.0, 8.0, 2.0))
                                                .horizontal_alignment(HorizontalAlignment::Left)
                                                .background(if selected {
                                                    ThemeBrush::CardBackground
                                                } else {
                                                    ThemeBrush::CardStroke
                                                })
                                                .content(
                                                    TextBlock::new()
                                                        .text(bridge.bridge_type())
                                                        .font_size(12.0)
                                                        .foreground(ThemeBrush::PrimaryText),
                                                ),
                                            Border::new()
                                                .corner_radius(8.0)
                                                .padding(Thickness::new(8.0, 3.0, 8.0, 3.0))
                                                .horizontal_alignment(HorizontalAlignment::Left)
                                                .background(Brush::Solid(Color::argb(
                                                    36, 76, 132, 73,
                                                )))
                                                .content(
                                                    TextBlock::new()
                                                        .text(bridge.gateway())
                                                        .font_size(12.0)
                                                        .font_weight(FontWeight::SEMI_BOLD),
                                                ),
                                        )),
                                    TextBlock::new()
                                        .text(endpoint(bridge))
                                        .text_wrapping(TextWrapping::Wrap),
                                    TextBlock::new().text(bridge.local_url()).font_size(12.0),
                                    TextBlock::new()
                                        .text(format!("User: {}", bridge.user()))
                                        .font_size(12.0)
                                        .text_wrapping(TextWrapping::Wrap),
                                    TextBlock::new().text(activity(bridge)).font_size(12.0),
                                )),
                            ),
                    ),
            )
        });
        let confirm = self.pending.as_ref().filter(|p| !p.is_empty()).map(|p| Border::new().padding(16.0).corner_radius(8.0).background(ThemeBrush::SystemCriticalBackground).content(
            StackPanel::new().spacing(10.0).children((
                heading(format!("Stop {} bridge(s)?", p.len()), 18.0),
                label("Active SSH sessions and HTTP requests on these bridges will disconnect."),
                StackPanel::new().orientation(Orientation::Horizontal).spacing(8.0).children((
                    Button::new().style(ButtonStyle::Accent).on_click(context.message(Message::Confirm)).content("Disconnect and stop"),
                    Button::new().on_click(context.message(Message::Cancel)).content("Cancel"),
                )),
            ))
        )).unwrap_or_else(View::empty);
        Grid::new().margin(24.0).rows([GridLength::Auto, GridLength::STAR, GridLength::Auto]).row_spacing(20.0).children((
            Grid::new().grid_row(0).columns([GridLength::STAR, GridLength::Auto]).children((
                StackPanel::new().grid_column(0).spacing(6.0).children((heading("🐸 Running bridges",28.0), label("Connections, activity and data transferred since startup"))),
                Button::new().grid_column(1).vertical_alignment(VerticalAlignment::Center).is_enabled(!self.busy && !self.bridges.is_empty()).on_click(context.message(Message::AskStop(true))).content("Stop all…"),
            )),
            Grid::new().grid_row(1).columns([GridLength::Pixel(290.0), GridLength::STAR]).column_spacing(24.0).children((
                ScrollViewer::new().grid_column(0).horizontal_scroll_bar_visibility(ScrollBarVisibility::Disabled).content(StackPanel::new().spacing(8.0).keyed_children(rows)),
                ScrollViewer::new().grid_column(1).horizontal_scroll_bar_visibility(ScrollBarVisibility::Disabled).content(self.detail(context)),
            )),
            StackPanel::new().grid_row(2).spacing(12.0).children((
                confirm,
                self.error.as_ref().map(|error| InfoBar::new().is_open(true).severity(InfoBarSeverity::Error).title("Action failed").message(error.clone()).into()).unwrap_or_else(View::empty),
                TextBlock::new().text(if self.loading {"Loading bridges…".into()} else {format!("{} bridge(s) · updates every 2 seconds · closing this window keeps the tray and bridges running",self.bridges.len())}).font_size(12.0).text_wrapping(TextWrapping::Wrap),
            )),
        ))
    }
}
fn heading(text: impl Into<String>, size: f64) -> View {
    TextBlock::new()
        .text(text.into())
        .font_size(size)
        .font_weight(FontWeight::SEMI_BOLD)
        .text_wrapping(TextWrapping::Wrap)
        .into()
}
fn label(text: impl Into<String>) -> View {
    TextBlock::new()
        .text(text.into())
        .text_wrapping(TextWrapping::Wrap)
        .into()
}
fn endpoint(bridge: &Bridge) -> String {
    bridge.destination()
}
fn activity(bridge: &Bridge) -> &'static str {
    if let Some(s) = &bridge.usage {
        if s.idle_ms.is_some_and(|ms| ms < 3000) {
            "Transferring"
        } else if s.connections > 0 {
            "Connected, idle"
        } else {
            "Listening, idle"
        }
    } else {
        "Unresponsive"
    }
}
fn metric(title: &str, value: String, row: i32, column: i32) -> View {
    Border::new()
        .grid_row(row)
        .grid_column(column)
        .padding(16.0)
        .corner_radius(8.0)
        .background(ThemeBrush::CardBackground)
        .border_brush(Color::argb(55, 96, 150, 80))
        .border_thickness(1.0)
        .content(
            StackPanel::new()
                .spacing(8.0)
                .children((label(title), heading(value, 24.0))),
        )
}
pub(crate) fn run() -> Result<()> {
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(background::directory()?.join("manager.lock"))?;
    lock.try_lock()
        .context("The bridge manager is already open")?;
    // Probe before calling Reactor's delay-loaded bootstrap. Never crash the CLI
    // loader or silently attempt to fetch an OS compatibility/runtime component.
    unsafe {
        use windows_sys::{
            Win32::{Foundation::FreeLibrary, System::LibraryLoader::*},
            core::*,
        };
        let module = LoadLibraryW(w!("api-ms-win-appmodel-runtime-l1-1-5.dll"));
        let supported = !module.is_null()
            && GetProcAddress(module, c"TryCreatePackageDependency".as_ptr().cast()).is_some()
            && GetProcAddress(module, c"AddPackageDependency".as_ptr().cast()).is_some();
        if !module.is_null() {
            FreeLibrary(module);
        }
        if !supported {
            let message = w!(
                "The WinUI manager requires Windows 11 or Windows Server 2025 (or newer). Command-line bridges and the tray remain available."
            );
            windows_sys::Win32::UI::WindowsAndMessaging::MessageBoxW(
                std::ptr::null_mut(),
                message,
                w!("Guac Bridge manager"),
                0x10,
            );
            anyhow::bail!("The WinUI manager requires Windows 11 / Windows Server 2025 or newer");
        }
    }

    let icon = background::directory()?.join("frog.ico");
    std::fs::write(&icon, include_bytes!("../assets/frog.ico"))?;
    let icon: &'static str = Box::leak(icon.to_string_lossy().into_owned().into_boxed_str());
    #[cfg(feature = "bundled-ui")]
    let help = "Cannot open the bundled WinUI manager. Repair or reinstall Guac Bridge to restore its UI runtime files.";
    #[cfg(not(feature = "bundled-ui"))]
    let help = "Cannot open WinUI manager. Install Microsoft Windows App Runtime 2.4 or newer (x64) from https://learn.microsoft.com/windows/apps/windows-app-sdk/downloads";
    App::run_component::<Manager>(icon).context(help)?;
    Ok(())
}
