//! HTTPS for every outbound call (Worker, watchdog, Discord, GitHub, Mojang): ureq over rustls with ring,
//! verified against the **system** CA bundle (what Python's `urllib`/`ssl.create_default_context()` use:
//! `$SSL_CERT_FILE`, else Debian's /etc/ssl/certs/ca-certificates.crt), proxies from the environment as urllib.
//!
//! Documented divergence: **redirects are refused** (a 3xx is an `HTTPError` with that status). urllib follows
//! them, turning a POST into a GET and keeping `Authorization` across hosts; no endpoint the agent calls
//! redirects. Errors are reported by the Python class urllib would raise, which is all the callers print:
//! `HTTPError` (a status >= 300), `TimeoutError` (the timeout), `URLError` (DNS, connection, TLS), `ValueError`
//! (a URL the caller refuses).

use rustls_pki_types::CertificateDer;
use rustls_pki_types::pem::PemObject;
use std::io::Read;
use std::sync::OnceLock;
use std::time::Duration;

/// mc_agent.py `request`'s reply cap (`resp.read(1 << 20)`): a longer reply is cut there.
pub const MAX_REPLY: u64 = 1 << 20;
/// Debian's CA bundle (the `ca-certificates` package), OpenSSL's default file there.
pub const CA_BUNDLE: &str = "/etc/ssl/certs/ca-certificates.crt";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HttpError {
    /// `urllib.error.HTTPError` (status, the start of the body).
    Status(u16, Vec<u8>),
    /// Any other failure, by the Python class urllib would raise (`URLError`, `TimeoutError`, `ValueError`).
    Transport(&'static str),
}

impl HttpError {
    /// `type(e).__name__`.
    pub fn class_name(&self) -> &'static str {
        match self {
            HttpError::Status(..) => "HTTPError",
            HttpError::Transport(c) => c,
        }
    }

    pub fn status(&self) -> Option<u16> {
        match self {
            HttpError::Status(s, _) => Some(*s),
            HttpError::Transport(_) => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

/// The system roots, read once (an unreadable bundle leaves none: every HTTPS request then fails like an
/// unverifiable certificate does in Python).
fn roots() -> &'static [ureq::tls::Certificate<'static>] {
    static ROOTS: OnceLock<Vec<ureq::tls::Certificate<'static>>> = OnceLock::new();
    ROOTS.get_or_init(|| {
        let file = std::env::var("SSL_CERT_FILE")
            .ok()
            .filter(|f| !f.is_empty());
        let data = std::fs::read(file.as_deref().unwrap_or(CA_BUNDLE)).unwrap_or_default();
        CertificateDer::pem_slice_iter(&data)
            .filter_map(Result::ok)
            .map(|der| ureq::tls::Certificate::from_der(der.as_ref()).to_owned())
            .collect()
    })
}

fn agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| {
        // ureq's `rustls-no-provider` uses the process default: ring (never aws-lc-rs).
        let _ = rustls::crypto::ring::default_provider().install_default();
        let tls = ureq::tls::TlsConfig::builder()
            .provider(ureq::tls::TlsProvider::Rustls)
            .root_certs(ureq::tls::RootCerts::new_with_certs(roots()))
            .build();
        ureq::Agent::config_builder()
            .tls_config(tls)
            .max_redirects(0)
            .http_status_as_error(false)
            .build()
            .into()
    })
}

fn transport(e: &ureq::Error) -> HttpError {
    match e {
        ureq::Error::Timeout(_) => HttpError::Transport("TimeoutError"),
        ureq::Error::BadUri(_) => HttpError::Transport("ValueError"),
        _ => HttpError::Transport("URLError"),
    }
}

