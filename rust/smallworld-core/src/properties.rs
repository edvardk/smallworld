//! Reading server.properties: mc_rcon.py's `read_untrusted`, `read_properties` and `unescape`.
//!
//! The server directory is writable by the `minecraft` user, so the file is opened the careful way: never
//! following a symlink, never blocking on a FIFO, refusing anything but a regular file, reading at most 1 MiB.
//! Then each line is parsed like Python's regular expression `((?:\\.|[^=:\s\\])+)\s*[=:\s]\s*(.*)$` does.

use crate::error::PyError;
use crate::pystr::{self, PyStr};
use std::collections::HashMap;
use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;

/// Open flags std has no names for. `O_NOFOLLOW`'s value differs between the two architectures we build for.
const O_NONBLOCK: i32 = 0o4000;
const O_NOCTTY: i32 = 0o400;
#[cfg(target_arch = "aarch64")]
const O_NOFOLLOW: i32 = 0o100_000;
#[cfg(not(target_arch = "aarch64"))]
const O_NOFOLLOW: i32 = 0o400_000;

/// What `read_properties` limits a server.properties to (Python: `read_untrusted(path, 1 << 20)`).
const PROPERTIES_MAX_BYTES: u64 = 1 << 20;

/// The settings of a server.properties file. A type alias is just a shorter name for a type.
pub type Properties = HashMap<PyStr, PyStr>;

/// mc_rcon.py `UNTRUSTED_MAX_BYTES`: `read_untrusted`'s default limit.
pub const UNTRUSTED_MAX_BYTES: u64 = 16 << 20;

/// `read_untrusted(path, 1 << 20)`: the first 1 MiB of a regular file, never through a symlink.
pub fn read_untrusted(path: &PyStr) -> Result<Vec<u8>, PyError> {
    read_untrusted_limit(path, PROPERTIES_MAX_BYTES)
}

/// `read_untrusted(path, limit)`: the first `limit` bytes of a regular file, never through a symlink, never
/// blocking (a FIFO opens at once and is refused).
pub fn read_untrusted_limit(path: &PyStr, limit: u64) -> Result<Vec<u8>, PyError> {
    let os_path = std::ffi::OsStr::from_bytes(&path.to_os_bytes()).to_owned();
    // `?` here: if opening fails, return the error (with the path, as Python's OSError has it).
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(O_NONBLOCK | O_NOFOLLOW | O_NOCTTY) // std adds O_CLOEXEC itself
        .open(&os_path)
        .map_err(|e| PyError::from_io_with_path(&e, Some(path)))?;
    let meta = file.metadata().map_err(|e| PyError::from_io(&e))?;
    if !meta.is_file() {
        return Err(PyError::Os {
            errno: 22,
            strerror: "not a regular file".into(),
            filename: Some(path.clone()),
        });
    }
    let mut data = Vec::new();
    // `take` limits the reader to N bytes; `read_to_end` collects them. `&file` borrows the file: we lend it to
    // the reader without giving it away (it is closed when `file` goes out of scope at the end of the function).
    (&file)
        .take(limit)
        .read_to_end(&mut data)
        .map_err(|e| PyError::from_io(&e))?;
    Ok(data)
}

/// `read_properties(path)`: every `key=value` of a Java .properties file (the subset Minecraft writes).
pub fn read_properties(path: &PyStr) -> Result<Properties, PyError> {
    let text = pystr::decode_utf8_strict(&read_untrusted(path)?)?;
    parse_properties(&PyStr::from(text.as_str()))
}

/// The parsing half of `read_properties`, on already decoded text.
pub fn parse_properties(text: &PyStr) -> Result<Properties, PyError> {
    let mut props = Properties::new();
    for raw in text.splitlines() {
        let line = raw.strip();
        let cps = line.code_points();
        // `if not line or line[0] in "#!": continue`
        match cps.first() {
            None => continue,
            Some(&c) if c == u32::from('#') || c == u32::from('!') => continue,
            Some(_) => {}
        }
        match split_key_value(cps) {
            Some((key_end, value_start)) => {
                let key = unescape(&cps[..key_end])?;
                let value = unescape(&cps[value_start..])?;
                props.insert(key, value);
            }
            // No separator after the key: the whole line is the key, with an empty value.
            None => {
                props.insert(unescape(cps)?, PyStr::new());
            }
        }
    }
    Ok(props)
}

/// What the regular expression matches on a stripped line: where the key ends and the value starts, or `None` if
/// it doesn't match. The key is one or more of: a backslash and any character, or a character that is not `=`,
/// `:`, whitespace or a backslash. A separator follows: whitespace, then one `=` or `:` or (if there was
/// whitespace) nothing, then whitespace again.
fn split_key_value(line: &[u32]) -> Option<(usize, usize)> {
    let is_sep = |c: u32| c == u32::from('=') || c == u32::from(':') || pystr::is_space(c);
    let backslash = u32::from('\\');
    let mut i = 0;
    while i < line.len() {
        let c = line[i];
        if c == backslash {
            if i + 1 < line.len() {
                i += 2; // an escaped character belongs to the key, whatever it is
                continue;
            }
            return None; // a lone backslash at the end: the pattern can't continue past it
        }
        if is_sep(c) {
            break;
        }
        i += 1;
    }
    if i == 0 || i == line.len() {
        return None; // no key, or no separator after it
    }
    let key_end = i;
    // `\s*[=:\s]\s*`: skip whitespace, then one '=' or ':' if there is one, then whitespace.
    let mut j = key_end;
    while j < line.len() && pystr::is_space(line[j]) {
        j += 1;
    }
    if j < line.len() && (line[j] == u32::from('=') || line[j] == u32::from(':')) {
        j += 1;
        while j < line.len() && pystr::is_space(line[j]) {
            j += 1;
        }
    }
    Some((key_end, j))
}

