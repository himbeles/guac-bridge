//! Guacamole URL interpretation, login/TLS and session refresh.
//! Authentication secrets stay in this module's session, never its debug output.
use crate::options::BridgeOptions;
use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{
    Engine,
    engine::general_purpose::{STANDARD as B64, STANDARD_NO_PAD},
};
use std::{sync::Arc, time::Duration};
use tokio::net::TcpStream;
use tokio_tungstenite::{
    Connector, MaybeTlsStream, WebSocketStream, tungstenite::client::IntoClientRequest,
};
use url::Url;

pub(super) struct Destination {
    base: Url,
    connection: String,
    kind: String,
    source: String,
}
pub(super) struct Session {
    pub socket: WebSocketStream<MaybeTlsStream<TcpStream>>,
    pub http: reqwest::Client,
    pub token: String,
    pub username: String,
}
impl Destination {
    pub(super) fn parse(args: &BridgeOptions) -> Result<Self> {
        let mut base = Url::parse(args.url.as_deref().context("URL is required")?)?;
        ensure!(
            ["http", "https"].contains(&base.scheme())
                && base.host_str().is_some()
                && base.username().is_empty()
                && base.password().is_none(),
            "Supply an HTTP(S) Guacamole URL without embedded credentials"
        );
        let mut connection = args.connection_id.clone();
        let mut source = args.data_source.clone();
        let mut kind = "c".to_owned();
        if let Some((_, encoded)) = base.fragment().and_then(|text| text.split_once("/client/")) {
            let text = percent_encoding::percent_decode_str(encoded.split('?').next().unwrap())
                .decode_utf8()?;
            let decoded = B64
                .decode(text.as_bytes())
                .or_else(|_| STANDARD_NO_PAD.decode(text.as_bytes()))?;
            let decoded = String::from_utf8(decoded)?;
            let values: Vec<_> = decoded.split('\0').collect();
            ensure!(values.len() == 3, "Invalid Guacamole client URL");
            connection = connection.or_else(|| Some(values[0].into()));
            kind = values[1].into();
            source = source.or_else(|| Some(values[2].into()));
        }
        ensure!(kind == "c" || kind == "g", "Invalid connection type");
        base.set_fragment(None);
        base.set_query(None);
        base.set_path(&(base.path().trim_end_matches('/').to_owned() + "/"));
        Ok(Self {
            base,
            connection: connection.context("URL needs #/client/... or --connection-id")?,
            kind,
            source: source.context("URL needs a data source")?,
        })
    }
    pub(super) async fn connect(&self, args: &BridgeOptions) -> Result<Session> {
        let Self {
            base,
            connection,
            kind,
            source,
        } = self;
        let username = crate::credentials::username(args)?;
        let password = crate::credentials::password(args, &username)?;
        let mut http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(args.connect_timeout))
            .timeout(Duration::from_secs(args.connect_timeout));
        let mut roots = rustls::RootCertStore::empty();
        for cert in rustls_native_certs::load_native_certs().certs {
            let _ = roots.add(cert);
        }
        if let Some(path) = &args.ca_file {
            let pem = std::fs::read(path)?;
            for cert in rustls_pemfile::certs(&mut &pem[..]) {
                roots.add(cert?)?;
            }
            for cert in reqwest::Certificate::from_pem_bundle(&pem)? {
                http = http.add_root_certificate(cert);
            }
        }
        if args.insecure {
            eprintln!("\x1b[33mTLS verification disabled by --insecure\x1b[0m");
            http = http.danger_accept_invalid_certs(true);
        }
        let http = http.build()?;
        let response = http
            .post(base.join("api/tokens")?)
            .form(&[
                ("username", username.as_str()),
                ("password", password.as_str()),
            ])
            .send()
            .await
            .map_err(|error| error.without_url())
            .context("Guacamole login request failed")?;
        ensure!(
            response.status().is_success(),
            "Guacamole login rejected (HTTP {})",
            response.status()
        );
        let login: serde_json::Value = response
            .json()
            .await
            .context("Invalid Guacamole login response")?;
        let token = login["authToken"]
            .as_str()
            .context("No authentication token; this login requires an external SSO/MFA flow")?
            .to_owned();
        if args.save_password {
            crate::credentials::save(args, &username, &password)?;
        }
        let mut ws_url = base.join("websocket-tunnel")?;
        ws_url
            .set_scheme(if base.scheme() == "https" {
                "wss"
            } else {
                "ws"
            })
            .unwrap();
        ws_url.query_pairs_mut().extend_pairs([
            ("token", token.as_str()),
            ("GUAC_DATA_SOURCE", source.as_str()),
            ("GUAC_ID", connection.as_str()),
            ("GUAC_TYPE", kind.as_str()),
            ("GUAC_WIDTH", "1024"),
            ("GUAC_HEIGHT", "768"),
            ("GUAC_DPI", "96"),
            ("GUAC_TIMEZONE", "Europe/Berlin"),
            ("GUAC_IMAGE", "image/png"),
        ]);
        let mut request = ws_url.as_str().into_client_request()?;
        request
            .headers_mut()
            .insert("Sec-WebSocket-Protocol", "guacamole".parse()?);
        request
            .headers_mut()
            .insert("Origin", base.origin().ascii_serialization().parse()?);
        let mut tls = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        if args.insecure {
            tls.dangerous()
                .set_certificate_verifier(Arc::new(InsecureVerifier));
        }
        let (socket, _) = tokio::time::timeout(Duration::from_secs(args.connect_timeout), async {
            let stream = crate::proxy::connect(&ws_url).await?;
            tokio_tungstenite::client_async_tls_with_config(
                request,
                stream,
                None,
                Some(Connector::Rustls(Arc::new(tls))),
            )
            .await
            .map_err(|_| {
                anyhow!("Guacamole WebSocket connection failed; check proxy and certificate trust")
            })
        })
        .await?
        .map_err(|_| anyhow!("Guacamole WebSocket connection failed"))?;
        Ok(Session {
            socket,
            http,
            token,
            username,
        })
    }

    pub(super) fn is_connection(&self) -> bool {
        self.kind == "c"
    }
    pub(super) fn refresh_url(&self) -> Result<Url> {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|_| anyhow!("Invalid refresh URL"))?
            .pop_if_empty()
            .extend([
                "api",
                "session",
                "data",
                &self.source,
                "connections",
                &self.connection,
            ]);
        Ok(url)
    }
}
// Refresh failures used to terminate healthy forwarding every 60 seconds.
// Keep servicing local sockets while HTTP refresh is pending; the independent
// WebSocket/remote-heartbeat checks still detect an actual disconnected session.
pub(super) async fn refresh_once(http: &reqwest::Client, url: Url, token: &str) -> Result<()> {
    match http
        .get(url)
        .query(&[("token", token)])
        .timeout(Duration::from_secs(15))
        .send()
        .await
    {
        Ok(response) if response.status().as_u16() == 401 || response.status().as_u16() == 403 => {
            bail!(
                "Guacamole session refresh rejected (HTTP {}); reconnect and authenticate",
                response.status()
            )
        }
        Ok(response) if !response.status().is_success() => {
            eprintln!(
                "Session refresh returned HTTP {}; keeping the active tunnel and retrying in 60 seconds",
                response.status()
            );
        }
        Err(error) => {
            eprintln!(
                "Session refresh failed: {:#}; keeping the active tunnel and retrying in 60 seconds",
                error.without_url()
            );
        }
        _ => (),
    }
    Ok(())
}

