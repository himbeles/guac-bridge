//! Proxy selection for WebSockets, matching the standard HTTP(S)_PROXY and NO_PROXY environment.
use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};
use url::Url;
fn variable(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .or_else(|| std::env::var(name.to_lowercase()).ok())
        .filter(|s| !s.is_empty())
}
fn bypass(host: &str, port: u16, list: &str) -> bool {
    list.split(',').map(str::trim).any(|entry| {
        if entry == "*" {
            return true;
        }
        let (entry, p) = entry
            .rsplit_once(':')
            .filter(|(_, p)| p.parse::<u16>().is_ok())
            .map_or((entry, None), |(h, p)| (h, p.parse::<u16>().ok()));
        if p.is_some_and(|p| p != port) {
            return false;
        }
        let suffix = entry.trim_start_matches('.').to_lowercase();
        let host = host.to_lowercase();
        !suffix.is_empty() && (host == suffix || host.ends_with(&format!(".{suffix}")))
    })
}
pub(crate) async fn connect(url: &Url) -> Result<TcpStream> {
    let host = url.host_str().context("WebSocket URL has no host")?;
    let port = url
        .port_or_known_default()
        .context("WebSocket URL has no port")?;
    let proxy = if variable("NO_PROXY").is_some_and(|s| bypass(host, port, &s)) {
        None
    } else {
        variable(if url.scheme() == "wss" {
            "HTTPS_PROXY"
        } else {
            "HTTP_PROXY"
        })
        .or_else(|| variable("ALL_PROXY"))
    };
    let stream = if let Some(proxy) = proxy {
        let proxy = Url::parse(&proxy).context("Invalid proxy URL")?;
        ensure!(
            proxy.scheme() == "http",
            "WebSocket proxy must use an http:// CONNECT proxy"
        );
        let mut stream = TcpStream::connect((
            proxy.host_str().context("Proxy has no host")?,
            proxy.port_or_known_default().unwrap_or(80),
        ))
        .await
        .context("Cannot connect to proxy")?;
        let host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host.to_owned()
        };
        let mut request = format!("CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n");
        if !proxy.username().is_empty() {
            let username = percent_encoding::percent_decode_str(proxy.username()).decode_utf8()?;
            let password = percent_encoding::percent_decode_str(proxy.password().unwrap_or(""))
                .decode_utf8()?;
            request.push_str(&format!(
                "Proxy-Authorization: Basic {}\r\n",
                STANDARD.encode(format!("{username}:{password}"))
            ));
        }
        request.push_str("\r\n");
        stream.write_all(request.as_bytes()).await?;
        let mut headers = Vec::new();
        let mut byte = [0];
        while !headers.ends_with(b"\r\n\r\n") {
            ensure!(headers.len() < 16384, "Proxy response too large");
            stream.read_exact(&mut byte).await?;
            headers.push(byte[0]);
        }
        let status = std::str::from_utf8(&headers)?
            .lines()
            .next()
            .unwrap_or("")
            .split_whitespace()
            .nth(1)
            .unwrap_or("");
        ensure!(status == "200", "Proxy CONNECT rejected (HTTP {status})");
        stream
    } else {
        TcpStream::connect((host, port))
            .await
            .context("Cannot connect to Guacamole WebSocket")?
    };
    stream.set_nodelay(true)?;
    Ok(stream)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exclusions() {
        assert!(bypass("a.example.org", 443, "localhost,.example.org"));
        assert!(!bypass("badexample.org", 443, "example.org"));
        assert!(!bypass("example.org", 80, "example.org:443"));
        assert!(bypass("127.0.0.1", 80, "127.0.0.1,localhost"));
    }
}