/// `unescape(s)`: Java's escapes. `\t \n \r \f`, `\uXXXX` (four characters read as `int(..., 16)`), and a
/// backslash before anything else stands for that character.
pub fn unescape(s: &[u32]) -> Result<PyStr, PyError> {
    let mut out = PyStr::new();
    let mut i = 0;
    while i < s.len() {
        let c = s[i];
        if c == u32::from('\\') && i + 1 < s.len() {
            let n = s[i + 1];
            if n == u32::from('u') && i + 5 < s.len() {
                let hex = &s[i + 2..i + 6];
                let value = pystr::parse_hex(hex).ok_or_else(|| {
                    let text = PyStr::from_code_points(hex);
                    PyError::Value(format!(
                        "invalid literal for int() with base 16: {}",
                        text.repr()
                    ))
                })?;
                // chr(value): a negative value is out of range; any 0..0xFFFF is a code point (surrogates too).
                let cp = u32::try_from(value)
                    .map_err(|_| PyError::Value("chr() arg not in range(0x110000)".into()))?;
                out.push(cp);
                i += 6;
                continue;
            }
            out.push(match char::from_u32(n) {
                Some('t') => u32::from('\t'),
                Some('n') => u32::from('\n'),
                Some('r') => u32::from('\r'),
                Some('f') => 0x0C,
                _ => n,
            });
            i += 2;
            continue;
        }
        out.push(c);
        i += 1;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(text: &str) -> Vec<(String, String)> {
        let props = parse_properties(&PyStr::from(text)).unwrap();
        let mut items: Vec<(String, String)> = props
            .iter()
            .map(|(k, v)| (k.display(), v.display()))
            .collect();
        items.sort();
        items
    }

    fn pairs(want: &[(&str, &str)]) -> Vec<(String, String)> {
        let mut v: Vec<(String, String)> = want
            .iter()
            .map(|(k, v)| ((*k).into(), (*v).into()))
            .collect();
        v.sort();
        v
    }

    #[test]
    fn the_python_tests_example() {
        // tests/test_rcon.py test_read_properties_escapes
        let got = parse("#c\nmotd=A\\u00e9 \\: b\\=c\nrcon.password=abc\nempty=\n");
        assert_eq!(
            got,
            pairs(&[
                ("motd", "Aé : b=c"),
                ("rcon.password", "abc"),
                ("empty", "")
            ])
        );
    }

    #[test]
    fn separators_whitespace_and_comments() {
        let got = parse(
            "  a = 1  \r\nb:2\nc 3\nd  :  4\n! bang\n   # hash\ne\nf=\n=g\nh\\ i=5\nj = = 6\nk\\\nl\t7\x0cm=8",
        );
        let want = pairs(&[
            ("a", "1"),
            ("b", "2"),
            ("c", "3"),
            ("d", "4"),
            ("e", ""),
            ("f", ""),
            ("=g", ""),
            ("h i", "5"),
            ("j", "= 6"),
            ("k\\", ""),
            ("l", "7"),
            ("m", "8"),
        ]);
        assert_eq!(got, want);
    }

    #[test]
    fn escapes() {
        let got = parse("a=\\t\\n\\r\\f\\q\\\\\nb=\\u0041\\u0x42\\u 43 Z\nc=\\u12\nd=x\\");
        assert_eq!(
            got,
            pairs(&[
                ("a", "\t\n\r\x0cq\\"),
                ("b", "ABCZ"),
                ("c", "u12"),
                ("d", "x\\")
            ])
        );
        let err = parse_properties(&PyStr::from("a=\\uZZZZ")).unwrap_err();
        assert_eq!(
            err.traceback_line(),
            "ValueError: invalid literal for int() with base 16: 'ZZZZ'"
        );
        let err = parse_properties(&PyStr::from("a=\\u-001")).unwrap_err();
        assert_eq!(
            err.traceback_line(),
            "ValueError: chr() arg not in range(0x110000)"
        );
    }

    #[test]
    fn surrogate_escapes_are_kept_until_used() {
        let props =
            parse_properties(&PyStr::from("motd=\\uD83D\\uDE00\nrcon.password=\\ud800")).unwrap();
        assert_eq!(props[&PyStr::from("motd")].code_points(), &[0xD83D, 0xDE00]);
        assert!(props[&PyStr::from("rcon.password")].encode_utf8().is_err());
    }

    #[test]
    fn later_lines_win() {
        assert_eq!(parse("a=1\na=2"), pairs(&[("a", "2")]));
    }
}
