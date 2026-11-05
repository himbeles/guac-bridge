use crate::options::BridgeOptions;
use anyhow::Result;
use clap::{Parser, Subcommand};
use std::{ffi::OsString, time::Duration};

#[derive(Parser)]
#[command(
    name = "guac-bridge",
    version,
    propagate_version = true,
    about = "🐸 Guac Bridge · TCP forwarding through Guacamole or FortiGate, with SSH, LLM and RDP presets",
    after_help = "Start with: guac-bridge tcp CONNECTION_URL --port SERVICE_PORT.\nSSH, LLM and RDP are TCP presets that select default ports and labels.\nBridge commands start or reuse a background helper by default; use --foreground to stay attached.\nPasswords: GUAC_BRIDGE_PASSWORD for the gateway; GUAC_BRIDGE_SERVER_PASSWORD for the FortiGate SSH server.\nUnspecified passwords use the native credential store or an interactive prompt.\nRun guac-bridge COMMAND --help for every argument and command examples."
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
    #[command(display_order = 10)]
    PrepareUpdate,
    /// List registered bridges, gateways and local listeners.
    #[command(display_order = 11)]
    List,
    /// Show the Windows tray and running bridge manager.
    #[command(display_order = 12)]
    Tray {
        #[arg(long, hide = true)]
        background: bool,
    },
    /// Forward any TCP service (main command); supply either port, or both for different ports.
    #[command(
        display_order = 0,
        after_help = "Example: guac-bridge tcp CONNECTION_URL --port 5432\nA single --port or --remote-port value is used for both local and remote ports.\nDifferent ports: guac-bridge tcp CONNECTION_URL --port 15432 --remote-port 5432\nTCP bytes, binary protocols and TLS pass unchanged. The remote target must be loopback on the helper server.\nStatus: guac-bridge tcp --status --port 5432\nStop: guac-bridge tcp --stop --port 5432"
    )]
    Tcp(BridgeOptions),
    /// TCP preset for SSH: local 2222 -> remote 22.
    #[command(
        display_order = 1,
        after_help = "Example: guac-bridge ssh CONNECTION_URL\nConnect: ssh -p 2222 USER@127.0.0.1\nPreset ports can be overridden independently; --port 2225 keeps remote port 22.\nStop: guac-bridge ssh --stop"
    )]
    Ssh(BridgeOptions),
    /// TCP preset for a model service: local 8000 -> remote 8000.
    #[command(
        display_order = 2,
        after_help = "Example: guac-bridge llm CONNECTION_URL\nConnect your model client to 127.0.0.1:8000 using the service's application protocol.\nThis is the same TCP transport; endpoints and authentication pass unchanged.\nStop: guac-bridge llm --stop"
    )]
    Llm(BridgeOptions),
    /// TCP preset for Remote Desktop: local 33899 -> remote 3389.
    #[command(
        display_order = 3,
        after_help = "Example: guac-bridge rdp CONNECTION_URL\nConnect: mstsc /v:127.0.0.1:33899\nThe gateway SSH terminal runs the helper; no additional SSH tunnel is required.\nRDP security negotiation passes unchanged; UDP is not forwarded.\nStop: guac-bridge rdp --stop"
    )]
    Rdp(BridgeOptions),
}

