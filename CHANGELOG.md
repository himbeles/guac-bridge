# Changelog

## 0.6.0

- Speed up bulk SSH transfers and LLM responses with larger flow-control windows and SSH frames.
- Negotiate faster settings automatically, retaining compatible limits for older clients and helpers.
- Document connection tests, benchmark results, and reproduction steps.

## 0.5.2

- Warn on server version mismatches by default; add `--require-server-version` for strict checks.

## 0.5.1

- Add `--ignore-server-version` for temporary upgrade connections, including older helpers.

## 0.5.0

- Faster SSH transfers and LLM streaming with bounded ACK pipelining.
- Require matching client/server versions, with clear installation instructions on mismatch.
- Improve cancellation, shutdown, and local port-conflict diagnostics.
- Add benchmarking tools and documentation.