/// One request. `headers` are sent as given (no `User-Agent` is added: pass the caller's, as Python sets it);
/// a reply over `max_reply` bytes is cut. Any status outside 200..300 is `HttpError::Status`.
///
/// # Errors
/// See `HttpError`.
pub fn request(
    method: &str,
    url: &str,
    headers: &[(&str, &str)],
    body: Option<&[u8]>,
    timeout: Duration,
    max_reply: u64,
) -> Result<Response, HttpError> {
    let a = agent();
    macro_rules! with_headers {
        ($rb:expr) => {{
            let mut rb = $rb.config().timeout_global(Some(timeout)).build();
            for (k, v) in headers {
                rb = rb.header(*k, *v);
            }
            rb
        }};
    }
    let result = match (method, body) {
        ("GET", _) => with_headers!(a.get(url)).call(),
        ("DELETE", _) => with_headers!(a.delete(url)).call(),
        ("POST", Some(b)) => with_headers!(a.post(url)).send(b),
        ("POST", None) => with_headers!(a.post(url)).send_empty(),
        ("PUT", Some(b)) => with_headers!(a.put(url)).send(b),
        ("PUT", None) => with_headers!(a.put(url)).send_empty(),
        ("PATCH", Some(b)) => with_headers!(a.patch(url)).send(b),
        ("PATCH", None) => with_headers!(a.patch(url)).send_empty(),
        _ => return Err(HttpError::Transport("ValueError")),
    };
    let resp = result.map_err(|e| transport(&e))?;
    let status = resp.status().as_u16();
    let mut data = Vec::new();
    resp.into_body()
        .into_reader()
        .take(max_reply)
        .read_to_end(&mut data)
        .map_err(|_| HttpError::Transport("TimeoutError"))?;
    if (200..300).contains(&status) {
        Ok(Response { status, body: data })
    } else {
        Err(HttpError::Status(status, data))
    }
}

/// notify.py `_post`: https only (else `ValueError`), `User-Agent: smallworld`, 10 s, the first 4 KiB of the
/// reply.
///
/// # Errors
/// See `HttpError`.
pub fn notify_post(
    url: &str,
    body: &[u8],
    content_type: &str,
    token: Option<&str>,
) -> Result<Vec<u8>, HttpError> {
    if !url.starts_with("https://") {
        return Err(HttpError::Transport("ValueError"));
    }
    let auth = token
        .filter(|t| !t.is_empty())
        .map(|t| format!("Bearer {t}"));
    let mut headers = vec![("Content-Type", content_type), ("User-Agent", "smallworld")];
    if let Some(a) = auth.as_deref() {
        headers.push(("Authorization", a));
    }
    request(
        "POST",
        url,
        &headers,
        Some(body),
        Duration::from_secs(10),
        4096,
    )
    .map(|r| r.body)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::net::TcpListener;

    /// A one-shot HTTP server on loopback; returns the URL and a handle yielding the raw request head + body.
    fn serve(reply: &'static str) -> (String, std::thread::JoinHandle<String>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/x", l.local_addr().unwrap());
        let h = std::thread::spawn(move || {
            let (s, _) = l.accept().unwrap();
            let mut r = BufReader::new(s.try_clone().unwrap());
            let mut head = String::new();
            let mut len = 0usize;
            loop {
                let mut line = String::new();
                r.read_line(&mut line).unwrap();
                if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                    len = v.trim().parse().unwrap();
                }
                head.push_str(&line);
                if line == "\r\n" {
                    break;
                }
            }
            let mut body = vec![0u8; len];
            r.read_exact(&mut body).unwrap();
            (&s).write_all(reply.as_bytes()).unwrap();
            head + &String::from_utf8_lossy(&body)
        });
        (url, h)
    }

    #[test]
    fn posts_and_reads() {
        let (url, h) = serve("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
        let r = request(
            "POST",
            &url,
            &[("User-Agent", "t"), ("Content-Type", "application/json")],
            Some(b"{\"a\":1}"),
            Duration::from_secs(5),
            MAX_REPLY,
        )
        .unwrap();
        assert_eq!(r.body, b"{}");
        let seen = h.join().unwrap();
        assert!(seen.starts_with("POST /x HTTP/1.1\r\n"), "{seen}");
        assert!(
            seen.contains("user-agent: t\r\n") || seen.contains("User-Agent: t\r\n"),
            "{seen}"
        );
        assert!(seen.ends_with("{\"a\":1}"));
    }

    #[test]
    fn redirects_are_refused() {
        let (url, h) = serve(
            "HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );
        let e = request("GET", &url, &[], None, Duration::from_secs(5), MAX_REPLY).unwrap_err();
        assert_eq!(e.status(), Some(302));
        assert_eq!(e.class_name(), "HTTPError");
        h.join().unwrap();
    }

    #[test]
    fn errors_name_python_classes() {
        assert_eq!(
            notify_post("http://x", b"", "text/plain", None)
                .unwrap_err()
                .class_name(),
            "ValueError"
        );
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", l.local_addr().unwrap());
        drop(l);
        assert_eq!(
            request("GET", &url, &[], None, Duration::from_secs(2), 10)
                .unwrap_err()
                .class_name(),
            "URLError"
        );
    }
}
