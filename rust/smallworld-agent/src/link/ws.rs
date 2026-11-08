//! mc_agent.py's WebSocket client (RFC 6455, client side; 1972-2149): `ws_accept`, `encode_frame`, `WebSocket`
//! (connect over TCP or rustls, handshake + accept check, masking, fragmentation, 1 MiB cap, ping/pong, close).
//! Owner: link.
//!
//! Design (PORTING.md "link"): the console tail sends while the session waits in `recv`, and a rustls stream is
//! one state machine, so **one I/O thread owns the stream**. It reads with a short timeout, parses frames (the
//! parser keeps a partial frame across reads, like Python's buffer), answers pings, echoes a close, and hands
//! text messages to the session through a channel. Every sender (the session's hello and results, the tail's
//! batches) pushes ready-made masked frames into the other channel (`WsHandle`, a `WsSender`), which the I/O
//! thread writes between reads. After an error or a close the thread stops reading but still writes what is
//! queued (Python's `close()` sends its close frame after either) until the session shuts it down.

use base64::Engine as _;
use rustls::pki_types::{CertificateDer, ServerName, pem::PemObject};
use sha1::{Digest, Sha1};
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream, ToSocketAddrs};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender, TryRecvError};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// mc_agent.py `WS_GUID`.
pub const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
pub const WS_MAX_MESSAGE: usize = 1 << 20;
pub const WS_RECV_TIMEOUT_S: f64 = 15.0;
pub const WS_PING_S: f64 = 45.0;
pub const WS_DEAD_S: f64 = 120.0;
/// `WebSocket.connect`'s timeout (`socket.create_connection(..., timeout=20.0)`), also the handshake's.
pub const WS_CONNECT_TIMEOUT_S: f64 = 20.0;
/// How long the I/O thread blocks in one read before it looks at the send queue again.
const IO_POLL: Duration = Duration::from_millis(50);

/// Opcodes.
pub const OP_CONT: u8 = 0x0;
pub const OP_TEXT: u8 = 0x1;
pub const OP_BIN: u8 = 0x2;
pub const OP_CLOSE: u8 = 0x8;
pub const OP_PING: u8 = 0x9;
pub const OP_PONG: u8 = 0xA;

/// A failed session or connection: Python's exception class (`WebSocketError`, `ConnectionRefusedError`,
/// `TimeoutError`, `SSLError`, ...) and its text. The control loop logs only the class.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WsError {
    pub class: String,
    pub message: String,
}

impl WsError {
    /// `WebSocketError(message)`.
    pub fn ws(message: impl Into<String>) -> Self {
        Self {
            class: "WebSocketError".into(),
            message: message.into(),
        }
    }

    /// The `OSError` Python's socket/ssl layer raises for this I/O error.
    pub fn io(e: &io::Error) -> Self {
        let class = match e.kind() {
            io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => "TimeoutError",
            io::ErrorKind::InvalidData => "SSLError",
            _ => smallworld_core::errno::io_class(e),
        };
        Self {
            class: class.into(),
            message: e.to_string(),
        }
    }
}

impl std::fmt::Display for WsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

/// `secrets.token_bytes(n)` (ring's system RNG, the one rustls uses).
fn random_bytes<const N: usize>() -> [u8; N] {
    let mut buf = [0u8; N];
    if rustls::crypto::ring::default_provider()
        .secure_random
        .fill(&mut buf)
        .is_err()
    {
        // getrandom failing is not survivable for TLS either; fall back to std's per-process random keys.
        use std::hash::{BuildHasher, Hasher};
        for chunk in buf.chunks_mut(8) {
            let mut h = std::collections::hash_map::RandomState::new().build_hasher();
            h.write_u128(Instant::now().elapsed().as_nanos());
            let v = h.finish().to_le_bytes();
            chunk.copy_from_slice(&v[..chunk.len()]);
        }
    }
    buf
}

