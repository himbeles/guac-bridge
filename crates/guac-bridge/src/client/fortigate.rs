//! FortiGate portal password login and hterm custom SSH sessions.
//! Neither HTTP error bodies nor credential-bearing URLs are included in errors.
use super::guacamole::Session;
use crate::options::BridgeOptions;
use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use futures_util::SinkExt;
use reqwest::cookie::CookieStore;
use std::{sync::Arc, time::Duration};
use tokio_tungstenite::{
    Connector,
    tungstenite::{Message, client::IntoClientRequest},
};
use url::Url;

pub(super) fn base(args: &BridgeOptions) -> Result<Url> {
    let mut url = Url::parse(args.url.as_deref().context("Gateway URL is required")?)?;
    ensure!(
        ["http", "https"].contains(&url.scheme())
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
            && url.query().is_none()
            && url.fragment().is_none(),
        "Supply a FortiGate HTTP(S) portal URL without credentials, query or fragment"
    );
    ensure!(
        ["", "/", "/sslvpn/portal/index.html", "/remote/login"].contains(&url.path()),
        "Supply the FortiGate portal origin or login URL"
    );
    url.set_path("/");
    Ok(url)
}

// hterm uses JavaScript string.length, not UTF-8 bytes or Unicode scalar count.
pub(super) fn instruction(op: &str, value: &str) -> Message {
    Message::Text(
        format!(
            "{}.{op},{}.{value};",
            op.len(),
            value.encode_utf16().count()
        )
        .into(),
    )
}
async fn text(mut response: reqwest::Response, limit: usize) -> Result<String> {
    ensure!(
        response.status().is_success(),
        "FortiGate request rejected (HTTP {})",
        response.status()
    );
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|e| e.without_url())? {
        ensure!(
            bytes.len() + chunk.len() <= limit,
            "FortiGate response exceeds size limit"
        );
        bytes.extend_from_slice(&chunk);
    }
    String::from_utf8(bytes).context("Invalid FortiGate text response")
}
async fn get(http: &reqwest::Client, url: Url, limit: usize) -> Result<String> {
    text(
        http.get(url).send().await.map_err(|e| e.without_url())?,
        limit,
    )
    .await
}
fn login_redirect(body: &str, base: &Url) -> Result<Url> {
    let body: String = body.lines().map(str::trim).collect();
    let redirect = body.strip_prefix("ret=1,redir=").context(
        "FortiGate password login rejected or requires an unsupported MFA/SSO challenge",
    )?;
    let url = base
        .join(redirect)
        .context("Invalid FortiGate login redirect")?;
    ensure!(
        url.origin() == base.origin()
            && ["/remote/hostcheck_install", "/sslvpn/portal/index.html"].contains(&url.path()),
        "FortiGate login requires an unsupported redirect or compliance challenge"
    );
    Ok(url)
}
pub(super) async fn connect(args: &BridgeOptions) -> Result<Session> {
    let base = base(args)?;
    let username = crate::credentials::username(args)?;
    let password = crate::credentials::password(args, &username)?;
    let server_user = crate::credentials::server_username(args)?;
    let server_password = crate::credentials::server_password(args, &server_user)?;
    let jar = Arc::new(reqwest::cookie::Jar::default());
    let (http, tls) = super::tls::clients(args, Some(jar.clone()))?;
    let form: String = url::form_urlencoded::Serializer::new(String::new())
        .extend_pairs([
            ("ajax", "1"),
            ("username", username.as_str()),
            ("credential", password.as_str()),
        ])
        .finish();
    let response = http
        .post(base.join("remote/logincheck")?)
        .header("Content-Type", "text/plain;charset=UTF-8")
        .header("Origin", base.origin().ascii_serialization())
        .body(form)
        .send()
        .await
        .map_err(|e| e.without_url())?;
    let redirect = login_redirect(&text(response, 65536).await?, &base)?;
    let hostcheck = redirect.path() == "/remote/hostcheck_install";
    let page = get(&http, redirect, 65536).await?;
    if hostcheck {
        ensure!(
            page.len() < 1024
                && page.contains("document.location=")
                && page.contains("/sslvpn/portal/index.html"),
            "FortiGate endpoint compliance challenge requires interactive authentication"
        );
        get(&http, base.join("sslvpn/portal/index.html")?, 1_048_576).await?;
    }
    let portal: serde_json::Value =
        serde_json::from_str(&get(&http, base.join("remote/portal")?, 1_048_576).await?)
            .context("FortiGate portal did not return authenticated session metadata")?;
    ensure!(
        portal["user"].as_str().is_some_and(|s| !s.is_empty())
            && portal["fgt_sslvpn_sid"]
                .as_str()
                .is_some_and(|s| !s.is_empty()),
        "FortiGate session is not authenticated"
    );
    let mut ws_url = base.join("remote/rds")?;
    // Cookie selection uses the original HTTPS URL so Secure/path scoping is preserved.
    let cookies = jar
        .cookies(&ws_url)
        .context("FortiGate login supplied no session cookie for the terminal endpoint")?;
    ws_url
        .set_scheme(if base.scheme() == "https" {
            "wss"
        } else {
            "ws"
        })
        .unwrap();
    let connection = serde_json::json!({"name":"", "apptype":"ssh", "host":args.server_host, "port":args.server_port.to_string()});
    ws_url
        .query_pairs_mut()
        .append_pair("connection", &connection.to_string());
    let mut request = ws_url.as_str().into_client_request()?;
    request
        .headers_mut()
        .insert("Sec-WebSocket-Protocol", "hterm".parse()?);
    request
        .headers_mut()
        .insert("Origin", base.origin().ascii_serialization().parse()?);
    request.headers_mut().insert("Cookie", cookies);
    let (mut socket, response) =
        tokio::time::timeout(Duration::from_secs(args.connect_timeout), async {
            let stream = crate::proxy::connect(&ws_url).await?;
            tokio_tungstenite::client_async_tls_with_config(
                request,
                stream,
                None,
                Some(Connector::Rustls(Arc::new(tls))),
            )
            .await
            .map_err(|_| anyhow::anyhow!("FortiGate terminal handshake failed"))
        })
        .await
        .context("FortiGate terminal connection timed out")??;
    ensure!(
        response
            .headers()
            .get("Sec-WebSocket-Protocol")
            .is_some_and(|v| v == "hterm"),
        "FortiGate did not negotiate hterm"
    );
    for message in [
        instruction("resize", "120:40"),
        instruction("user", &server_user),
        instruction("passwd", &server_password),
        instruction("start", ""),
    ] {
        tokio::time::timeout(
            Duration::from_secs(args.connect_timeout),
            socket.send(message),
        )
        .await
        .context("FortiGate SSH initialization timed out")?
        .map_err(|_| anyhow::anyhow!("FortiGate SSH initialization failed"))?;
    }
    if args.save_password {
        crate::credentials::save(args, &username, &password)?;
    }
    Ok(Session {
        socket,
        http,
        token: String::new(),
        username,
        server_secret: Some((server_user, server_password)),
    })
}
pub(super) async fn refresh(http: &reqwest::Client, args: &BridgeOptions) -> Result<()> {
    let mut url = base(args)?.join("remote/portal")?;
    url.query_pairs_mut().append_pair("access", "keepalive");
    let response = http
        .get(url)
        .timeout(Duration::from_secs(15))
        .send()
        .await
        .map_err(|e| e.without_url())?;
    ensure!(
        response.status().is_success() && !response.status().is_redirection(),
        "FortiGate session renewal rejected; reconnect and authenticate"
    );
    // A redirect or login page must not be treated as successful renewal.
    let result = text(response, 1_048_576).await?;
    ensure!(
        !result.trim_start().starts_with('<'),
        "FortiGate session expired; reconnect and authenticate"
    );
    Ok(())
}

