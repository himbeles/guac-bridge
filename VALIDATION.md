Validation: 2026-10-30

- Python 3.12.14 on Linux; aiohttp 3.14.4.
- Eight unittest tests passed against a simulated Guacamole gateway and a mock
  vLLM endpoint, with the real server helper running in a Bash PTY.
- Built universal wheel and source distribution with uv build.
- Installed the wheel using uv pip in a separate venv with existing runtime
  dependencies; both generated console commands passed --help smoke checks.
- An offline uv tool install could not resolve aiohttp because its wheel is not
  cached in the build environment. Normal installation requires dependency
  access through PyPI, your corporate index, or a prepared wheelhouse.
- No connection or login was attempted against the internal corporate jump host.
- Windows/macOS execution and a real Guacamole server remain unverified.
