//! The RCON protocol: mc_rcon.py's `Rcon` class.
//!
//! Every packet, both ways, is:
//!
//! ```text
//! length   i32, little-endian: the number of bytes that follow
//! id       i32, little-endian: chosen by the client, echoed by the server (-1 = login refused)
//! type     i32, little-endian: 3 = login, 2 = command, 0 = response
//! body     UTF-8 text
//! 0x00 0x00
//! ```
//!
//! A conversation: login (type 3, body = password), the server answers with the same id (or -1); then each
//! command (type 2) gets one or more response packets (type 0) with the command's id. A long reply is split into
//! several packets with no end marker, so after the first packet the client sends a dummy packet of type 0: the
//! server answers requests in order, so its answer ("Unknown request 0") arrives right after the last fragment.

use crate::error::PyError;
use crate::pystr::PyStr;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::Duration;

pub const TYPE_RESPONSE: i32 = 0;
pub const TYPE_COMMAND: i32 = 2;
pub const TYPE_LOGIN: i32 = 3;

/// The bytes of one packet: length, id, type, body, two zero bytes.
pub fn encode_packet(id: i32, kind: i32, payload: &[u8]) -> Vec<u8> {
    let mut body = Vec::with_capacity(payload.len() + 10);
    body.extend_from_slice(&id.to_le_bytes());
    body.extend_from_slice(&kind.to_le_bytes());
    body.extend_from_slice(payload);
    body.extend_from_slice(&[0, 0]);
    // The length counts everything after itself. A packet is never anywhere near 2 GiB: saturate rather than wrap.
    let length = i32::try_from(body.len()).unwrap_or(i32::MAX);
    let mut packet = length.to_le_bytes().to_vec();
    packet.extend_from_slice(&body);
    packet
}

/// A received packet, decoded the way `_recv` does: `data[8:-2]` as UTF-8, bad bytes replaced by U+FFFD.
#[derive(Debug, PartialEq, Eq)]
pub struct Packet {
    pub id: i32,
    pub kind: i32,
    pub body: String,
}

/// `_recv`'s second half: the `length` bytes after the length field.
pub fn decode_packet(data: &[u8]) -> Result<Packet, PyError> {
    // struct.unpack("<ii", data[:8]) needs 8 bytes.
    if data.len() < 8 {
        return Err(PyError::Struct(
            "unpack requires a buffer of 8 bytes".into(),
        ));
    }
    let id = i32::from_le_bytes([data[0], data[1], data[2], data[3]]);
    let kind = i32::from_le_bytes([data[4], data[5], data[6], data[7]]);
    // Python's slice data[8:-2] is empty when there are fewer than 10 bytes.
    let body = if data.len() >= 10 {
        &data[8..data.len() - 2]
    } else {
        &[][..]
    };
    Ok(Packet {
        id,
        kind,
        body: String::from_utf8_lossy(body).into_owned(),
    })
}

/// `strip_formatting`: removes Minecraft's colour codes, `§` and the character after it (`re.sub("§.", "")`:
/// `.` is anything but a newline).
pub fn strip_formatting(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '§' && chars.peek().is_some_and(|&n| n != '\n') {
            chars.next();
            continue;
        }
        out.push(c);
    }
    out
}

/// One RCON connection. A `struct` holds data; the functions that work on it live in an `impl` block below
/// (Python puts both in a `class`).
pub struct Rcon {
    stream: TcpStream,
    timeout: Duration,
    last_id: i32,
}

impl Rcon {
    /// `socket.create_connection((host, port), timeout)`: try each address the host resolves to; the last error
    /// wins. Then log in (`__enter__`). `port` is already what the system call gets (see ops.rs).
    pub fn connect(
        host: &str,
        port: u16,
        password: &PyStr,
        timeout: Duration,
    ) -> Result<Rcon, PyError> {
        let addrs: Vec<SocketAddr> = (host, port)
            .to_socket_addrs()
            .map_err(|e| gai_error(&e))?
            .collect();
        let mut last_error = PyError::Gai {
            code: -2,
            message: "Name or service not known".into(),
        };
        for addr in addrs {
            match TcpStream::connect_timeout(&addr, timeout) {
                Ok(stream) => {
                    // `.map_err(...)` turns an io::Error into our PyError, so `?` can return it.
                    stream
                        .set_read_timeout(Some(timeout))
                        .map_err(|e| PyError::from_io(&e))?;
                    stream
                        .set_write_timeout(Some(timeout))
                        .map_err(|e| PyError::from_io(&e))?;
                    let mut rcon = Rcon {
                        stream,
                        timeout,
                        last_id: 0,
                    };
                    rcon.login(password)?;
                    return Ok(rcon);
                }
                Err(e) => last_error = PyError::from_io(&e),
            }
        }
        Err(last_error)
    }

    /// `__enter__`'s login: `_request(TYPE_LOGIN, password)`, id -1 = wrong password.
    fn login(&mut self, password: &PyStr) -> Result<(), PyError> {
        let rid = self.send(TYPE_LOGIN, password)?;
        loop {
            let packet = self.recv()?;
            // Login replies with an empty RESPONSE before the auth result on some servers.
            if packet.kind == TYPE_RESPONSE {
                continue;
            }
            if packet.id != rid && packet.id != -1 {
                return Err(unexpected_id(packet.id, rid));
            }
            return if packet.id == -1 {
                Err(PyError::RconAuth)
            } else {
                Ok(())
            };
        }
    }