pub(super) enum Output {
    Bootstrap(Vec<u8>),
    Data(Vec<u8>),
    InputAck(Vec<u8>),
    Corrupt,
    Legacy,
    Fatal(Vec<u8>),
    FatalEnd,
}
#[derive(Default)]
pub(super) struct Decoder {
    pending: Vec<u8>,
}
impl Decoder {
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Vec<Output>> {
        const PREFIX: &[u8] = b"\x1b]482202;2;";
        let mut events = Vec::new();
        // Incremental scanning bounds retained terminal noise even for large WebSocket messages.
        for &byte in bytes {
            if byte == 27 {
                self.pending.clear();
            }
            self.pending.push(byte);
            if !self.pending.starts_with(PREFIX) {
                while !PREFIX.starts_with(&self.pending) && !self.pending.starts_with(PREFIX) {
                    self.pending.remove(0);
                }
                continue;
            }
            if self.pending.len() > 4096 {
                self.pending.clear();
                events.push(Output::Corrupt);
                continue;
            }
            if byte != 7 {
                continue;
            }
            let payload = &self.pending[PREFIX.len()..self.pending.len() - 1];
            if payload == b"GBERR:END" {
                events.push(Output::FatalEnd);
            } else {
                for (prefix, kind) in [
                    (b"GBWB:".as_slice(), 0),
                    (b"GBR1:".as_slice(), 1),
                    (b"GBIA:".as_slice(), 3),
                    (b"GBW1:".as_slice(), 4),
                    (b"GBERR:".as_slice(), 2),
                ] {
                    if let Some(encoded) = payload.strip_prefix(prefix) {
                        if kind == 4 {
                            events.push(Output::Legacy);
                            break;
                        }
                        match STANDARD.decode(encoded) {
                            Ok(data) => events.push(match kind {
                                0 => Output::Bootstrap(data),
                                1 => Output::Data(data),
                                3 => Output::InputAck(data),
                                _ => Output::Fatal(data),
                            }),
                            Err(_) if kind == 1 || kind == 3 => events.push(Output::Corrupt),
                            Err(_) => {
                                anyhow::bail!("Invalid FortiGate bootstrap/fatal carrier encoding")
                            }
                        }
                        break;
                    }
                }
            }
            self.pending.clear();
        }
        Ok(events)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn login_rejects_challenges_and_cross_origin_redirects() {
        let base = Url::parse("https://gateway.example/").unwrap();
        for value in [
            "ret=0",
            "ret=2",
            "ret=1,redir=https://other.example/remote/hostcheck_install",
            "ret=1,redir=/remote/mfa",
        ] {
            assert!(login_redirect(value, &base).is_err());
        }
        assert_eq!(
            login_redirect("ret=1,redir=/remote/hostcheck_install?auth_type=1", &base)
                .unwrap()
                .path(),
            "/remote/hostcheck_install"
        );
    }
    #[tokio::test]
    async fn renewal_rejects_expiry_redirects_and_login_pages() {
        use clap::Parser;
        use tokio::{
            io::{AsyncReadExt, AsyncWriteExt},
            net::TcpListener,
        };
        #[derive(Parser)]
        struct Config {
            #[command(flatten)]
            args: BridgeOptions,
        }
        for (status, body, rejected) in [
            (200, "{\"user\":\"fixture\"}", false),
            (401, "", true),
            (302, "", true),
            (200, "<html>login</html>", true),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let config = Config::try_parse_from(["test", &url]).unwrap();
            let server = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = [0u8; 4096];
                let size = stream.read(&mut request).await.unwrap();
                assert!(String::from_utf8_lossy(&request[..size]).contains("access=keepalive"));
                stream.write_all(format!("HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).as_bytes()).await.unwrap();
            });
            let client = reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .build()
                .unwrap();
            assert_eq!(refresh(&client, &config.args).await.is_err(), rejected);
            server.await.unwrap();
        }
    }
    #[test]
    fn hterm_lengths_use_utf16() {
        assert_eq!(instruction("str", "😀").into_text().unwrap(), "3.str,2.😀;");
    }
    #[test]
    fn osc_survives_fragmentation_noise_and_bounds_incomplete_messages() {
        let wire = crate::terminal_wire::osc_bytes("GBR1:", &[0, 255, 27, 29]);
        for split in 0..=wire.len() {
            let mut decoder = Decoder::default();
            decoder.feed(b"PowerShell prompt\r\n").unwrap();
            let mut events = decoder.feed(&wire[..split]).unwrap();
            events.extend(decoder.feed(&wire[split..]).unwrap());
            assert_eq!(events.len(), 1);
            assert!(matches!(&events[0],Output::Data(data) if data==&[0,255,27,29]));
        }
        let events = Decoder::default()
            .feed(&[b"\x1b]482202;2;".as_slice(), &vec![b'x'; 4096]].concat())
            .unwrap();
        assert!(events.iter().any(|event| matches!(event, Output::Corrupt)));
    }
}
