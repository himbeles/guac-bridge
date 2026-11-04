//! Stop bridges and the tray before an installer or external package update.
use anyhow::{Context, Result};
use std::fs::{File, OpenOptions};

fn gate() -> Result<File> {
    Ok(OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(crate::background::directory()?.join("update.lock"))?)
}
pub(crate) fn startup_guard() -> Result<File> {
    let file = gate()?;
    file.try_lock_shared()
        .context("An update is in progress; wait until it finishes before starting a bridge")?;
    Ok(file)
}
fn update_guard() -> Result<File> {
    let file = gate()?;
    file.try_lock().context(
        "A bridge is still starting or another update is running; retry when it finishes",
    )?;
    Ok(file)
}
pub(crate) fn prepare() -> Result<()> {
    let _guard = update_guard()?;
    crate::background::stop_all()?;
    crate::console::line("Bridges and tray stopped. Ready to update.");
    Ok(())
}
