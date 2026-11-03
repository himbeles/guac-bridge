use crate::options::BridgeOptions;
use anyhow::Result;
use clap::{Parser, Subcommand};
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
    #[command(hide = true)]
    Manager,
    /// Stop all registered bridges and the tray before an external update.
    PrepareUpdate,
    /// List registered bridges, their Guacamole destinations and local listeners.
    List,
    /// Show the Windows tray and running bridge manager.
    Tray {
        #[arg(long, hide = true)]
        background: bool,
    },
    /// Forward the remote SSH service.
    Ssh(BridgeOptions),
    /// Forward a remote OpenAI-compatible HTTP endpoint, including streaming.
    Llm(BridgeOptions),
}

pub(crate) fn run(arguments: impl IntoIterator<Item = OsString>) -> i32 {
    let arguments: Vec<_> = arguments.into_iter().collect();
    let default_background = arguments
        .get(1)
        .and_then(|a| a.to_str())
        .is_some_and(|s| matches!(s, "ssh" | "llm"))
        && !arguments.iter().any(|a| {
            a.to_str().is_some_and(|s| {
                matches!(
                    s,
                    "--foreground"
                        | "--managed-child"
                        | "--benchmark-control"
                        | "--forget-password"
                )
            })
        });
    let requested_hook = default_background
        || arguments.iter().any(|arg| {
            arg.to_str()
                .is_some_and(|value| matches!(value, "--stop" | "--status"))
        });
    let cli = match Cli::try_parse_from(arguments) {
        Ok(cli) => cli,
        Err(e) => {
            let code = e.exit_code();
            if requested_hook && code != 0 {
                crate::console::line(e.to_string().trim_end());
            } else {
                let _ = e.print();
            }
            return code;
        }
    };
    let (mode, mut args) = match cli.command {
        Command::List => {
            return match crate::background::list() {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("Cannot list bridges: {e:#}");
                    1
                }
            };
        }
        Command::PrepareUpdate => {
            return match crate::maintenance::prepare() {
                Ok(()) => 0,
                Err(e) => {
                    crate::console::line(&format!("Update preparation failed: {e:#}"));
                    1
                }
            };
        }
        Command::Manager => {
            #[cfg(windows)]
            {
                return match crate::manager::run() {
                    Ok(()) => 0,
                    Err(e) => {
                        eprintln!("{e:#}");
                        1
                    }
                };
            }
            #[cfg(not(windows))]
            {
                eprintln!("The manager is available on Windows");
                return 1;
            }
        }
        Command::Tray { background } => {
            return match crate::tray::run(background) {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("{e:#}");
                    1
                }
            };
        }
        Command::Ssh(a) => ("ssh", a),
        Command::Llm(a) => ("llm", a),
    };
    args.mode = mode.into();
    let hook_output = args.background
        || args.stop
        || args.status
        || (!args.foreground
            && !args.managed_child
            && !args.benchmark_control
            && !args.forget_password);
    let result = (|| -> Result<()> {
        args.validate()?;
        if !args.background && !args.stop && !args.status {
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
        }
        if args.stop || args.status || args.background {
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
    fn start_requires_positional_url_and_user_names_the_guacamole_login() {
        for mode in ["ssh", "llm"] {
            assert!(Cli::try_parse_from(["guac-bridge", mode]).is_err());
            let cli = Cli::try_parse_from([
                "guac-bridge",
                mode,
                "https://guac.example/",
                "--user",
                "alice",
            ])
            .unwrap();
            let (Command::Ssh(args) | Command::Llm(args)) = cli.command else {
                panic!("bridge expected")
            };
            assert_eq!(args.url.as_deref(), Some("https://guac.example/"));
            assert_eq!(args.username.as_deref(), Some("alice"));
            for action in ["--stop", "--status"] {
                assert!(Cli::try_parse_from(["guac-bridge", mode, action]).is_ok());
            }
        }
    }

    #[test]
    fn background_is_default_with_explicit_foreground_and_internal_exceptions() {
        for mode in ["ssh", "llm"] {
            for (extra, managed) in [
                (vec![], true),
                (vec!["--foreground"], false),
                (vec!["--managed-child"], false),
                (vec!["--benchmark-control"], false),
                (vec!["--forget-password"], false),
                (vec!["--stop"], false),
                (vec!["--status"], false),
            ] {
                let cli = Cli::try_parse_from(
                    ["guac-bridge", mode, "https://example.invalid"]
                        .into_iter()
                        .chain(extra),
                )
                .unwrap();
                let (Command::Ssh(mut args) | Command::Llm(mut args)) = cli.command else {
                    panic!("bridge expected")
                };
                args.validate().unwrap();
                assert_eq!(args.background, managed);
            }
        }
    }

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
                    ["guac-bridge", mode, "http://example.invalid"]
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
                    Cli::try_parse_from(["guac-bridge", mode, "http://example.invalid", option])
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
