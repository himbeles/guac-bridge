use crate::options::{BridgeOptions, LEGACY_HTTP_COMMAND};
use anyhow::Result;
use clap::{Parser, Subcommand};
use std::{ffi::OsString, time::Duration};

#[derive(Parser)]
#[command(
    name = "guac-bridge",
    version,
    propagate_version = true,
    about = "🐸 Guac Bridge · SSH, RDP, HTTP(S) and TCP bridges through Guacamole or FortiGate",
    after_help = "Bridge commands start or reuse a background helper by default; use --foreground to stay attached.\nPasswords: GUAC_BRIDGE_PASSWORD for the gateway; GUAC_BRIDGE_SERVER_PASSWORD for the FortiGate SSH server.\nUnspecified passwords use the native credential store or an interactive prompt.\nRun guac-bridge COMMAND --help for every argument and command examples."
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
    /// List registered bridges, gateways and local listeners.
    List,
    /// Show the Windows tray and running bridge manager.
    Tray {
        #[arg(long, hide = true)]
        background: bool,
    },
    /// Forward the remote SSH service.
    #[command(
        after_help = "Defaults: local 127.0.0.1:2222 -> remote loopback:22.\nExample: guac-bridge ssh CONNECTION_URL\nConnect: ssh -p 2222 USER@127.0.0.1\nStop: guac-bridge ssh --stop"
    )]
    Ssh(BridgeOptions),
    /// Forward any HTTP service, including all paths, methods and streaming; use --https for TLS passthrough.
    #[command(
        after_help = "Defaults: local 127.0.0.1:8000 -> remote loopback:8000.\nExample: guac-bridge http CONNECTION_URL\nHTTPS: guac-bridge http CONNECTION_URL --https (remote port 443).\nTLS passes unchanged; configure service hostname/SNI and certificate trust in your HTTP client.\nStop: guac-bridge http --stop"
    )]
    Http(BridgeOptions),
    /// Forward the remote RDP service directly to your Remote Desktop client.
    #[command(
        after_help = "Defaults: local 127.0.0.1:13389 -> remote loopback:3389.\nExample: guac-bridge rdp CONNECTION_URL\nConnect: mstsc /v:127.0.0.1:13389\nThe gateway SSH terminal still runs the helper; no additional SSH tunnel is required.\nRDP TCP traffic and its security negotiation pass unchanged; UDP is not forwarded.\nStop: guac-bridge rdp --stop"
    )]
    Rdp(BridgeOptions),
    /// Forward any TCP service; specify --port and --remote-port.
    #[command(
        after_help = "Example: guac-bridge tcp CONNECTION_URL --port 15432 --remote-port 5432\nForwards local 127.0.0.1:15432 -> remote loopback:5432, preserving binary data and TLS.\nBoth ports are required at startup. The remote target must be loopback on the helper server.\nStatus: guac-bridge tcp --status --port 15432\nStop: guac-bridge tcp --stop --port 15432"
    )]
    Tcp(BridgeOptions),
    /// Deprecated: use http. Existing defaults are preserved.
    #[command(name = LEGACY_HTTP_COMMAND)]
    LegacyHttp(BridgeOptions),
}