pub(crate) fn run(arguments: impl IntoIterator<Item = OsString>) -> i32 {
    let arguments: Vec<_> = arguments.into_iter().collect();
    let default_background = arguments
        .get(1)
        .and_then(|a| a.to_str())
        .is_some_and(|s| matches!(s, "tcp" | "ssh" | "llm" | "rdp"))
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
        Command::Rdp(a) => ("rdp", a),
        Command::Tcp(a) => ("tcp", a),
    };
    args.mode = mode.into();
    let hook_output = args.background
        || args.stop
        || args.status
        || (!args.foreground
            && !args.managed_child
            && !args.benchmark_control
            && !args.forget_password
            && !args.forget_server_password);
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
            if args.gateway == crate::options::Gateway::Guacamole {
                args.resolved_password = Some(serde_json::from_str(&line)?);
            } else {
                #[derive(serde::Deserialize)]
                struct Handoff {
                    gateway_password: String,
                    server_password: String,
                }
                let handoff: Handoff = serde_json::from_str(&line)
                    .map_err(|_| anyhow::anyhow!("Invalid private credential handoff"))?;
                args.resolved_password = Some(handoff.gateway_password);
                args.resolved_server_password = Some(handoff.server_password);
            }
        }
        if args.stop || args.status || args.background {
            return crate::background::handle(args);
        }
        if args.forget_password || args.forget_server_password {
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
    fn bridge(mode: &str, flags: &[&str]) -> BridgeOptions {
        let cli = Cli::try_parse_from(
            [
                "guac-bridge",
                mode,
                "https://gateway.example/#/client/MQBjAG15c3Fs",
            ]
            .into_iter()
            .chain(flags.iter().copied()),
        )
        .unwrap();
        let mut args = match cli.command {
            Command::Tcp(a) | Command::Ssh(a) | Command::Llm(a) | Command::Rdp(a) => a,
            _ => panic!("Bridge command expected"),
        };
        args.mode = mode.into();
        args
    }
    #[test]
    fn supported_commands_require_start_url_and_allow_control_without_url() {
        for mode in ["tcp", "ssh", "llm", "rdp"] {
            assert!(Cli::try_parse_from(["guac-bridge", mode]).is_err());
            let args = bridge(mode, &["--user", "alice", "--port", "5432"]);
            assert_eq!(args.username.as_deref(), Some("alice"));
            for action in ["--stop", "--status"] {
                assert!(Cli::try_parse_from(["guac-bridge", mode, action]).is_ok());
            }
        }
    }
    #[test]
    fn removed_http_commands_and_https_flag_are_rejected() {
        for mode in ["http", "https"] {
            assert!(Cli::try_parse_from(["guac-bridge", mode, "https://gateway.example"]).is_err());
        }
        for mode in ["tcp", "ssh", "llm", "rdp"] {
            assert!(
                Cli::try_parse_from(["guac-bridge", mode, "https://gateway.example", "--https"])
                    .is_err()
            );
        }
    }
    #[test]
    fn background_is_default_with_explicit_foreground_and_control_exceptions() {
        for mode in ["tcp", "ssh", "llm", "rdp"] {
            for (extra, managed) in [
                (vec![], true),
                (vec!["--foreground"], false),
                (vec!["--managed-child"], false),
                (vec!["--benchmark-control"], false),
                (vec!["--forget-password"], false),
                (vec!["--stop"], false),
                (vec!["--status"], false),
            ] {
                let mut flags = vec!["--port", "5432"];
                flags.extend(extra);
                let mut args = bridge(mode, &flags);
                args.validate().unwrap();
                assert_eq!(args.background, managed);
            }
        }
    }
    #[test]
    fn tcp_mirrors_a_single_port_and_preserves_explicit_pairs() {
        for (flags, ports, valid) in [
            (vec![], (0, 0), false),
            (vec!["--port", "5432"], (5432, 5432), true),
            (vec!["--remote-port", "5432"], (5432, 5432), true),
            (
                vec!["--port", "15432", "--remote-port", "5432"],
                (15432, 5432),
                true,
            ),
            (vec!["--port", "0", "--foreground"], (0, 0), false),
            (
                vec!["--port", "0", "--remote-port", "5432", "--foreground"],
                (0, 5432),
                true,
            ),
            (vec!["--remote-port", "5432", "--stop"], (5432, 5432), true),
            (
                vec!["--remote-port", "5432", "--status"],
                (5432, 5432),
                true,
            ),
            (vec!["--forget-password"], (0, 0), true),
        ] {
            let mut args = bridge("tcp", &flags);
            assert_eq!((args.port(), args.target_port()), ports, "{flags:?}");
            assert_eq!(args.validate().is_ok(), valid, "{flags:?}");
        }
    }
    #[test]
    fn presets_keep_defaults_and_independent_overrides() {
        for (mode, local, remote) in [("ssh", 2222, 22), ("llm", 8000, 8000), ("rdp", 33899, 3389)]
        {
            let mut args = bridge(mode, &[]);
            args.validate().unwrap();
            assert_eq!((args.port(), args.target_port()), (local, remote));
            let args = bridge(mode, &["--port", "2225"]);
            assert_eq!((args.port(), args.target_port()), (2225, remote));
            let args = bridge(mode, &["--remote-port", "2227"]);
            assert_eq!((args.port(), args.target_port()), (local, 2227));
        }
    }
    #[test]
    fn tcp_and_gateway_windows_are_independent() {
        for mode in ["tcp", "ssh", "llm", "rdp"] {
            for (flags, expected) in [
                (vec![], (4, 8)),
                (vec!["--window", "32"], (32, 8)),
                (vec!["--input-window", "64"], (4, 64)),
                (vec!["--window", "1", "--input-window", "128"], (1, 128)),
            ] {
                let mut extra = vec!["--port", "5432"];
                extra.extend(flags);
                let mut args = bridge(mode, &extra);
                args.validate().unwrap();
                assert_eq!((args.window, args.input_window), expected);
                assert_eq!(args.frame_size, 65536);
            }
            for option in ["--reliable-input", "--reliable-window"] {
                assert!(
                    Cli::try_parse_from(["guac-bridge", mode, "https://gateway.example", option])
                        .is_err()
                );
            }
        }
    }
    #[test]
    fn gateway_window_bounds_reject_zero_and_excessive_queues() {
        for value in ["0", "129"] {
            let mut args = bridge("ssh", &["--input-window", value]);
            assert!(
                args.validate()
                    .unwrap_err()
                    .to_string()
                    .contains("--input-window")
            );
        }
    }
    #[test]
    fn gateway_options_are_shared_and_preserve_guacamole_default() {
        for mode in ["tcp", "ssh", "llm", "rdp"] {
            let mut args = bridge(mode, &["--port", "5432"]);
            args.validate().unwrap();
            assert_eq!(args.gateway, crate::options::Gateway::Guacamole);
            let mut args = bridge(
                mode,
                &[
                    "--port",
                    "5432",
                    "--gateway",
                    "fortigate",
                    "--server-host",
                    "server.internal",
                    "--server-user",
                    "alice",
                    "--server-os",
                    "windows",
                ],
            );
            args.validate().unwrap();
            assert_eq!(args.server_port, 22);
            args.server_os = "linux".into();
            assert!(args.validate().is_err());
            args.server_os = "windows".into();
            args.connection_id = Some("guac-only".into());
            assert!(args.validate().is_err());
        }
    }
}
