use crate::cli::Args;
use anyhow::{Context, Result, bail};
use url::Url;
fn entry(args: &Args, user: &str) -> Result<keyring::Entry> {
    let mut url = Url::parse(args.url.as_deref().context("--url is required")?)?;
    url.set_fragment(None);
    url.set_query(None);
    let service = format!("guac-bridge:{}", url.as_str().trim_end_matches('/'));
    keyring::Entry::new(&service, user).context("System credential store is unavailable")
}
pub(crate) fn username(args: &Args) -> Result<String> {
    if let Some(name) = &args.username {
        return Ok(name.clone());
    }
    use std::io::Write;
    eprint!("Guacamole username: ");
    std::io::stderr().flush()?;
    let mut name = String::new();
    std::io::stdin().read_line(&mut name)?;
    let name = name.trim();
    if name.is_empty() {
        bail!("A Guacamole username is required");
    }
    Ok(name.into())
}
pub(crate) fn password(args: &Args, user: &str) -> Result<String> {
    if let Some(value) = &args.resolved_password {
        return Ok(value.clone());
    }
    if let Ok(value) = std::env::var("GUAC_BRIDGE_PASSWORD") {
        return Ok(value);
    }
    if !args.no_keyring {
        match entry(args, user).and_then(|e| e.get_password().map_err(Into::into)) {
            Ok(value) => return Ok(value),
            Err(_) if args.managed_child => bail!(
                "No saved password available. Run the bridge in the foreground with --save-password first."
            ),
            Err(_) => (),
        }
    }
    rpassword::prompt_password("Guacamole password: ")
        .context("Cannot read password from the console")
}
pub(crate) fn save(args: &Args, user: &str, password: &str) -> Result<()> {
    entry(args, user)?
        .set_password(password)
        .map_err(|_| anyhow::anyhow!("Cannot save password in the system credential store"))
}
pub(crate) fn forget(args: &Args) -> Result<()> {
    let user = username(args)?;
    entry(args, &user)?
        .delete_credential()
        .map_err(|_| anyhow::anyhow!("Cannot remove the saved password"))?;
    eprintln!("Saved password removed");
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