/// `ws_accept(key)`: base64(sha1(key + GUID)).
pub fn ws_accept(key: &str) -> String {
    let mut h = Sha1::new();
    h.update(key.as_bytes());
    h.update(WS_GUID.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(h.finalize())
}

/// `encode_frame(opcode, payload, mask)`: one final, masked frame (clients must mask, RFC 6455 5.3).
pub fn encode_frame(opcode: u8, payload: &[u8], mask: [u8; 4]) -> Vec<u8> {
    let n = payload.len();
    let mut out = Vec::with_capacity(n + 14);
    out.push(0x80 | opcode);
    if n < 126 {
        out.push(0x80 | u8::try_from(n).unwrap_or(0));
    } else if n < 1 << 16 {
        out.push(0x80 | 0x7E);
        out.extend_from_slice(&u16::try_from(n).unwrap_or(0).to_be_bytes());
    } else {
        out.push(0x80 | 0x7F);
        out.extend_from_slice(&(n as u64).to_be_bytes());
    }
    out.extend_from_slice(&mask);
    out.extend(payload.iter().enumerate().map(|(i, b)| b ^ mask[i % 4]));
    out
}

/// A sender for the session's text messages (the console tail's batches, results).
pub trait WsSender: Send + Sync {
    /// `ws.send_text(text)`; false if the session is gone.
    fn send_text(&self, text: &str) -> bool;
}

enum Out {
    Frame(Vec<u8>),
    Shutdown,
}

enum Incoming {
    Text(String),
    Closed,
    Error(WsError),
}

/// The sending half of a session: frames are masked here and written by the I/O thread, in order.
pub struct WsHandle {
    tx: Sender<Out>,
}

impl WsHandle {
    fn send(&self, opcode: u8, payload: &[u8]) -> bool {
        self.tx
            .send(Out::Frame(encode_frame(opcode, payload, random_bytes())))
            .is_ok()
    }

    /// `ws.ping()`.
    pub fn ping(&self) -> bool {
        self.send(OP_PING, b"")
    }
}

impl WsSender for WsHandle {
    fn send_text(&self, text: &str) -> bool {
        self.send(OP_TEXT, text.as_bytes())
    }
}

/// The stream: plain TCP (ws://, loopback only by `check_url`) or TLS over rustls (wss://).
enum Conn {
    Plain(TcpStream),
    Tls(Box<rustls::StreamOwned<rustls::ClientConnection, TcpStream>>),
}

impl Conn {
    fn tcp(&self) -> &TcpStream {
        match self {
            Conn::Plain(s) => s,
            Conn::Tls(s) => &s.sock,
        }
    }
}

impl Read for Conn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(s) => s.read(buf),
            Conn::Tls(s) => s.read(buf),
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(s) => s.write(buf),
            Conn::Tls(s) => s.write(buf),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        match self {
            Conn::Plain(s) => s.flush(),
            Conn::Tls(s) => s.flush(),
        }
    }
}

/// The system roots as `ssl.create_default_context()` loads them (`$SSL_CERT_FILE`, else Debian's bundle).
fn tls_config() -> Result<Arc<rustls::ClientConfig>, WsError> {
    let file = std::env::var("SSL_CERT_FILE")
        .ok()
        .filter(|f| !f.is_empty());
    let data = std::fs::read(file.as_deref().unwrap_or(crate::http::CA_BUNDLE)).unwrap_or_default();
    let mut roots = rustls::RootCertStore::empty();
    for der in CertificateDer::pem_slice_iter(&data).filter_map(Result::ok) {
        let _ = roots.add(der);
    }
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| WsError {
            class: "SSLError".into(),
            message: e.to_string(),
        })?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(Arc::new(config))
}