pub(crate) fn run(arguments: impl IntoIterator<Item = OsString>) -> i32 {
    let arguments: Vec<_> = arguments.into_iter().collect();
    let default_background =
        arguments.get(1).and_then(|a| a.to_str()).is_some_and(|s| {
            matches!(s, "ssh" | "http" | "rdp" | "tcp") || s == LEGACY_HTTP_COMMAND
        }) && !arguments.iter().any(|a| {
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
        Command::LegacyHttp(mut a) => {
            a.legacy_http = true;
            ("http", a)
        }
        Command::Http(a) => ("http", a),
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

    #[test]
    fn start_requires_positional_url_and_user_names_the_guacamole_login() {
        for mode in ["ssh", "http", "rdp", LEGACY_HTTP_COMMAND] {
            assert!(Cli::try_parse_from(["guac-bridge", mode]).is_err());
            let cli = Cli::try_parse_from([
                "guac-bridge",
                mode,
                "https://guac.example/",
                "--user",
                "alice",
            ])
            .unwrap();
            let (Command::Ssh(args)
            | Command::Http(args)
            | Command::Rdp(args)
            | Command::Tcp(args)
            | Command::LegacyHttp(args)) = cli.command
            else {
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
        for mode in ["ssh", "http", "rdp", LEGACY_HTTP_COMMAND] {
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
                let (Command::Ssh(mut args)
                | Command::Http(mut args)
                | Command::Rdp(mut args)
                | Command::Tcp(mut args)
                | Command::LegacyHttp(mut args)) = cli.command
                else {
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
            for mode in ["ssh", "http", "rdp", LEGACY_HTTP_COMMAND] {
                let cli = Cli::try_parse_from(
                    ["guac-bridge", mode, "http://example.invalid"]
                        .into_iter()
                        .chain(flags.iter().copied()),
                )
                .unwrap();
                let (Command::Ssh(mut args)
                | Command::Http(mut args)
                | Command::Rdp(mut args)
                | Command::Tcp(mut args)
                | Command::LegacyHttp(mut args)) = cli.command
                else {
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
        for mode in ["ssh", "http", "rdp", LEGACY_HTTP_COMMAND] {
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
            let (Command::Ssh(mut args)
            | Command::Http(mut args)
            | Command::Rdp(mut args)
            | Command::Tcp(mut args)
            | Command::LegacyHttp(mut args)) = cli.command
            else {
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
    #[test]
    fn gateway_options_are_shared_by_bridges_and_preserve_guacamole_default() {
        for mode in ["ssh", "http", "rdp", LEGACY_HTTP_COMMAND] {
            let cli = Cli::try_parse_from([
                "guac-bridge",
                mode,
                "https://guac.example/#/client/MQBjAG15c3Fs",
            ])
            .unwrap();
            let (Command::Ssh(mut old)
            | Command::Http(mut old)
            | Command::Rdp(mut old)
            | Command::Tcp(mut old)
            | Command::LegacyHttp(mut old)) = cli.command
            else {
                panic!()
            };
            old.validate().unwrap();
            assert_eq!(old.gateway, crate::options::Gateway::Guacamole);
            let cli = Cli::try_parse_from([
                "guac-bridge",
                mode,
                "https://gateway.example",
                "--gateway",
                "fortigate",
                "--server-host",
                "server.internal",
                "--server-user",
                "server-user",
                "--server-os",
                "windows",
            ])
            .unwrap();
            let (Command::Ssh(mut args)
            | Command::Http(mut args)
            | Command::Rdp(mut args)
            | Command::Tcp(mut args)
            | Command::LegacyHttp(mut args)) = cli.command
            else {
                panic!()
            };
            args.validate().unwrap();
            assert_eq!(args.server_port, 22);
            args.server_os = "linux".into();
            assert!(args.validate().is_err());
            args.server_os = "windows".into();
            args.connection_id = Some("guac-only".into());
            assert!(args.validate().is_err());
        }
    }
    #[test]
    fn tcp_requires_explicit_endpoints_and_preserves_management_actions() {
        for (flags, valid) in [
            (vec![], false),
            (vec!["--port", "5432"], false),
            (vec!["--remote-port", "5432"], false),
            (vec!["--port", "5432", "--remote-port", "5432"], true),
            (
                vec!["--port", "0", "--remote-port", "5432", "--foreground"],
                true,
            ),
            (vec!["--port", "5432", "--stop"], true),
            (vec!["--port", "5432", "--status"], true),
            (vec!["--stop"], false),
            (vec!["--forget-password"], true),
        ] {
            let cli = Cli::try_parse_from(
                [
                    "guac-bridge",
                    "tcp",
                    "https://gateway.example/#/client/MQBjAG15c3Fs",
                ]
                .into_iter()
                .chain(flags.iter().copied()),
            )
            .unwrap();
            let Command::Tcp(mut args) = cli.command else {
                panic!()
            };
            args.mode = "tcp".into();
            assert_eq!(args.validate().is_ok(), valid, "{flags:?}");
            if args.background {
                assert_eq!(args.port(), 5432);
                assert_eq!(args.target_port(), 5432);
            }
        }
    }
    #[test]
    fn service_defaults_and_https_validation_preserve_legacy_compatibility() {
        for (mode, local, remote) in [
            ("ssh", 2222, 22),
            ("http", 8000, 8000),
            ("rdp", 13389, 3389),
            (LEGACY_HTTP_COMMAND, 8000, 8000),
        ] {
            for https in [false, true] {
                let mut argv = vec![
                    "guac-bridge",
                    mode,
                    "https://gateway.example/#/client/MQBjAG15c3Fs",
                ];
                if https {
                    argv.push("--https");
                }
                let cli = Cli::try_parse_from(argv).unwrap();
                let mut args = match cli.command {
                    Command::Ssh(a)
                    | Command::Http(a)
                    | Command::Rdp(a)
                    | Command::Tcp(a)
                    | Command::LegacyHttp(a) => a,
                    _ => panic!(),
                };
                args.legacy_http = mode == LEGACY_HTTP_COMMAND;
                args.mode = if args.legacy_http { "http" } else { mode }.into();
                if mode != "http" && !args.legacy_http && https {
                    assert!(args.validate().is_err());
                    continue;
                }
                args.validate().unwrap();
                assert_eq!(args.port(), local);
                assert_eq!(args.target_port(), if https { 443 } else { remote });
                args.target_port = Some(8443);
                assert_eq!(args.target_port(), 8443);
            }
        }
    }
}
