use crate::{FRAME, WINDOW};
use anyhow::{Result, ensure};
use clap::{Args as ClapArgs, Parser, Subcommand};
use std::{ffi::OsString, time::Duration};

#[derive(Parser)]
#[command(
    name = "guac-bridge",
    version,
    propagate_version = true,
    about = "🐸 Guac Bridge · SSH and OpenAI-compatible endpoints through Guacamole"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    /// Stop bridges and tray, then upgrade this uv tool installation.
    Update,
    /// Stop all registered bridges and the tray before an external update.
    PrepareUpdate,
    #[command(hide = true)]
    InternalUpdate { parent: u32, uv: std::path::PathBuf },
    /// Show the Windows tray and running bridge manager.
    Tray {
        #[arg(long, hide = true)]
        background: bool,
    },
    /// Forward the remote SSH service.
    Ssh(Args),
    /// Forward a remote OpenAI-compatible HTTP endpoint, including streaming.
    Llm(Args),
}

#[derive(ClapArgs, Clone, Debug)]
pub(crate) struct Args {
    #[arg(long, required_unless_present_any=["stop","status"])]
    pub url: Option<String>,
    #[arg(long, alias = "user")]
    pub username: Option<String>,
    #[arg(long)]
    pub connection_id: Option<String>,
    #[arg(long)]
    pub data_source: Option<String>,
    /// Local loopback port (SSH: 2222, LLM: 8000).
    #[arg(long)]
    pub port: Option<u16>,
    /// Remote loopback service port (SSH: 22, LLM: 8000).
    #[arg(long)]
    pub target_port: Option<u16>,
    #[arg(long, default_value = "127.0.0.1")]
    pub target_host: String,
    #[arg(long, default_value_t = 30)]
    pub connect_timeout: u64,
    #[arg(long, default_value_t = 1.0)]
    pub startup_delay: f64,
    #[arg(long, default_value_t = 8)]
    pub max_connections: usize,
    /// TCP frames in flight per connection; independent of --input-window.
    #[arg(long, default_value_t=WINDOW)]
    pub window: usize,
    /// Maximum bytes per frame. Larger frames reduce overhead; small writes still send immediately.
    #[arg(long, default_value_t=FRAME as u32, value_parser=clap::value_parser!(u32).range(4096..=65536))]
    pub frame_size: u32,
    #[arg(long)]
    pub ca_file: Option<String>,
    /// Disable certificate verification. Prefer --ca-file for a private CA.
    #[arg(long)]
    pub insecure: bool,
    /// Save the password to the native system credential store after successful login.
    #[arg(long, conflicts_with_all=["forget_password","no_keyring"])]
    pub save_password: bool,
    #[arg(long, conflicts_with = "no_keyring")]
    pub forget_password: bool,
    /// Skip the native credential store for this connection.
    #[arg(long)]
    pub no_keyring: bool,
    /// Start or reuse a background bridge. It stays running after SSH exits; use --stop to end it.
    #[arg(long, conflicts_with_all=["stop","status"])]
    pub ensure_running: bool,
    /// Stop the managed bridge on --port, including its active connections.
    #[arg(long, conflicts_with = "status")]
    pub stop: bool,
    /// Show the managed bridge status on --port.
    #[arg(long)]
    pub status: bool,
    /// Show a separate console for the managed process on Windows.
    #[arg(long)]
    pub show_console: bool,
    /// Use a manually installed helper instead of deploying the bundled agent.
    #[arg(long)]
    pub server_command: Option<String>,
    /// Local metadata-only transport log (rotates at 2 MiB, plus one backup).
    #[arg(long)]
    pub debug_log: Option<String>,
    /// Remote helper metadata log; use an absolute path in a private directory.
    #[arg(long)]
    pub server_debug_log: Option<String>,
    /// Remote server OS. Windows requires an SSH terminal and PowerShell 5.1+.
    #[arg(long, default_value = "linux", value_parser = ["linux", "windows"])]
    pub server_os: String,
    /// Linux helper-upload bytes between remote acknowledgements; live forwarding is unaffected.
    #[arg(long,default_value_t=crate::deploy::UPLOAD_CHUNK as u32,value_parser=clap::value_parser!(u32).range(3072..=49152))]
    pub upload_batch_size: u32,
    #[arg(long, hide = true)]
    pub benchmark_control: bool,
    #[arg(long, hide = true)]
    pub managed_child: bool,
    #[arg(skip)]
    pub mode: String,
    /// Guacamole input blobs awaiting gateway ACKs (1-128); independent of TCP credits.
    #[arg(long, default_value_t = 8)]
    pub input_window: usize,
    #[arg(skip)]
    pub input_chunk: usize,
    #[arg(skip)]
    pub resolved_password: Option<String>,
}
impl Args {
    pub fn port(&self) -> u16 {
        self.port
            .unwrap_or(if self.mode == "ssh" { 2222 } else { 8000 })
    }
    pub fn target_port(&self) -> u16 {
        self.target_port
            .unwrap_or(if self.mode == "ssh" { 22 } else { 8000 })
    }
    fn validate(&mut self) -> Result<()> {
        ensure!(
            self.upload_batch_size.is_multiple_of(4),
            "--upload-batch-size must be a multiple of 4"
        );
        ensure!(
            (1..=64).contains(&self.window),
            "--window must be between 1 and 64"
        );
        ensure!(
            [4096, 8192, 16384, 32768, 65536].contains(&self.frame_size),
            "--frame-size must be 4096, 8192, 16384, 32768 or 65536"
        );
        ensure!(
            (1..=16).contains(&self.max_connections) && self.connect_timeout > 0,
            "Invalid connection limit or timeout"
        );
        ensure!(
            self.startup_delay.is_finite() && self.startup_delay >= 0.0,
            "Invalid startup delay"
        );
        ensure!(
            ["127.0.0.1", "localhost", "::1"].contains(&self.target_host.as_str()),
            "Remote target must be loopback"
        );
        ensure!(self.target_port() > 0, "Target port must be positive");
        ensure!(
            !(self.ensure_running || self.stop || self.status) || self.port() > 0,
            "Managed bridges require a fixed --port greater than zero"
        );
        ensure!(
            (1..=128).contains(&self.input_window),
            "--input-window must be between 1 and 128"
        );
        self.input_chunk = 6048;
        Ok(())
    }
}
pub fn alias_entry(mode: &str) -> i32 {
    if std::env::args_os().nth(1).is_some_and(|a| a == "tray") {
        return main_entry(std::env::args_os());
    }
    let mut arguments = vec![OsString::from("guac-bridge"), OsString::from(mode)];
    arguments.extend(std::env::args_os().skip(1));
    main_entry(arguments)
}
pub fn main_entry(arguments: impl IntoIterator<Item = OsString>) -> i32 {
    let arguments: Vec<_> = arguments.into_iter().collect();
    let requested_hook = arguments.iter().any(|arg| {
        arg.to_str()
            .is_some_and(|value| matches!(value, "--ensure-running" | "--stop" | "--status"))
    });
    let cli = match Cli::try_parse_from(arguments) {
        Ok(cli) => cli,
        Err(e) => {
            let code = e.exit_code();
            if requested_hook {
                crate::console::line(e.to_string().trim_end());
            } else {
                let _ = e.print();
            }
            return code;
        }
    };
    let maintenance = match &cli.command {
        Command::Update => Some(crate::update::start()),
        Command::PrepareUpdate => Some(crate::update::prepare()),
        Command::InternalUpdate { parent, uv } => Some(crate::update::worker(*parent, uv)),
        _ => None,
    };
    if let Some(result) = maintenance {
        return match result {
            Ok(()) => 0,
            Err(e) => {
                crate::console::line(&format!("Update failed: {e:#}"));
                1
            }
        };
    }
    let (mode, mut args) = match cli.command {
        Command::Tray { background } => {
            return match crate::tray::run(background) {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("{e:#}");
                    1
                }
            };
        }
        Command::Update | Command::PrepareUpdate | Command::InternalUpdate { .. } => unreachable!(),
        Command::Ssh(a) => ("ssh", a),
        Command::Llm(a) => ("llm", a),
    };
    args.mode = mode.into();
    let hook_output = args.ensure_running || args.stop || args.status;
    let result = (|| -> Result<()> {
        args.validate()?;
        if !args.ensure_running && !args.stop && !args.status {
            crate::diagnostics::init(args.debug_log.as_deref())?;
            crate::diagnostics::event(&format!(
                "transport window={} frame_size={} input_window={} input_chunk={} input_protocol=reliable-v1",
                args.window, args.frame_size, args.input_window, args.input_chunk
            ));
        }
        if args.managed_child {
            use std::io::BufRead;
            let mut line = String::new();
            std::io::stdin().lock().read_line(&mut line)?;
            args.resolved_password = Some(serde_json::from_str(&line)?);
            #[cfg(windows)]
            if args.show_console {
                crate::background::visible_console()?;
            }
        }
        if args.stop || args.status || args.ensure_running {
            return crate::background::handle(args);
        }
        if args.forget_password {
            return crate::credentials::forget(&args);
        }
        if !args.benchmark_control {
            eprintln!("\x1b[32m🐸 Guac Bridge · {}\x1b[0m", mode.to_uppercase());
        }
        let _ = rustls::crypto::ring::default_provider().install_default();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let result = runtime.block_on(crate::client::serve(std::sync::Arc::new(args)));
        runtime.shutdown_timeout(Duration::from_millis(100));
        result
    })();
    let code = match result {
        Ok(()) => 0,
        Err(e) => {
            crate::diagnostics::event(&format!("fatal: {e:#}"));
            let message = format!("\x1b[31mError: {e:#}\x1b[0m");
            if hook_output {
                crate::console::line(&message);
            } else {
                eprintln!("{message}");
            }
            1
        }
    };
    crate::diagnostics::shutdown();
    code
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tcp_and_gateway_windows_are_independent() {
        for (flags, expected) in [
            (vec![], (4, 8)),
            (vec!["--window", "32"], (32, 8)),
            (vec!["--input-window", "64"], (4, 64)),
            (vec!["--window", "1", "--input-window", "128"], (1, 128)),
        ] {
            for mode in ["ssh", "llm"] {
                let cli = Cli::try_parse_from(
                    ["guac-bridge", mode, "--url", "http://example.invalid"]
                        .into_iter()
                        .chain(flags.iter().copied()),
                )
                .unwrap();
                let (Command::Ssh(mut args) | Command::Llm(mut args)) = cli.command else {
                    panic!("Expected bridge command")
                };
                args.validate().unwrap();
                assert_eq!((args.window, args.input_window), expected);
                assert_eq!(args.frame_size, 65536);
            }
        }
    }

    #[test]
    fn protocol_selection_options_are_not_exposed() {
        for mode in ["ssh", "llm"] {
            for option in ["--reliable-input", "--reliable-window"] {
                assert!(
                    Cli::try_parse_from([
                        "guac-bridge",
                        mode,
                        "--url",
                        "http://example.invalid",
                        option
                    ])
                    .is_err()
                );
            }
        }
    }

    #[test]
    fn gateway_window_bounds_reject_zero_and_excessive_queues() {
        for value in ["0", "129"] {
            let cli = Cli::try_parse_from([
                "guac-bridge",
                "ssh",
                "--url",
                "http://example.invalid",
                "--input-window",
                value,
            ])
            .unwrap();
            let (Command::Ssh(mut args) | Command::Llm(mut args)) = cli.command else {
                panic!("Expected bridge command")
            };
            assert!(
                args.validate()
                    .unwrap_err()
                    .to_string()
                    .contains("--input-window")
            );
        }
    }
}