/// `urllib.parse.urlsplit` for what `connect` needs: (secure, netloc, host, port, path?query).
fn split_url(url: &str) -> Result<(bool, String, String, u16, String), WsError> {
    let bad = || WsError {
        class: "ValueError".into(),
        message: "bad WebSocket URL".into(),
    };
    let (scheme, rest) = url.split_once("://").ok_or_else(bad)?;
    let scheme = scheme.to_ascii_lowercase();
    let secure = scheme == "https" || scheme == "wss";
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let netloc = rest[..end].to_string();
    let tail = &rest[end..];
    let tail = tail.split('#').next().unwrap_or("");
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    let mut target = if path.is_empty() {
        "/".to_string()
    } else {
        path.to_string()
    };
    if !query.is_empty() {
        target.push('?');
        target.push_str(query);
    }
    let hostport = netloc.rsplit_once('@').map_or(netloc.as_str(), |(_, h)| h);
    let (host, port) = if let Some(inner) = hostport.strip_prefix('[') {
        let (h, after) = inner.split_once(']').ok_or_else(bad)?;
        (h.to_string(), after.strip_prefix(':'))
    } else {
        match hostport.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), Some(p)),
            None => (hostport.to_string(), None),
        }
    };
    let port = match port {
        Some(p) if !p.is_empty() => p.parse::<u16>().map_err(|_| bad())?,
        _ => {
            if secure {
                443
            } else {
                80
            }
        }
    };
    Ok((secure, netloc, host.to_lowercase(), port, target))
}

/// `socket.create_connection((host, port), timeout)`: every resolved address in turn, the last error wins.
fn create_connection(host: &str, port: u16, timeout: Duration) -> Result<TcpStream, WsError> {
    let addrs = (host, port).to_socket_addrs().map_err(|e| WsError {
        class: "gaierror".into(),
        message: e.to_string(),
    })?;
    let mut last = WsError {
        class: "OSError".into(),
        message: "getaddrinfo returns an empty list".into(),
    };
    for addr in addrs {
        match TcpStream::connect_timeout(&addr, timeout) {
            Ok(s) => return Ok(s),
            Err(e) => last = WsError::io(&e),
        }
    }
    Err(last)
}

/// Frame parser over the receive buffer (`WebSocket._parse`): nothing is consumed before a whole frame is there.
#[derive(Default)]
struct Parser {
    buf: Vec<u8>,
}

impl Parser {
    fn parse(&mut self) -> Result<Option<(bool, u8, Vec<u8>)>, WsError> {
        let buf = &self.buf;
        if buf.len() < 2 {
            return Ok(None);
        }
        let (b0, b1) = (buf[0], buf[1]);
        if b0 & 0x70 != 0 {
            return Err(WsError::ws(
                "reserved bits set (no extension was negotiated)",
            ));
        }
        if b1 & 0x80 != 0 {
            return Err(WsError::ws("masked frame from the server (RFC 6455 5.1)"));
        }
        let op = b0 & 0x0F;
        let mut n = u64::from(b1 & 0x7F);
        let mut pos = 2usize;
        if n == 126 {
            if buf.len() < 4 {
                return Ok(None);
            }
            n = u64::from(u16::from_be_bytes([buf[2], buf[3]]));
            pos = 4;
        } else if n == 127 {
            if buf.len() < 10 {
                return Ok(None);
            }
            let mut b = [0u8; 8];
            b.copy_from_slice(&buf[2..10]);
            n = u64::from_be_bytes(b);
            pos = 10;
        }
        if n > WS_MAX_MESSAGE as u64 {
            return Err(WsError::ws("frame too large"));
        }
        if op >= 0x8 && (n > 125 || b0 & 0x80 == 0) {
            return Err(WsError::ws("bad control frame"));
        }
        let n = usize::try_from(n).unwrap_or(usize::MAX);
        if buf.len() < pos + n {
            return Ok(None);
        }
        let data = buf[pos..pos + n].to_vec();
        self.buf.drain(..pos + n);
        Ok(Some((b0 & 0x80 != 0, op, data)))
    }
}

/// The I/O thread's state.
struct Io {
    conn: Conn,
    parser: Parser,
    parts: Option<Vec<u8>>,
    out_rx: Receiver<Out>,
    in_tx: Sender<Incoming>,
    last_rx: Arc<Mutex<Instant>>,
}

