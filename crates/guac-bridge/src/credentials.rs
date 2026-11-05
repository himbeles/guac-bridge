use crate::options::{BridgeOptions, Gateway};
use anyhow::{Context, Result, bail};
use url::Url;
fn entry(args: &BridgeOptions, user: &str, server: bool) -> Result<keyring::Entry> {
    let mut url = Url::parse(args.url.as_deref().context("URL is required")?)?;
    url.set_fragment(None);
    url.set_query(None);
    let service = if server {
        format!(
            "guac-bridge:fortigate:server:{}:{}:{}",
            url.origin().ascii_serialization(),
            args.server_host
                .as_deref()
                .context("--server-host is required")?,
            args.server_port
        )
    } else if args.gateway == Gateway::Guacamole {
        format!("guac-bridge:{}", url.as_str().trim_end_matches('/'))
    } else {
        format!(
            "guac-bridge:fortigate:{}",
            url.origin().ascii_serialization()
        )
    };
    keyring::Entry::new(&service, user).context("System credential store is unavailable")
}
fn prompt_user(value: &Option<String>, label: &str) -> Result<String> {
    if let Some(name) = value {
        return Ok(name.clone());
    }
    use std::io::Write;
    eprint!("{label} username: ");
    std::io::stderr().flush()?;
    let mut name = String::new();
    std::io::stdin().read_line(&mut name)?;
    let name = name.trim();
    if name.is_empty() {
        bail!("A {label} username is required");
    }
    Ok(name.into())
}
pub(crate) fn username(args: &BridgeOptions) -> Result<String> {
    prompt_user(&args.username, args.gateway.name())
}
pub(crate) fn server_username(args: &BridgeOptions) -> Result<String> {
    prompt_user(&args.server_user, "Server SSH")
}
fn read_password(args: &BridgeOptions, user: &str, server: bool) -> Result<String> {
    let resolved = if server {
        &args.resolved_server_password
    } else {
        &args.resolved_password
    };
    if let Some(value) = resolved {
        return Ok(value.clone());
    }
    let variable = if server {
        "GUAC_BRIDGE_SERVER_PASSWORD"
    } else {
        "GUAC_BRIDGE_PASSWORD"
    };
    if let Ok(value) = std::env::var(variable) {
        return Ok(value);
    }
    if !args.no_keyring {
        match entry(args, user, server).and_then(|e| e.get_password().map_err(Into::into)) {
            Ok(value) => return Ok(value),
            Err(_) if args.managed_child => {
                bail!("No saved password available; authenticate in the calling console first")
            }
            Err(_) => (),
        }
    }
    let label = if server {
        "Server SSH"
    } else {
        args.gateway.name()
    };
    rpassword::prompt_password(format!("{label} password: "))
        .context("Cannot read password from the console")
}
pub(crate) fn password(args: &BridgeOptions, user: &str) -> Result<String> {
    read_password(args, user, false)
}
pub(crate) fn server_password(args: &BridgeOptions, user: &str) -> Result<String> {
    read_password(args, user, true)
}
fn store(args: &BridgeOptions, user: &str, password: &str, server: bool) -> Result<()> {
    entry(args, user, server)?
        .set_password(password)
        .map_err(|_| anyhow::anyhow!("Cannot save password in the system credential store"))
}
pub(crate) fn save(args: &BridgeOptions, user: &str, password: &str) -> Result<()> {
    store(args, user, password, false)
}
pub(crate) fn save_server(args: &BridgeOptions, user: &str, password: &str) -> Result<()> {
    store(args, user, password, true)
}
pub(crate) fn forget(args: &BridgeOptions) -> Result<()> {
    let server = args.forget_server_password;
    let user = if server {
        server_username(args)?
    } else {
        username(args)?
    };
    entry(args, &user, server)?
        .delete_credential()
        .map_err(|_| anyhow::anyhow!("Cannot remove the saved password"))?;
    eprintln!(
        "Saved {}password removed",
        if server { "server " } else { "" }
    );
    Ok(())
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    fn native_windows_store_roundtrip() {
        if std::env::var_os("GUAC_TEST_NATIVE_KEYRING").is_none() {
            return;
        }
        let service = format!("guac-bridge-test:{}", uuid::Uuid::new_v4());
        let entry = keyring::Entry::new(&service, "test-user").unwrap();
        entry.set_password("synthetic-test-password").unwrap();
        assert_eq!(entry.get_password().unwrap(), "synthetic-test-password");
        entry.delete_credential().unwrap();
        assert!(matches!(entry.get_password(), Err(keyring::Error::NoEntry)));
    }
}