    /// `_send`: the next id, then the packet. `&mut self` means this method may change the connection (it
    /// writes to the socket and bumps the id); Python's `self` is always mutable, Rust says so explicitly.
    fn send(&mut self, kind: i32, payload: &PyStr) -> Result<i32, PyError> {
        self.last_id += 1;
        let bytes = payload.encode_utf8()?;
        self.stream
            .write_all(&encode_packet(self.last_id, kind, &bytes))
            .map_err(|e| PyError::from_io(&e))?;
        Ok(self.last_id)
    }

    /// `_recv`: one packet. A negative length reads nothing (then fails to unpack, as in Python).
    fn recv(&mut self) -> Result<Packet, PyError> {
        let head = self.read_exact(4)?;
        let length = i32::from_le_bytes([head[0], head[1], head[2], head[3]]);
        let data = self.read_exact(usize::try_from(length).unwrap_or(0))?;
        decode_packet(&data)
    }

    /// `_read_exact`: exactly `n` bytes, however the network splits them. The buffer grows as data arrives (a
    /// huge length from a broken server never allocates more than what was really received).
    fn read_exact(&mut self, n: usize) -> Result<Vec<u8>, PyError> {
        let mut buf = Vec::new();
        let mut chunk = [0u8; 8192];
        while buf.len() < n {
            let want = (n - buf.len()).min(chunk.len());
            match self.stream.read(&mut chunk[..want]) {
                Ok(0) => return Err(PyError::Rcon("connection closed by server".into())),
                // `&chunk[..got]` borrows the first `got` bytes; `extend_from_slice` copies them into `buf`.
                Ok(got) => buf.extend_from_slice(&chunk[..got]),
                Err(e) if e.kind() == ErrorKind::Interrupted => {} // Python retries EINTR too (PEP 475)
                Err(e) => return Err(PyError::from_io(&e)),
            }
        }
        Ok(buf)
    }

    /// `settimeout`: Python's socket timeout covers every send and receive.
    fn set_timeout(&mut self, timeout: Duration) -> Result<(), PyError> {
        self.stream
            .set_read_timeout(Some(timeout))
            .map_err(|e| PyError::from_io(&e))?;
        self.stream
            .set_write_timeout(Some(timeout))
            .map_err(|e| PyError::from_io(&e))
    }

    /// `command(cmd, timeout, single)`: run a command and return its whole reply, colour codes removed.
    /// `timeout` (seconds) overrides the connection's for this command; `single` reads one packet only (for
    /// `stop`, after which the server may close the connection).
    pub fn command(
        &mut self,
        cmd: &PyStr,
        timeout: Option<f64>,
        single: bool,
    ) -> Result<String, PyError> {
        if let Some(t) = timeout {
            self.set_timeout(Duration::from_secs_f64(t))?;
        }
        let result = self.command_inner(cmd, single);
        // Python's `finally:` — restore the timeout whether the command worked or not.
        if timeout.is_some() {
            let restored = self.set_timeout(self.timeout);
            if result.is_ok() {
                restored?;
            }
        }
        result.map(|reply| strip_formatting(&reply))
    }

    fn command_inner(&mut self, cmd: &PyStr, single: bool) -> Result<String, PyError> {
        let cid = self.send(TYPE_COMMAND, cmd)?;
        let first = self.recv()?;
        if first.id != cid {
            return Err(unexpected_id(first.id, cid));
        }
        let mut reply = first.body;
        if single {
            return Ok(reply);
        }
        // Replies over 4096 bytes arrive as several packets with no end marker. The server handles one request
        // at a time, in order, so a follow-up packet of an invalid type is answered ("Unknown request 0") only
        // after the last fragment. It is sent after the first fragment arrives, because vanilla drops the
        // connection if two requests arrive in one read.
        let marker = self.send(TYPE_RESPONSE, &PyStr::new())?;
        loop {
            let packet = self.recv()?;
            if packet.id == marker {
                break;
            }
            if packet.id != cid {
                return Err(unexpected_id(packet.id, cid));
            }
            reply.push_str(&packet.body);
        }
        Ok(reply)
    }
}

fn unexpected_id(got: i32, expected: i32) -> PyError {
    PyError::Rcon(format!(
        "unexpected RCON request id {got} (expected {expected})"
    ))
}

