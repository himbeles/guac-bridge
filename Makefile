# Linux build; no GitHub, Actions or Python required.
# To bundle Windows server support: make build WINDOWS_AGENT=/path/to/agent.exe
CARGO ?= cargo
RUSTUP ?= rustup
WINDOWS_AGENT ?=
AGENT_TARGET := x86_64-unknown-linux-musl
ASSETS := crates/guac-bridge/assets
.PHONY: all build agent check test integration package help
all: build
help:
	@echo 'make build       Build static Linux helper and native clients'
	@echo 'make check       Build, format-check, lint and Rust tests'
	@echo 'make integration Run local PTY tests (Python + aiohttp required)'
	@echo 'make package     Create a portable Linux archive'
	@echo 'Windows: ./build.ps1 [-Check] [-Installer]'
agent:
	$(RUSTUP) target add $(AGENT_TARGET)
	$(CARGO) build --locked --release --no-default-features --features agent --bin guac-bridge-agent --target $(AGENT_TARGET) --config 'target.$(AGENT_TARGET).linker="rust-lld"'
	mkdir -p $(ASSETS)
	cp target/$(AGENT_TARGET)/release/guac-bridge-agent $(ASSETS)/guac-bridge-agent-linux-x86_64
	$(if $(WINDOWS_AGENT),cp "$(WINDOWS_AGENT)" $(ASSETS)/guac-bridge-agent-windows-x86_64.exe,:)
build: agent
	$(CARGO) build --locked --release --bins
check: build
	$(CARGO) fmt --all -- --check
	$(CARGO) clippy --locked --all-targets -- -D warnings
	$(CARGO) test --locked
test: check
integration: build
	python -m unittest discover -s tests/native -p test_native.py -v
package: build
	mkdir -p dist
	tar -czf dist/guac-bridge-linux-x86_64.tar.gz LICENSE README.md -C target/release guac-bridge
