//! mc_rcon.py's `connect()`: the RCON settings from the server's server.properties (`$MC_SERVER_DIR`), then a
//! logged-in connection (rcon.rs). Shared by smallworld-rcon and smallworld-agent.

use crate::error::PyError;
use crate::properties;
use crate::pystr::{IntError, PyInt, PyStr};
use crate::rcon::Rcon;
use std::os::unix::ffi::OsStrExt;
use std::time::Duration;

/// `connect()`'s default timeout, and the socket timeout of every command that doesn't set its own.
pub const TIMEOUT: Duration = Duration::from_secs(10);

/// An environment variable as Python's `os.environ.get` sees it.
pub fn env(name: &str) -> Option<PyStr> {
    std::env::var_os(name).map(|v| PyStr::from_os_bytes(v.as_bytes()))
}

/// `Path(text) / name` as text: pathlib drops empty and `.` parts and trailing slashes, and keeps exactly two
/// leading slashes (POSIX lets them mean something) but folds three or more into one.
pub fn path_join(dir: &PyStr, name: &str) -> PyStr {
    let slash = u32::from('/');
    let cps = dir.code_points();
    let root = if cps.starts_with(&[slash, slash]) && cps.get(2) != Some(&slash) {
        "//"
    } else if cps.first() == Some(&slash) {
        "/"
    } else {
        ""
    };
    let mut out = PyStr::from(root);
    for part in cps.split(|&c| c == slash) {
        if part.is_empty() || part == [u32::from('.')] {
            continue;
        }
        out.push_py(&PyStr::from_code_points(part));
        out.push(slash);
    }
    out.push_str(name);
    out
}

/// `SERVER_DIR / "server.properties"`, `SERVER_DIR` coming from `$MC_SERVER_DIR` (default /opt/minecraft/server).
pub fn properties_path() -> PyStr {
    path_join(&server_dir(), "server.properties")
}

/// mc_rcon.py's `SERVER_DIR`: `$MC_SERVER_DIR`, default /opt/minecraft/server.
pub fn server_dir() -> PyStr {
    env("MC_SERVER_DIR").unwrap_or_else(|| PyStr::from("/opt/minecraft/server"))
}

/// `int(text)`'s `ValueError`, worded as Python words it.
pub fn int_error(text: &PyStr, e: &IntError) -> PyError {
    match e {
        IntError::Invalid => PyError::Value(format!(
            "invalid literal for int() with base 10: {}",
            text.repr()
        )),
        IntError::TooManyDigits(n) => PyError::Value(format!(
            "Exceeds the limit (4300 digits) for integer string conversion: value has {n} digits; use \
             sys.set_int_max_str_digits() to increase the limit"
        )),
    }
}

/// The port the system call gets for `rcon.port`, the way Python and glibc's `getaddrinfo` treat it: taken
/// modulo 65536, refused (`gaierror -8`) when negative or too big.
pub fn socket_port(port: &PyInt) -> Result<u16, PyError> {
    let refused = || PyError::Gai {
        code: -8,
        message: "Servname not supported for ai_socktype".into(),
    };
    match port.to_i128() {
        Some(p) if (0..i128::from(i64::MAX)).contains(&p) => {
            Ok(u16::try_from(p % 65536).unwrap_or(0))
        }
        _ => Err(refused()),
    }
}

/// `connect()`: read server.properties, then connect and log in, with the default 10 s timeout.
pub fn connect() -> Result<Rcon, PyError> {
    connect_with(None, TIMEOUT)
}

/// `connect(server_dir, timeout)`: `server_dir` defaults to `SERVER_DIR` ($MC_SERVER_DIR or /opt/minecraft/server).
pub fn connect_with(server_dir: Option<&PyStr>, timeout: Duration) -> Result<Rcon, PyError> {
    let path = match server_dir {
        Some(dir) => path_join(dir, "server.properties"),
        None => properties_path(),
    };
    let props = properties::read_properties(&path)?;
    let get = |key: &str| props.get(&PyStr::from(key));
    if !get("enable-rcon").is_some_and(|v| v.eq_str("true")) {
        return Err(PyError::Rcon(
            "RCON is disabled in server.properties".into(),
        ));
    }
    // `props.get("rcon.ip") or "127.0.0.1"`: missing or empty means localhost.
    let host = match get("rcon.ip") {
        Some(ip) if !ip.is_empty() => ip.clone(),
        _ => PyStr::from("127.0.0.1"),
    };
    let port_text = get("rcon.port")
        .cloned()
        .unwrap_or_else(|| PyStr::from("25575"));
    let port = PyInt::parse(&port_text).map_err(|e| int_error(&port_text, &e))?;
    let password =
        get("rcon.password").ok_or_else(|| PyError::Key(PyStr::from("rcon.password")))?;
    let host = host.to_string_checked().ok_or_else(|| {
        PyError::UnicodeEncode(format!(
            "encoding with 'idna' codec failed (UnicodeEncodeError: 'idna' codec can't encode {})",
            host.repr()
        ))
    })?;
    Rcon::connect(&host, socket_port(&port)?, password, timeout)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths_like_pathlib() {
        let j = |d: &str| path_join(&PyStr::from(d), "server.properties").display();
        assert_eq!(
            j("/opt/minecraft/server"),
            "/opt/minecraft/server/server.properties"
        );
        assert_eq!(j(""), "server.properties");
        assert_eq!(j("//tmp/x/"), "//tmp/x/server.properties");
        assert_eq!(j("///tmp/./x//"), "/tmp/x/server.properties");
        assert_eq!(j("a/../b"), "a/../b/server.properties");
    }

    #[test]
    fn ports_like_getaddrinfo() {
        let p = |n: i128| socket_port(&PyInt::from_i128(n)).map_err(|e| e.to_string());
        assert_eq!(p(25575), Ok(25575));
        assert_eq!(p(65536 + 80), Ok(80));
        assert_eq!(
            p(-1),
            Err("[Errno -8] Servname not supported for ai_socktype".into())
        );
    }
}