enum Step {
    Go,
    /// Stop reading: an error or a close was handed to the session.
    Stop,
}

impl Io {
    fn write_frame(&mut self, frame: &[u8]) -> io::Result<()> {
        self.conn.write_all(frame)?;
        self.conn.flush()
    }

    fn fail(&self, e: WsError) -> Step {
        let _ = self.in_tx.send(Incoming::Error(e));
        Step::Stop
    }

    /// `recv`'s handling of one frame.
    fn frame(&mut self, fin: bool, op: u8, data: Vec<u8>) -> Step {
        *self.last_rx.lock().unwrap_or_else(PoisonError::into_inner) = Instant::now();
        match op {
            OP_PING => {
                if let Err(e) = self.write_frame(&encode_frame(OP_PONG, &data, random_bytes())) {
                    return self.fail(WsError::io(&e));
                }
                return Step::Go;
            }
            OP_PONG => return Step::Go,
            OP_CLOSE => {
                let echo = &data[..data.len().min(2)];
                let _ = self.write_frame(&encode_frame(OP_CLOSE, echo, random_bytes()));
                let _ = self.in_tx.send(Incoming::Closed);
                return Step::Stop;
            }
            OP_TEXT | OP_BIN => {
                if self.parts.is_some() {
                    return self.fail(WsError::ws("new message inside a fragmented one"));
                }
                self.parts = Some(Vec::new());
            }
            OP_CONT => {
                if self.parts.is_none() {
                    return self.fail(WsError::ws("continuation without a message"));
                }
            }
            _ => return self.fail(WsError::ws(format!("unknown opcode {op}"))),
        }
        let parts = self.parts.get_or_insert_with(Vec::new);
        parts.extend_from_slice(&data);
        if parts.len() > WS_MAX_MESSAGE {
            return self.fail(WsError::ws("message too large"));
        }
        if fin {
            let all = self.parts.take().unwrap_or_default();
            let text = String::from_utf8_lossy(&all).into_owned();
            let _ = self.in_tx.send(Incoming::Text(text));
        }
        Step::Go
    }

    /// Writes what is queued; `Err(true)` = shut down, `Err(false)` = a write failed.
    fn drain(&mut self) -> Result<(), bool> {
        loop {
            match self.out_rx.try_recv() {
                Ok(Out::Frame(f)) => {
                    if let Err(e) = self.write_frame(&f) {
                        let _ = self.in_tx.send(Incoming::Error(WsError::io(&e)));
                        return Err(false);
                    }
                }
                Ok(Out::Shutdown) | Err(TryRecvError::Disconnected) => return Err(true),
                Err(TryRecvError::Empty) => return Ok(()),
            }
        }
    }

    fn reading(&mut self) -> Result<Step, bool> {
        let mut chunk = vec![0u8; 65536];
        loop {
            self.drain()?;
            while let Some((fin, op, data)) = match self.parser.parse() {
                Ok(f) => f,
                Err(e) => return Ok(self.fail(e)),
            } {
                if let Step::Stop = self.frame(fin, op, data) {
                    return Ok(Step::Stop);
                }
            }
            match self.conn.read(&mut chunk) {
                Ok(0) => return Ok(self.fail(WsError::ws("connection closed"))),
                Ok(n) => self.parser.buf.extend_from_slice(&chunk[..n]),
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::WouldBlock
                            | io::ErrorKind::TimedOut
                            | io::ErrorKind::Interrupted
                    ) => {}
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
                    return Ok(self.fail(WsError::ws("connection closed")));
                }
                Err(e) => return Ok(self.fail(WsError::io(&e))),
            }
        }
    }

    fn run(mut self) {
        let _ = self.conn.tcp().set_read_timeout(Some(IO_POLL));
        let _ = self
            .conn
            .tcp()
            .set_write_timeout(Some(Duration::from_secs_f64(WS_RECV_TIMEOUT_S)));
        if let Ok(Step::Stop) | Err(false) = self.reading() {
            // Stopped reading: keep writing what the session queues (its close frame) until it shuts down.
            while let Ok(Out::Frame(f)) = self.out_rx.recv() {
                let _ = self.write_frame(&f);
            }
        }
        let _ = self.conn.tcp().shutdown(Shutdown::Both);
    }
}

