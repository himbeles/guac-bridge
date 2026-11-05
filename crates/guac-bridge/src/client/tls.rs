//! Shared trust and HTTP configuration for gateway transports.
use crate::options::BridgeOptions;
use anyhow::Result;
use std::{sync::Arc, time::Duration};
pub(super) fn clients(
    args: &BridgeOptions,
    cookies: Option<Arc<reqwest::cookie::Jar>>,
) -> Result<(reqwest::Client, rustls::ClientConfig)> {
    let mut http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(args.connect_timeout))
        .timeout(Duration::from_secs(args.connect_timeout));
    if let Some(jar) = cookies {
        http = http.cookie_provider(jar);
    }
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
    let mut tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    if args.insecure {
        eprintln!("\x1b[33mTLS verification disabled by --insecure\x1b[0m");
        http = http.danger_accept_invalid_certs(true);
        tls.dangerous()
            .set_certificate_verifier(Arc::new(InsecureVerifier));
    }
    Ok((http.build()?, tls))
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
