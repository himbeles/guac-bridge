use crate::{cli::Args, shell_quote};
use anyhow::{Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use sha2::{Digest, Sha256};
pub(crate) const AGENT: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/agent"));
pub(crate) fn digest() -> String {
    format!("{:x}", Sha256::digest(AGENT))
}
pub(crate) fn encoded_agent() -> Vec<u8> {
    STANDARD.encode(AGENT).into_bytes()
}
/// Wait for raw/no-echo mode before sending a long command or upload.
/// Replace the interactive shell so readline cannot re-enable echo for the script.
pub(crate) fn prepare(nonce: &str) -> Vec<u8> {
    format!(r"stty raw -echo || exit; printf '\033]482202;2;BOOT\007{nonce}:PREPARED\n\033]482203;\007'; exec /bin/sh -s
").into_bytes()
}
pub(crate) fn command(args: &Args, nonce: &str) -> Result<Vec<u8>> {
    let options = format!(
        "--stdio --target-host {} --target-port {} --connect-timeout {} --max-connections {} --window {} --frame-size {}",
        shell_quote(&args.target_host),
        args.target_port(),
        args.connect_timeout,
        args.max_connections,
        args.window,
        args.frame_size
    );
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
        // One shell command. All incoming payload bytes are consumed by head before exec.
        // A failure exits the login shell, so later upload bytes cannot become shell commands.
        format!(
            r#"umask 077; stty raw -echo || exit; printf '\033]482202;2;BOOT\007'; fail() {{ printf '{nonce}:ERROR:%s\n' "$1"; printf '\033]482203;\007'; exit 1; }}; [ "$(uname -s):$(uname -m)" = Linux:x86_64 ] || fail 'Requires Linux x86-64'; for cmd in head base64 sha256sum cut mktemp chmod mv mkdir rm; do command -v "$cmd" >/dev/null 2>&1 || fail "Missing $cmd"; done; cache="${{XDG_CACHE_HOME:-$HOME/.cache}}/guac-bridge/helpers"; mkdir -p "$cache" || fail 'Cannot create helper cache'; chmod 700 "$cache" || fail 'Cannot protect cache'; agent="$cache/{hash}"; if [ -f "$agent" ] && [ -x "$agent" ] && [ "$(sha256sum "$agent" | cut -d ' ' -f1)" = {hash} ]; then printf '{nonce}:CACHED\n'; else tmp=$(mktemp "$cache/.upload.XXXXXX") || fail 'Cannot create temporary helper'; trap 'rm -f "$tmp"' EXIT HUP INT TERM; printf '{nonce}:UPLOAD\n'; head -c {n} | base64 -d > "$tmp" || fail 'Upload failed'; printf '{nonce}:VERIFY\n'; [ "$(sha256sum "$tmp" | cut -d ' ' -f1)" = {hash} ] || fail 'Helper checksum mismatch'; chmod 700 "$tmp" && mv -f "$tmp" "$agent" || fail 'Cannot install helper'; trap - EXIT HUP INT TERM; fi; printf '{nonce}:STARTING\n'; reason=$("$agent" --version 2>&1) || fail "Helper cannot execute $agent (exit $?): $reason"; printf '\033]482203;\007'; exec "$agent" {options}; exit
"#
        )
    };
    ensure!(
        command.len() < 3900,
        "Bootstrap command exceeds terminal line limit"
    );
    Ok(command.into_bytes())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bundled_agent_is_linux_x64() {
        assert_eq!(&AGENT[..4], b"\x7fELF");
        assert_eq!(&AGENT[18..20], &[62, 0]);
        assert_eq!(digest().len(), 64);
    }
}