/// What `recv` can end with besides a message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecvError {
    /// `TimeoutError`: nothing arrived for `WS_RECV_TIMEOUT_S`.
    Timeout,
    Ws(WsError),
}

/// mc_agent.py `WebSocket`: the session's half (receiving, closing) plus its `WsHandle`.
pub struct WebSocket {
    handle: Arc<WsHandle>,
    events: Receiver<Incoming>,
    last_rx: Arc<Mutex<Instant>>,
    io: Option<JoinHandle<()>>,
}

impl WebSocket {
    /// `WebSocket.connect(url, headers, timeout)`: TCP (+ TLS for wss/https), the upgrade request, the 101 and
    /// `Sec-WebSocket-Accept` checks; then the I/O thread takes the stream.
    ///
    /// # Errors
    /// `WsError` (`WebSocketError` for a refused upgrade or a bad accept, an OSError class otherwise).
    pub fn connect(url: &str, headers: &[(&str, &str)], timeout_s: f64) -> Result<Self, WsError> {
        let (secure, netloc, host, port, target) = split_url(url)?;
        let timeout = Duration::from_secs_f64(timeout_s);
        let tcp = create_connection(&host, port, timeout)?;
        tcp.set_read_timeout(Some(timeout))
            .map_err(|e| WsError::io(&e))?;
        tcp.set_write_timeout(Some(timeout))
            .map_err(|e| WsError::io(&e))?;
        let mut conn = if secure {
            let name = ServerName::try_from(host.clone()).map_err(|e| WsError {
                class: "ValueError".into(),
                message: e.to_string(),
            })?;
            let c = rustls::ClientConnection::new(tls_config()?, name).map_err(|e| WsError {
                class: "SSLError".into(),
                message: e.to_string(),
            })?;
            Conn::Tls(Box::new(rustls::StreamOwned::new(c, tcp)))
        } else {
            Conn::Plain(tcp)
        };
        let key = base64::engine::general_purpose::STANDARD.encode(random_bytes::<16>());
        let mut lines = vec![
            format!("GET {target} HTTP/1.1"),
            format!("Host: {netloc}"),
            "Upgrade: websocket".into(),
            "Connection: Upgrade".into(),
            format!("Sec-WebSocket-Key: {key}"),
            "Sec-WebSocket-Version: 13".into(),
        ];
        lines.extend(headers.iter().map(|(k, v)| format!("{k}: {v}")));
        let request = lines.join("\r\n") + "\r\n\r\n";
        conn.write_all(request.as_bytes())
            .and_then(|()| conn.flush())
            .map_err(|e| WsError::io(&e))?;
        // _read_until(b"\r\n\r\n", 16384)
        let mut buf: Vec<u8> = Vec::new();
        let mut chunk = [0u8; 4096];
        let head_end = loop {
            if let Some(i) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                break i;
            }
            if buf.len() > 16384 {
                return Err(WsError::ws("response header too large"));
            }
            match conn.read(&mut chunk) {
                Ok(0) => return Err(WsError::ws("connection closed during the handshake")),
                Ok(n) => buf.extend_from_slice(&chunk[..n]),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => {
                    return Err(WsError::ws("connection closed during the handshake"));
                }
                Err(e) => return Err(WsError::io(&e)),
            }
        };
        // latin-1 decode: every byte is one code point
        let head: String = buf[..head_end].iter().map(|&b| char::from(b)).collect();
        let rest = buf[head_end + 4..].to_vec();
        let mut fields = head.split("\r\n");
        let status = fields.next().unwrap_or("");
        let ok = status.strip_prefix("HTTP/1.1 101").is_some_and(|r| {
            r.chars()
                .next()
                .is_none_or(|c| !(c.is_alphanumeric() || c == '_'))
        });
        if !ok {
            let shown: String = status.chars().take(40).collect();
            return Err(WsError::ws(format!("upgrade refused: {shown}")));
        }
        let mut accept = None;
        for f in fields {
            let (k, v) = f.split_once(':').unwrap_or((f, ""));
            if smallworld_core::pystr::strip_ws(k).to_lowercase() == "sec-websocket-accept" {
                accept = Some(smallworld_core::pystr::strip_ws(v).to_string());
            }
        }
        if accept.as_deref() != Some(ws_accept(&key).as_str()) {
            return Err(WsError::ws("bad Sec-WebSocket-Accept"));
        }
        Ok(Self::start(conn, rest))
    }

    fn start(conn: Conn, rest: Vec<u8>) -> Self {
        let (out_tx, out_rx) = mpsc::channel();
        let (in_tx, in_rx) = mpsc::channel();
        let last_rx = Arc::new(Mutex::new(Instant::now()));
        let io = Io {
            conn,
            parser: Parser { buf: rest },
            parts: None,
            out_rx,
            in_tx,
            last_rx: Arc::clone(&last_rx),
        };
        let thread = std::thread::Builder::new()
            .name("websocket".into())
            .spawn(move || io.run())
            .ok();
        Self {
            handle: Arc::new(WsHandle { tx: out_tx }),
            events: in_rx,
            last_rx,
            io: thread,
        }
    }

    /// The sender the session and the console tail share.
    pub fn handle(&self) -> Arc<WsHandle> {
        Arc::clone(&self.handle)
    }

    /// `ws.last_rx`: when the last frame arrived.
    pub fn last_rx(&self) -> Instant {
        *self.last_rx.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// `recv()`: the next text message; `None` when the server closed.
    ///
    /// # Errors
    /// `RecvError::Timeout` after `timeout` without a message; `RecvError::Ws` when the session failed.
    pub fn recv(&self, timeout: Duration) -> Result<Option<String>, RecvError> {
        match self.events.recv_timeout(timeout) {
            Ok(Incoming::Text(t)) => Ok(Some(t)),
            Ok(Incoming::Closed) => Ok(None),
            Ok(Incoming::Error(e)) => Err(RecvError::Ws(e)),
            Err(RecvTimeoutError::Timeout) => Err(RecvError::Timeout),
            Err(RecvTimeoutError::Disconnected) => {
                Err(RecvError::Ws(WsError::ws("connection closed")))
            }
        }
    }

    /// `close()`: a close frame (1000), then the socket goes.
    pub fn close(&mut self) {
        let _ = self.handle.send(OP_CLOSE, &1000u16.to_be_bytes());
        let _ = self.handle.tx.send(Out::Shutdown);
        if let Some(t) = self.io.take() {
            let _ = t.join();
        }
    }
}

