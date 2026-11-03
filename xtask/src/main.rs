//! Repository tasks. Cargo is the only build entry point on Linux and Windows.
mod build;
mod distribution;
mod installer;
mod project;
mod release;

use anyhow::Result;
use clap::{Args, Parser, Subcommand, ValueEnum};
use project::Project;
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Build, check and package Guac Bridge from the repository root")]
struct Cli {
    #[command(subcommand)]
    command: Task,
}

#[derive(Subcommand)]
enum Task {
    /// Build matching helpers, then the native client and console fixtures.
    Build(BuildOptions),
    /// Format-check, lint and unit-test the entire Rust workspace.
    Check(BuildOptions),
    /// Build and run the existing real-PTY integration tests (Python + aiohttp).
    Integration {
        #[command(flatten)]
        build: BuildOptions,
        #[arg(long, value_enum, default_value = "all")]
        suite: Suite,
    },
    /// Build and stage one standalone server helper, without client dependencies.
    Helper {
        #[arg(value_enum)]
        os: build::ServerOs,
    },
    /// Create the native portable archive, optionally from a manylinux/Windows wheel.
    Portable {
        #[arg(long)]
        wheel: Option<PathBuf>,
        #[command(flatten)]
        build: BuildOptions,
    },
    /// Build the Windows installer with its private UI runtime (Inno Setup 6).
    Installer(BuildOptions),
    /// Build optional Python/uv distributions using maturin.
    Python {
        #[command(flatten)]
        build: BuildOptions,
        /// Build the source distribution instead of a wheel.
        #[arg(long)]
        sdist: bool,
    },
    /// Check version/tag consistency without building or publishing.
    VerifyRelease {
        #[arg(long)]
        tag: Option<String>,
    },
    /// Write checksums and changelog notes for existing dist/ artifacts; never publish.
    ReleaseNotes,
}

#[derive(Args, Default)]
struct BuildOptions {
    /// Use already-staged matching helpers instead of rebuilding them (CI).
    #[arg(long, conflicts_with = "windows_agent")]
    prepared: bool,
    /// Embed a Windows helper built from this revision when building on Linux.
    #[arg(long)]
    windows_agent: Option<PathBuf>,
}

#[derive(Clone, Copy, ValueEnum)]
enum Suite {
    All,
    Native,
    Console,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let project = Project::load()?;
    match cli.command {
        Task::Build(options) => {
            build::client(&project, &options, false)?;
        }
        Task::Check(options) => {
            build::prepare(&project, &options)?;
            project.cargo(&["fmt", "--all", "--", "--check"])?;
            project.cargo(&[
                "clippy",
                "--locked",
                "--workspace",
                "--all-targets",
                "--",
                "-D",
                "warnings",
            ])?;
            project.cargo(&["test", "--locked", "--workspace"])?;
        }
        Task::Helper { os } => {
            build::helper(&project, os)?;
        }
        Task::Integration {
            build: options,
            suite,
        } => {
            build::client(&project, &options, false)?;
            build::integration(&project, suite)?;
        }
        Task::Portable {
            wheel,
            build: options,
        } => {
            let binary = if wheel.is_none() {
                Some(build::client(&project, &options, false)?)
            } else {
                None
            };
            distribution::portable(&project, binary.as_deref(), wheel.as_deref())?;
        }
        Task::Installer(options) => installer::build(&project, &options)?,
        Task::Python {
            build: options,
            sdist,
        } => {
            build::prepare(&project, &options)?;
            if sdist {
                project.python(&["-m", "maturin", "sdist", "--out", "dist"])?;
            } else {
                project.python(&[
                    "-m",
                    "maturin",
                    "build",
                    "--locked",
                    "--release",
                    "--out",
                    "dist",
                ])?;
            }
        }
        Task::VerifyRelease { tag } => release::verify(&project, tag.as_deref())?,
        Task::ReleaseNotes => release::notes(&project)?,
    }
    Ok(())
}
