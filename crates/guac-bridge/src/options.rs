//! Bridge configuration shared by CLI parsing, credentials, deployment and management.
//! Validation resolves the default background lifecycle before any process is started.
use crate::{FRAME, WINDOW};
use anyhow::{Result, ensure};
use clap::{Args as ClapArgs, ValueEnum};

// Retained only for the deprecated command and existing registration filenames.
pub(crate) const LEGACY_HTTP_COMMAND: &str = "llm";

#[derive(
    Clone, Copy, Debug, Default, PartialEq, Eq, ValueEnum, serde::Serialize, serde::Deserialize,
)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Gateway {
    #[default]
    Guacamole,
    Fortigate,
}
impl Gateway {
    pub fn name(self) -> &'static str {
        match self {
            Self::Guacamole => "Guacamole",
            Self::Fortigate => "FortiGate",
        }
    }
    pub fn argument(self) -> &'static str {
        match self {
            Self::Guacamole => "guacamole",
            Self::Fortigate => "fortigate",
        }
    }
}

#[derive(ClapArgs, Clone)]
pub(crate) struct BridgeOptions {
    /// Gateway URL (Guacamole connection URL or FortiGate portal origin). Required when starting a bridge.
    #[arg(value_name = "URL", required_unless_present_any=["stop","status"])]
    pub url: Option<String>,
    /// Gateway login username (prompted when omitted).
    #[arg(long = "user", help_heading = "Connection")]
    pub username: Option<String>,
    /// Gateway type. Existing commands default to Guacamole.
    #[arg(
        long,
        value_enum,
        default_value = "guacamole",
        help_heading = "Gateway"
    )]
    pub gateway: Gateway,
    /// SSH server opened by FortiGate, distinct from the helper's loopback target.
    #[arg(long, help_heading = "Server")]
    pub server_host: Option<String>,
    #[arg(long, default_value_t = 22, value_parser = clap::value_parser!(u16).range(1..), help_heading = "Server")]
    pub server_port: u16,
    #[arg(long, help_heading = "Server")]
    pub server_user: Option<String>,
    #[arg(long, conflicts_with_all=["forget_server_password", "no_keyring"], help_heading = "Server")]
    pub save_server_password: bool,
    #[arg(long, conflicts_with_all=["forget_password", "no_keyring"], help_heading = "Server")]
    pub forget_server_password: bool,
    #[arg(skip)]
    pub resolved_server_password: Option<String>,
    #[arg(long)]
    pub connection_id: Option<String>,
    #[arg(long)]
    pub data_source: Option<String>,
    /// Preserve HTTPS end-to-end (TLS passthrough); defaults the remote service to 443.
    #[arg(long, help_heading = "Endpoints")]
    pub https: bool,
    #[arg(skip)]
    pub legacy_http: bool,
    /// Local loopback port (SSH: 2222, HTTP: 8080).
    #[arg(long = "port", help_heading = "Endpoints")]
    pub port: Option<u16>,
    /// Remote service port (SSH: 22, HTTP: 80; --https: 443).
    #[arg(long = "remote-port", help_heading = "Endpoints")]
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
    /// Resolved by validation; background operation is the default.
    #[arg(skip)]
    pub background: bool,
    /// Run in this terminal until Ctrl+C instead of starting a background bridge.
    #[arg(long, conflicts_with_all=["stop","status"])]
    pub foreground: bool,
    /// Stop the managed bridge on --port, including its active connections.
    #[arg(long, conflicts_with = "status")]
    pub stop: bool,
    /// Show the managed bridge status on --port.
    #[arg(long)]
    pub status: bool,
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
impl BridgeOptions {
    pub fn command_name(&self) -> &str {
        if self.legacy_http {
            LEGACY_HTTP_COMMAND
        } else {
            &self.mode
        }
    }
    pub fn port(&self) -> u16 {
        self.port.unwrap_or(match self.mode.as_str() {
            "ssh" => 2222,
            _ if self.legacy_http => 8000,
            _ => 8080,
        })
    }
    pub fn target_port(&self) -> u16 {
        self.target_port.unwrap_or(if self.https {
            443
        } else {
            match self.mode.as_str() {
                "ssh" => 22,
                _ if self.legacy_http => 8000,
                _ => 80,
            }
        })
    }
    pub(crate) fn validate(&mut self) -> Result<()> {
        ensure!(
            !self.https || self.mode == "http",
            "--https requires an HTTP bridge"
        );
        if !self.foreground
            && !self.managed_child
            && !self.benchmark_control
            && !self.forget_password
            && !self.forget_server_password
            && !self.stop
            && !self.status
        {
            self.background = true;
        }
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
            !(self.background || self.stop || self.status) || self.port() > 0,
            "Managed bridges require a fixed --port greater than zero"
        );
        ensure!(
            (1..=128).contains(&self.input_window),
            "--input-window must be between 1 and 128"
        );
        if self.gateway == Gateway::Fortigate {
            ensure!(
                self.connection_id.is_none() && self.data_source.is_none(),
                "--connection-id and --data-source apply only to Guacamole"
            );
            if !self.stop && !self.status && !self.forget_password {
                ensure!(
                    self.server_host.as_ref().is_some_and(|h| !h.is_empty()
                        && !h.chars().any(|c| c.is_control() || c.is_whitespace())),
                    "FortiGate requires --server-host HOST"
                );
                ensure!(
                    self.forget_password
                        || self.forget_server_password
                        || self.server_os == "windows",
                    "The initial FortiGate adapter requires --server-os windows; Linux carrier support has not been validated"
                );
            }
        } else {
            ensure!(
                self.server_host.is_none()
                    && self.server_user.is_none()
                    && self.server_port == 22
                    && !self.save_server_password
                    && !self.forget_server_password,
                "--server-host, --server-port, --server-user and server password options require --gateway fortigate"
            );
        }
        self.input_chunk = 6048;
        Ok(())
    }
}