impl Drop for WebSocket {
    fn drop(&mut self) {
        let _ = self.handle.tx.send(Out::Shutdown);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use sha2::Sha256;
    use std::net::TcpListener;

    const VECTORS: &str = include_str!("../../../../tests/agent_parity/vectors/websocket.json");

    fn hex(b: &[u8]) -> String {
        use std::fmt::Write as _;
        b.iter().fold(String::new(), |mut s, x| {
            let _ = write!(s, "{x:02x}");
            s
        })
    }

    #[test]
    fn websocket_vectors() {
        let v: Value = serde_json::from_str(VECTORS).unwrap();
        let v = &v["vectors"];
        assert_eq!(v["guid"], WS_GUID);
        for a in v["accept"].as_array().unwrap() {
            assert_eq!(ws_accept(a["nonce_b64"].as_str().unwrap()), a["accept"]);
        }
        let payloads: Vec<Vec<u8>> = vec![
            b"".to_vec(),
            b"Hello".to_vec(),
            vec![b'x'; 125],
            vec![b'x'; 126],
            vec![b'x'; 65535],
            vec![b'x'; 65536],
            "caf\u{e9}".as_bytes().to_vec(),
        ];
        let frames = v["encode_frame"].as_array().unwrap();
        assert_eq!(frames.len(), 11);
        for f in frames {
            let want = f["payload_sha256"].as_str().unwrap();
            let p = payloads
                .iter()
                .find(|p| hex(&Sha256::digest(p)) == want)
                .expect("a known payload");
            let m = f["mask"].as_str().unwrap();
            let mask: Vec<u8> = (0..4)
                .map(|i| u8::from_str_radix(&m[2 * i..2 * i + 2], 16).unwrap())
                .collect();
            let op = u8::try_from(f["opcode"].as_u64().unwrap()).unwrap();
            let frame = encode_frame(op, p, [mask[0], mask[1], mask[2], mask[3]]);
            assert_eq!(frame.len() as u64, f["frame_len"].as_u64().unwrap());
            assert_eq!(hex(&frame[..frame.len().min(14)]), f["frame_head"]);
            assert_eq!(hex(&Sha256::digest(&frame)), f["frame_sha256"]);
        }
        let limits = &v["limits"];
        assert_eq!(limits["max_message"], WS_MAX_MESSAGE);
        assert_eq!(limits["recv_timeout_s"], WS_RECV_TIMEOUT_S);
        assert_eq!(limits["ping_s"], WS_PING_S);
        assert_eq!(limits["dead_s"], WS_DEAD_S);
    }

    #[test]
    fn parser_keeps_partial_frames_and_refuses_bad_ones() {
        let mut p = Parser::default();
        p.buf.extend_from_slice(&[0x81, 0x05, b'H', b'e']);
        assert_eq!(p.parse(), Ok(None));
        p.buf.extend_from_slice(b"llo");
        assert_eq!(p.parse(), Ok(Some((true, OP_TEXT, b"Hello".to_vec()))));
        assert!(p.buf.is_empty());
        for (bytes, err) in [
            (
                vec![0xC1, 0x00],
                "reserved bits set (no extension was negotiated)",
            ),
            (
                vec![0x81, 0x80],
                "masked frame from the server (RFC 6455 5.1)",
            ),
            (vec![0x09, 0x00], "bad control frame"),
            (
                vec![0x81, 127, 0, 0, 0, 0, 0, 0x20, 0, 1],
                "frame too large",
            ),
        ] {
            let mut p = Parser { buf: bytes };
            assert_eq!(p.parse(), Err(WsError::ws(err)));
        }
    }

    #[test]
    fn urls_split_like_urlsplit() {
        assert_eq!(
            split_url("wss://hb.Example.org/agent/ws").unwrap(),
            (
                true,
                "hb.Example.org".into(),
                "hb.example.org".into(),
                443,
                "/agent/ws".into()
            )
        );
        assert_eq!(
            split_url("ws://127.0.0.1:8787/agent/ws?x=1").unwrap(),
            (
                false,
                "127.0.0.1:8787".into(),
                "127.0.0.1".into(),
                8787,
                "/agent/ws?x=1".into()
            )
        );
        assert_eq!(split_url("ws://[::1]:9/").unwrap().2, "::1");
    }

    /// A tiny server: answers the upgrade, then runs `script` on the raw socket.
    fn server(
        script: impl FnOnce(TcpStream) + Send + 'static,
    ) -> (String, std::thread::JoinHandle<String>) {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("ws://{}/agent/ws", l.local_addr().unwrap());
        let h = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut buf = Vec::new();
            let mut c = [0u8; 1024];
            while !buf.windows(4).any(|w| w == b"\r\n\r\n") {
                let n = s.read(&mut c).unwrap();
                buf.extend_from_slice(&c[..n]);
            }
            let head = String::from_utf8_lossy(&buf).into_owned();
            let key = head
                .lines()
                .find_map(|l| l.strip_prefix("Sec-WebSocket-Key: "))
                .unwrap()
                .to_string();
            let reply = format!(
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {}\r\n\r\n",
                ws_accept(&key)
            );
            s.write_all(reply.as_bytes()).unwrap();
            script(s);
            head
        });
        (url, h)
    }

    /// Reads one masked client frame (opcode, unmasked payload).
    fn read_client_frame(s: &mut TcpStream) -> (u8, Vec<u8>) {
        let mut h = [0u8; 2];
        s.read_exact(&mut h).unwrap();
        assert_eq!(h[1] & 0x80, 0x80, "client frames are masked");
        let mut n = u64::from(h[1] & 0x7F);
        if n == 126 {
            let mut b = [0u8; 2];
            s.read_exact(&mut b).unwrap();
            n = u64::from(u16::from_be_bytes(b));
        }
        let mut mask = [0u8; 4];
        s.read_exact(&mut mask).unwrap();
        let mut data = vec![0u8; usize::try_from(n).unwrap()];
        s.read_exact(&mut data).unwrap();
        for (i, b) in data.iter_mut().enumerate() {
            *b ^= mask[i % 4];
        }
        (h[0] & 0x0F, data)
    }

    #[test]
    fn session_round_trip_fragments_pings_and_close() {
        let (url, h) = server(|mut s| {
            assert_eq!(read_client_frame(&mut s), (OP_TEXT, b"hello".to_vec()));
            // a fragmented message with a ping in the middle
            s.write_all(&[0x01, 0x03, b'a', b'b', b'c']).unwrap();
            s.write_all(&[0x89, 0x02, b'p', b'!']).unwrap();
            s.write_all(&[0x80, 0x02, b'd', b'e']).unwrap();
            assert_eq!(read_client_frame(&mut s), (OP_PONG, b"p!".to_vec()));
            // the tail's send from another thread
            assert_eq!(read_client_frame(&mut s), (OP_TEXT, b"from tail".to_vec()));
            s.write_all(&[0x88, 0x02, 0x03, 0xE8]).unwrap();
            assert_eq!(read_client_frame(&mut s), (OP_CLOSE, vec![0x03, 0xE8]));
            assert_eq!(read_client_frame(&mut s), (OP_CLOSE, vec![0x03, 0xE8]));
        });
        let mut ws = WebSocket::connect(&url, &[("User-Agent", "t")], 5.0).unwrap();
        assert!(ws.handle().send_text("hello"));
        assert_eq!(ws.recv(Duration::from_secs(5)), Ok(Some("abcde".into())));
        let tail = ws.handle();
        std::thread::spawn(move || assert!(tail.send_text("from tail")))
            .join()
            .unwrap();
        assert_eq!(ws.recv(Duration::from_secs(5)), Ok(None));
        ws.close();
        let head = h.join().unwrap();
        assert!(head.starts_with("GET /agent/ws HTTP/1.1\r\nHost: 127.0.0.1:"));
        assert!(head.contains("\r\nSec-WebSocket-Version: 13\r\nUser-Agent: t\r\n\r\n"));
    }

    #[test]
    fn refused_upgrade_and_timeouts() {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("ws://{}/agent/ws", l.local_addr().unwrap());
        let t = std::thread::spawn(move || {
            let (mut s, _) = l.accept().unwrap();
            let mut c = [0u8; 4096];
            let _ = s.read(&mut c).unwrap();
            s.write_all(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
        });
        let e = WebSocket::connect(&url, &[], 5.0).err().unwrap();
        assert_eq!(
            e,
            WsError::ws("upgrade refused: HTTP/1.1 503 Service Unavailable")
        );
        t.join().unwrap();

        let (url, h) = server(|mut s| {
            let _ = read_client_frame(&mut s); // the close frame
        });
        let mut ws = WebSocket::connect(&url, &[], 5.0).unwrap();
        assert_eq!(ws.recv(Duration::from_millis(200)), Err(RecvError::Timeout));
        ws.close();
        h.join().unwrap();

        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("ws://{}/", l.local_addr().unwrap());
        drop(l);
        let e = WebSocket::connect(&url, &[], 5.0).err().unwrap();
        assert_eq!(e.class, "ConnectionRefusedError");
    }
}
