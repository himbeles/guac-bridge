use crate::{options::BridgeOptions, shell_quote};
use anyhow::{Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use sha2::{Digest, Sha256};
// Bound terminal input between remote shell acknowledgements. Base64 boundaries must be multiples of four.
pub(crate) const UPLOAD_CHUNK: usize = 24576;
// Keep each Guacamole input blob below typical PTY input-buffer capacity.
pub(crate) const UPLOAD_INPUT_CHUNK: usize = 1024;
pub(crate) const AGENT: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/agent"));
pub(crate) fn digest() -> String {
    format!("{:x}", Sha256::digest(AGENT))
}
pub(crate) const WINDOWS_AGENT: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/agent-windows"));
pub(crate) fn agent(args: &BridgeOptions) -> Result<&'static [u8]> {
    if args.server_os == "windows" {
        ensure!(
            !WINDOWS_AGENT.is_empty(),
            "This source build has no bundled Windows helper. Use a release build or set GUAC_BRIDGE_WINDOWS_AGENT when compiling."
        );
        Ok(WINDOWS_AGENT)
    } else {
        Ok(AGENT)
    }
}
pub(crate) fn encoded_agent(args: &BridgeOptions) -> Result<Vec<u8>> {
    Ok(STANDARD.encode(agent(args)?).into_bytes())
}