#[cfg(test)]
mod refresh_tests {
    use super::*;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };
    #[test]
    fn destination_preserves_app_path_and_decodes_named_connection_fields() {
        use clap::Parser;
        #[derive(Parser)]
        struct Config {
            #[command(flatten)]
            options: BridgeOptions,
        }
        let config = Config::try_parse_from([
            "test",
            "https://guac.example/guacamole/?token=private#/client/MQBjAG15c3Fs",
        ])
        .unwrap();
        let destination = Destination::parse(&config.options).unwrap();
        assert_eq!(destination.base.as_str(), "https://guac.example/guacamole/");
        assert_eq!(destination.connection, "1");
        assert_eq!(destination.source, "mysql");
        assert!(destination.is_connection());
        assert_eq!(
            destination.refresh_url().unwrap().as_str(),
            "https://guac.example/guacamole/api/session/data/mysql/connections/1"
        );
        let config = Config::try_parse_from([
            "test",
            "https://guac.example/guacamole/#/client/MQBjAG15c3Fs",
            "--connection-id",
            "chosen/id",
            "--data-source",
            "other",
        ])
        .unwrap();
        let destination = Destination::parse(&config.options).unwrap();
        assert!(
            destination
                .refresh_url()
                .unwrap()
                .as_str()
                .ends_with("/other/connections/chosen%2Fid")
        );
        for value in [
            "file:///private",
            "https://alice:secret@guac.example/#/client/MQBjAG15c3Fs",
            "https://guac.example/#/client/invalid",
        ] {
            let config = Config::try_parse_from(["test", value]).unwrap();
            assert!(Destination::parse(&config.options).is_err());
        }
    }
    #[tokio::test]
    async fn refresh_transient_failure_then_success_and_auth_rejection() {
        let http = reqwest::Client::builder().no_proxy().build().unwrap();
        for (status, fatal) in [(503, false), (200, false), (401, true), (403, true)] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = Url::parse(&format!(
                "http://{}/refresh",
                listener.local_addr().unwrap()
            ))
            .unwrap();
            let server = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut bytes = [0; 4096];
                let received = stream.read(&mut bytes).await.unwrap();
                assert!(received > 0);
                stream.write_all(format!("HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").as_bytes()).await.unwrap();
            });
            assert_eq!(
                refresh_once(&http, url, "synthetic-token").await.is_err(),
                fatal
            );
            server.await.unwrap();
        }
    }
}

#[derive(Debug)]
struct InsecureVerifier;
impl rustls::client::danger::ServerCertVerifier for InsecureVerifier {
    fn verify_server_cert(
        &self,
        _: &rustls::pki_types::CertificateDer<'_>,
        _: &[rustls::pki_types::CertificateDer<'_>],
        _: &rustls::pki_types::ServerName<'_>,
        _: &[u8],
        _: rustls::pki_types::UnixTime,
    ) -> std::result::Result<rustls::client::danger::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::danger::ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        m: &[u8],
        c: &rustls::pki_types::CertificateDer<'_>,
        s: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(
            m,
            c,
            s,
            &rustls::crypto::ring::default_provider().signature_verification_algorithms,
        )
    }
    fn verify_tls13_signature(
        &self,
        m: &[u8],
        c: &rustls::pki_types::CertificateDer<'_>,
        s: &rustls::DigitallySignedStruct,
    ) -> std::result::Result<rustls::client::danger::HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(
            m,
            c,
            s,
            &rustls::crypto::ring::default_provider().signature_verification_algorithms,
        )
    }
    fn supported_verify_schemes(&self) -> Vec<rustls::SignatureScheme> {
        rustls::crypto::ring::default_provider()
            .signature_verification_algorithms
            .supported_schemes()
    }
}
