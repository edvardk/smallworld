//! The audit trail: one structured journal entry per command (mc_agent.py `audit`), sent with journald's native
//! protocol over its datagram socket, so no libsystemd is needed.
//!
//! The entry is exactly Python's: `MESSAGE`, `SYSLOG_IDENTIFIER=mc-agent`, `MC_AUDIT=1`, `PRIORITY=5`, then one
//! `MC_<NAME>` field per keyword argument in the order given (a later field with the same name replaces the
//! earlier one in place, like Python's `dict |=`). A value with a newline uses the protocol's binary-safe form
//! (`KEY\n<u64 little-endian length><bytes>\n`); others are `KEY=value\n`. When the socket is missing (or the
//! datagram is refused, e.g. too big), the line `AUDIT <message>` goes to stderr instead, as in Python.

use crate::paths::JOURNAL_SOCKET;
use std::io::Write;
use std::os::unix::net::UnixDatagram;
use std::path::Path;

/// The entry's fields in order, after the `MC_` prefixing and upper-casing (`MC_{k.upper()}`).
pub fn entry_fields(message: &str, fields: &[(&str, &str)]) -> Vec<(String, String)> {
    let mut entry: Vec<(String, String)> = vec![
        ("MESSAGE".into(), message.into()),
        ("SYSLOG_IDENTIFIER".into(), "mc-agent".into()),
        ("MC_AUDIT".into(), "1".into()),
        ("PRIORITY".into(), "5".into()),
    ];
    for (k, v) in fields {
        // str.upper() of the keyword names used here (ASCII identifiers).
        let key = format!("MC_{}", k.to_uppercase());
        match entry.iter_mut().find(|(name, _)| *name == key) {
            Some(slot) => slot.1 = (*v).to_string(),
            None => entry.push((key, (*v).to_string())),
        }
    }
    entry
}

/// The datagram for one entry (journald's native protocol).
pub fn encode(message: &str, fields: &[(&str, &str)]) -> Vec<u8> {
    let mut data = Vec::new();
    for (key, value) in entry_fields(message, fields) {
        let raw = value.as_bytes();
        if raw.contains(&b'\n') {
            data.extend_from_slice(key.as_bytes());
            data.push(b'\n');
            data.extend_from_slice(&(raw.len() as u64).to_le_bytes());
            data.extend_from_slice(raw);
            data.push(b'\n');
        } else {
            data.extend_from_slice(key.as_bytes());
            data.push(b'=');
            data.extend_from_slice(raw);
            data.push(b'\n');
        }
    }
    data
}

/// Sends one entry to the journald socket at `socket`; on any error, `AUDIT <message>` on stderr.
pub fn audit_to(socket: &Path, message: &str, fields: &[(&str, &str)]) {
    let data = encode(message, fields);
    let sent = UnixDatagram::unbound().and_then(|s| s.send_to(&data, socket));
    if sent.is_err() {
        let _ = writeln!(std::io::stderr(), "AUDIT {message}");
    }
}

/// mc_agent.py `audit(message, **fields)`.
pub fn audit(message: &str, fields: &[(&str, &str)]) {
    audit_to(Path::new(JOURNAL_SOCKET), message, fields);
}

/// Parses a native-protocol datagram back into fields (tests and the parity harness).
pub fn decode(mut data: &[u8]) -> Option<Vec<(String, Vec<u8>)>> {
    let mut out = Vec::new();
    while !data.is_empty() {
        let nl = data.iter().position(|&b| b == b'\n')?;
        let line = &data[..nl];
        if let Some(eq) = line.iter().position(|&b| b == b'=') {
            out.push((
                String::from_utf8(line[..eq].to_vec()).ok()?,
                line[eq + 1..].to_vec(),
            ));
            data = &data[nl + 1..];
        } else {
            let key = String::from_utf8(line.to_vec()).ok()?;
            let rest = &data[nl + 1..];
            let len = usize::try_from(u64::from_le_bytes(rest.get(..8)?.try_into().ok()?)).ok()?;
            let value = rest.get(8..8 + len)?.to_vec();
            if rest.get(8 + len) != Some(&b'\n') {
                return None;
            }
            out.push((key, value));
            data = &rest[9 + len..];
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encodes_like_python() {
        let data = encode(
            "mcctl action=restart ok=True login=mcctl:ed",
            &[
                ("action", "restart"),
                ("login", "mcctl:ed"),
                ("args", "{}"),
                ("via", "mcctl"),
                ("result", "ok"),
            ],
        );
        assert_eq!(
            String::from_utf8(data).unwrap(),
            "MESSAGE=mcctl action=restart ok=True login=mcctl:ed\nSYSLOG_IDENTIFIER=mc-agent\nMC_AUDIT=1\n\
             PRIORITY=5\nMC_ACTION=restart\nMC_LOGIN=mcctl:ed\nMC_ARGS={}\nMC_VIA=mcctl\nMC_RESULT=ok\n"
        );
    }

    #[test]
    fn multi_line_values_use_the_binary_form() {
        let data = encode("m", &[("result", "a\nb")]);
        let mut want =
            b"MESSAGE=m\nSYSLOG_IDENTIFIER=mc-agent\nMC_AUDIT=1\nPRIORITY=5\nMC_RESULT\n".to_vec();
        want.extend_from_slice(&3u64.to_le_bytes());
        want.extend_from_slice(b"a\nb\n");
        assert_eq!(data, want);
        let back = decode(&data).unwrap();
        assert_eq!(
            back.last().unwrap(),
            &("MC_RESULT".to_string(), b"a\nb".to_vec())
        );
    }

    #[test]
    fn a_field_named_like_a_fixed_one_replaces_it_in_place() {
        let f = entry_fields("m", &[("audit", "x"), ("action", "a"), ("action", "b")]);
        assert_eq!(f[2], ("MC_AUDIT".into(), "x".into()));
        assert_eq!(f.len(), 5);
        assert_eq!(f[4], ("MC_ACTION".into(), "b".into()));
    }

    #[test]
    fn sends_a_datagram() {
        let dir = std::env::temp_dir().join(format!("swcore-audit-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let sock = dir.join("journal.sock");
        let _ = std::fs::remove_file(&sock);
        let server = UnixDatagram::bind(&sock).unwrap();
        audit_to(&sock, "hello", &[("via", "mcctl")]);
        let mut buf = vec![0u8; 4096];
        let n = server.recv(&mut buf).unwrap();
        assert_eq!(&buf[..n], encode("hello", &[("via", "mcctl")]).as_slice());
        std::fs::remove_dir_all(dir).unwrap();
    }
}