/// A failed name lookup, as Python's `socket.gaierror` (the codes and texts are glibc's; this binary uses musl's
/// resolver, so its own wording is translated back).
fn gai_error(e: &std::io::Error) -> PyError {
    let text = e.to_string();
    if text.contains("Temporary failure") || text.contains("Try again") {
        PyError::Gai {
            code: -3,
            message: "Temporary failure in name resolution".into(),
        }
    } else {
        PyError::Gai {
            code: -2,
            message: "Name or service not known".into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn packets_encode_and_decode() {
        let p = encode_packet(7, TYPE_COMMAND, b"list");
        assert_eq!(
            p,
            [
                14, 0, 0, 0, 7, 0, 0, 0, 2, 0, 0, 0, b'l', b'i', b's', b't', 0, 0
            ]
        );
        assert_eq!(
            decode_packet(&p[4..]).unwrap(),
            Packet {
                id: 7,
                kind: 2,
                body: "list".into()
            }
        );
        assert_eq!(
            decode_packet(&[255, 255, 255, 255, 0, 0, 0, 0, 0])
                .unwrap()
                .body,
            ""
        );
        assert_eq!(
            decode_packet(&[1, 0, 0, 0]).unwrap_err().traceback_line(),
            "struct.error: unpack requires a buffer of 8 bytes"
        );
        // Invalid UTF-8 is replaced, one U+FFFD per bad sequence, as Python's errors="replace".
        assert_eq!(
            decode_packet(&[1, 0, 0, 0, 0, 0, 0, 0, b'a', 0xFF, 0xE2, 0x82, 0, 0])
                .unwrap()
                .body,
            "a\u{FFFD}\u{FFFD}"
        );
    }

    #[test]
    fn formatting_codes_are_stripped() {
        assert_eq!(
            strip_formatting("§6There are §a3§6 players"),
            "There are 3 players"
        );
        assert_eq!(strip_formatting("§§a"), "a");
        assert_eq!(strip_formatting("a§\nb§"), "a§\nb§");
    }

    /// A tiny scripted server: answers the login, then for each command packet calls `reply`, which returns the
    /// raw bytes to send (written in single bytes when `trickle`, to exercise fragmented reads).
    fn serve(password: &'static str, trickle: bool, reply: fn(i32, &str) -> Vec<u8>) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            loop {
                let mut head = [0u8; 4];
                if s.read_exact(&mut head).is_err() {
                    return;
                }
                let mut data = vec![0u8; usize::try_from(i32::from_le_bytes(head)).unwrap()];
                s.read_exact(&mut data).unwrap();
                let p = decode_packet(&data).unwrap();
                let out = match p.kind {
                    TYPE_LOGIN => encode_packet(if p.body == password { p.id } else { -1 }, 2, b""),
                    TYPE_COMMAND => reply(p.id, &p.body),
                    _ => encode_packet(p.id, 0, b"Unknown request 0"),
                };
                if trickle {
                    for b in out {
                        s.write_all(&[b]).unwrap();
                    }
                } else {
                    s.write_all(&out).unwrap();
                }
            }
        });
        port
    }

    fn connect(port: u16, password: &str) -> Result<Rcon, PyError> {
        Rcon::connect(
            "127.0.0.1",
            port,
            &PyStr::from(password),
            Duration::from_secs(5),
        )
    }

    #[test]
    fn fragmented_reads_and_multi_packet_replies() {
        let port = serve("pw", true, |id, cmd| {
            let mut out = encode_packet(id, 0, format!("§6{cmd} part 1;").as_bytes());
            out.extend(encode_packet(id, 0, b" part 2"));
            out
        });
        let mut r = connect(port, "pw").unwrap();
        assert_eq!(
            r.command(&PyStr::from("list"), None, false).unwrap(),
            "list part 1; part 2"
        );
        assert_eq!(
            r.command(&PyStr::from("again"), Some(2.0), false).unwrap(),
            "again part 1; part 2"
        );
    }

    #[test]
    fn single_reads_one_packet() {
        let port = serve("pw", false, |id, _| {
            encode_packet(id, 0, b"Stopping the server")
        });
        let mut r = connect(port, "pw").unwrap();
        assert_eq!(
            r.command(&PyStr::from("stop"), Some(3.0), true).unwrap(),
            "Stopping the server"
        );
    }

    #[test]
    fn wrong_password_and_wrong_ids() {
        let port = serve("right", false, |id, _| encode_packet(id, 0, b""));
        assert_eq!(
            connect(port, "wrong").err().unwrap().traceback_line(),
            "RconAuthError: RCON authentication failed"
        );
        let port = serve("pw", false, |id, _| encode_packet(id + 5, 0, b""));
        let err = connect(port, "pw")
            .unwrap()
            .command(&PyStr::from("x"), None, false)
            .unwrap_err();
        assert_eq!(err.to_string(), "unexpected RCON request id 7 (expected 2)");
    }

    #[test]
    fn closed_connection_and_refused() {
        let port = serve("pw", false, |_, _| Vec::new());
        // The server answers nothing to the command; with a 1 s timeout that is Python's "timed out".
        let mut r = Rcon::connect(
            "127.0.0.1",
            port,
            &PyStr::from("pw"),
            Duration::from_secs(1),
        )
        .unwrap();
        assert_eq!(
            r.command(&PyStr::from("x"), None, false)
                .unwrap_err()
                .traceback_line(),
            "TimeoutError: timed out"
        );
        let free = TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let err = connect(free, "pw").err().unwrap();
        assert_eq!(
            err.traceback_line(),
            "ConnectionRefusedError: [Errno 111] Connection refused"
        );
    }
}