/// Wait for raw/no-echo mode before sending a long command or upload.
/// Replace the interactive shell so readline cannot re-enable echo for the script.
pub(crate) fn prepare(args: &BridgeOptions, nonce: &str) -> Result<Vec<u8>> {
    if args.server_os == "windows" {
        return windows_command(args, nonce);
    }
    Ok(format!(r"stty raw -echo || exit; printf '\033]482202;2;BOOT\007{nonce}:PREPARED\n\033]482203;\007'; exec /bin/sh -s
").into_bytes())
}
pub(crate) fn command(args: &BridgeOptions, nonce: &str) -> Result<Vec<u8>> {
    if args.server_os == "windows" {
        return Ok(Vec::new());
    }
    let mut options = format!(
        "--stdio --target-host {} --target-port {} --connect-timeout {} --max-connections {} --window {} --frame-size {}",
        shell_quote(&args.target_host),
        args.target_port(),
        args.connect_timeout,
        args.max_connections,
        args.window,
        args.frame_size
    );
    if args.benchmark_control {
        options.push_str(" --benchmark-control");
    }
    if let Some(path) = &args.server_debug_log {
        options.push_str(&format!(" --debug-log {}", shell_quote(path)));
    }
    let command = if let Some(path) = &args.server_command {
        let path = if let Some(tail) = path.strip_prefix("~/") {
            format!("\"$HOME\"/{}", shell_quote(tail))
        } else {
            shell_quote(path)
        };
        format!("exec {path} {options}; exit\n")
    } else {
        let hash = digest();
        let n = AGENT.len().div_ceil(3) * 4;
        let chunk = args.upload_batch_size;
        // One shell command. All incoming payload bytes are consumed by head before exec.
        // A failure exits the login shell, so later upload bytes cannot become shell commands.
        format!(
            r#"umask 077; stty raw -echo || exit; printf '\033]482202;2;BOOT\007'; fail() {{ printf '{nonce}:ERROR:%s\n' "$1"; printf '\033]482203;\007'; exit 1; }}; [ "$(uname -s):$(uname -m)" = Linux:x86_64 ] || fail 'Requires Linux x86-64'; for cmd in head base64 sha256sum cut mktemp chmod mv mkdir rm; do command -v "$cmd" >/dev/null 2>&1 || fail "Missing $cmd"; done; cache="${{XDG_CACHE_HOME:-$HOME/.cache}}/guac-bridge/helpers"; mkdir -p "$cache" || fail 'Cannot create helper cache'; chmod 700 "$cache" || fail 'Cannot protect cache'; agent="$cache/{hash}"; if [ -f "$agent" ] && [ -x "$agent" ] && [ "$(sha256sum "$agent" | cut -d ' ' -f1)" = {hash} ]; then printf '{nonce}:CACHED\n'; else tmp=$(mktemp "$cache/.upload.XXXXXX") || fail 'Cannot create temporary helper'; trap 'rm -f "$tmp"' EXIT HUP INT TERM; printf '{nonce}:UPLOAD\n'; remaining={n}; index=0; while [ "$remaining" -gt 0 ]; do count={chunk}; if [ "$remaining" -lt "$count" ]; then count="$remaining"; fi; reason=$( {{ head -c "$count" | base64 -d >> "$tmp"; }} 2>&1) || fail "Upload block $((index+1)) decode failed: $reason"; remaining=$((remaining-count)); index=$((index+1)); printf '{nonce}:CHUNK:%s\n' "$index"; done; printf '{nonce}:VERIFY\n'; [ "$(sha256sum "$tmp" | cut -d ' ' -f1)" = {hash} ] || fail 'Helper checksum mismatch'; chmod 700 "$tmp" && mv -f "$tmp" "$agent" || fail 'Cannot install helper'; trap - EXIT HUP INT TERM; fi; printf '{nonce}:STARTING\n'; reason=$("$agent" --version 2>&1) || fail "Helper cannot execute $agent (exit $?): $reason"; printf '\033]482203;\007'; exec "$agent" {options}; exit
"#
        )
    };
    ensure!(
        command.len() < 3900,
        "Bootstrap command exceeds terminal line limit"
    );
    Ok(command.into_bytes())
}

fn powershell_quote(text: &str) -> String {
    format!("'{}'", text.replace('\'', "''"))
}
fn windows_command(args: &BridgeOptions, nonce: &str) -> Result<Vec<u8>> {
    let bytes = if args.server_command.is_some() {
        &[][..]
    } else {
        agent(args)?
    };
    let hash = format!("{:x}", Sha256::digest(bytes));
    let mut options = format!(
        "--target-host {} --target-port {} --connect-timeout {} --max-connections {} --window {} --frame-size {}",
        powershell_quote(&args.target_host),
        args.target_port(),
        args.connect_timeout,
        args.max_connections,
        args.window,
        args.frame_size
    );
    if args.benchmark_control {
        options.push_str(" --benchmark-control");
    }
    if let Some(path) = &args.server_debug_log {
        options.push_str(&format!(" --debug-log {}", powershell_quote(path)));
    }
    let script = include_str!("windows_bootstrap.ps1")
        .replace("@NONCE@", nonce)
        .replace("@HASH@", &hash)
        .replace("@COUNT@", &(bytes.len().div_ceil(3) * 4).to_string())
        .replace(
            "@MANUAL@",
            &args
                .server_command
                .as_deref()
                .map(powershell_quote)
                .unwrap_or_else(|| "$null".into()),
        )
        .replace("@OPTIONS@", &options);
    powershell_command(&script)
}

fn powershell_command(script: &str) -> Result<Vec<u8>> {
    let script: String = script.lines().map(str::trim).collect::<Vec<_>>().join("\n");
    let utf16: Vec<u8> = script.encode_utf16().flat_map(u16::to_le_bytes).collect();
    let command = format!(
        "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand {}\r",
        STANDARD.encode(utf16)
    );
    ensure!(
        command.len() < 8000,
        "Windows bootstrap exceeds the command line limit"
    );
    Ok(command.into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_bootstrap_is_encoded_and_bounded() {
        use clap::Parser;
        #[derive(Parser)]
        struct TestArgs {
            #[command(flatten)]
            args: BridgeOptions,
        }
        let args = TestArgs::parse_from([
            "test",
            "https://example.invalid/#/client/test",
            "--server-os",
            "windows",
        ]);
        // Command length depends on the helper hash, never its file size.
        let script=include_str!("windows_bootstrap.ps1")
            .replace("@NONCE@", &"a".repeat(32)).replace("@HASH@", &"b".repeat(64))
            .replace("@COUNT@", "99999999").replace("@MANUAL@", "$null")
            .replace("@OPTIONS@", "--target-host '127.0.0.1' --target-port 22 --connect-timeout 30 --max-connections 8 --window 32 --frame-size 65536");
        let lf = script.replace("\r\n", "\n");
        let crlf = lf.replace('\n', "\r\n");
        let command = powershell_command(&lf).unwrap();
        assert!(command.len() < 8000);
        assert_eq!(command, powershell_command(&crlf).unwrap());
        assert_eq!(args.args.server_os, "windows");
        assert_eq!(powershell_quote("a'b"), "'a''b'");
    }
    #[test]
    fn chunked_upload_can_decode_each_block_independently() {
        assert_eq!(UPLOAD_CHUNK % 4, 0);
        for size in [
            0,
            1,
            2,
            3,
            10,
            UPLOAD_CHUNK,
            UPLOAD_CHUNK + 1,
            UPLOAD_CHUNK * 2 + 9,
        ] {
            let bytes: Vec<u8> = (0..size).map(|i| i as u8).collect();
            let encoded = STANDARD.encode(&bytes);
            let mut decoded = Vec::new();
            for part in encoded.as_bytes().chunks(UPLOAD_CHUNK) {
                decoded.extend(STANDARD.decode(part).unwrap());
            }
            assert_eq!(decoded, bytes);
        }
    }
    #[test]
    fn bundled_agent_is_linux_x64() {
        assert_eq!(&AGENT[..4], b"\x7fELF");
        assert_eq!(&AGENT[18..20], &[62, 0]);
        assert_eq!(digest().len(), 64);
    }
}
