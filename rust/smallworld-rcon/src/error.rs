//! The errors `mc_rcon.py` can end with, one variant per Python exception class.
//!
//! In Python an error is an exception: it is raised, flies up through the callers until an `except` catches it,
//! and if nothing does, Python prints a traceback whose last line is `ClassName: message` and exits with 1.
//! In Rust an error is a value: a function that can fail returns `Result<T, PyError>`, which is either
//! `Ok(value)` or `Err(error)`, and the `?` operator after a call means "if this is an `Err`, return it from
//! my function too" (the rest of the function is skipped, exactly like an exception flying past).
//!
//! `mc_rcon.py`'s operations catch `(OSError, RconError)` and turn them into a message; that is
//! `PyError::is_caught`. Anything else ends the program: `main.rs` prints `traceback_line()`, the same last line
//! Python's traceback ends with, and exits with 1.

use crate::errno;
use crate::pystr::PyStr;
use std::fmt;
use std::io;

/// One Python exception. An `enum` is a type whose value is exactly one of its variants; each variant can carry
/// its own data (Python would use one class per variant).
#[derive(Debug)]
pub enum PyError {
    /// `RconError(message)` (mc_rcon.py's own errors).
    Rcon(String),
    /// `RconAuthError("RCON authentication failed")`: the server refused the password.
    RconAuth,
    /// `OSError` and its subclasses: an error number, its text, and the file it was about (if any).
    Os {
        errno: i32,
        strerror: String,
        filename: Option<PyStr>,
    },
    /// `TimeoutError("timed out")`: a socket operation took longer than its timeout.
    Timeout,
    /// `socket.gaierror`: the host or port couldn't be resolved.
    Gai { code: i32, message: String },
    /// `OSError` without an error number (an I/O error Rust reports in its own words).
    OsOther(String),
    /// `KeyError(key)`: a setting missing from server.properties.
    Key(PyStr),
    /// `ValueError(message)`.
    Value(String),
    /// `OverflowError(message)`.
    Overflow(String),
    /// `struct.error(message)`: a reply packet too short to hold its header.
    Struct(String),
    /// `UnicodeDecodeError`: server.properties is not UTF-8.
    UnicodeDecode(String),
    /// `UnicodeEncodeError`: text with lone surrogates can't be sent.
    UnicodeEncode(String),
    /// `ModuleNotFoundError`: the Discord helper (notify.py) is missing.
    ModuleNotFound(String),
    /// The Discord helper failed and already printed its own traceback: exit 1 without another line.
    HelperFailed,
}

impl PyError {
    /// An `OSError` from an I/O error, the way Python would have raised it for the same system call.
    pub fn from_io(e: &io::Error) -> PyError {
        Self::from_io_with_path(e, None)
    }

    /// Same, for an error about a file (Python then appends `: 'path'` to the message).
    pub fn from_io_with_path(e: &io::Error, filename: Option<&PyStr>) -> PyError {
        // A socket timeout: Rust sees EAGAIN (a read past SO_RCVTIMEO) or its own "timed out" (connect).
        if matches!(
            e.kind(),
            io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
        ) && filename.is_none()
        {
            return PyError::Timeout;
        }
        match e.raw_os_error() {
            Some(n) => PyError::Os {
                errno: n,
                strerror: errno::strerror(n),
                filename: filename.cloned(),
            },
            None => PyError::OsOther(e.to_string()),
        }
    }

    /// Whether `except (OSError, RconError)` in mc_rcon.py catches this one.
    pub fn is_caught(&self) -> bool {
        matches!(
            self,
            PyError::Rcon(_)
                | PyError::RconAuth
                | PyError::Os { .. }
                | PyError::Timeout
                | PyError::Gai { .. }
                | PyError::OsOther(_)
        )
    }

    /// The exception's class as a traceback names it: `module.Class`, except for built-in classes and those of
    /// the script itself (module `__main__`, where mc_rcon.py's own classes live), which are named alone.
    pub fn class_name(&self) -> String {
        match self {
            PyError::Rcon(_) => "RconError".into(),
            PyError::RconAuth => "RconAuthError".into(),
            PyError::Os { errno, .. } => errno::oserror_class(*errno).into(),
            PyError::Timeout => "TimeoutError".into(),
            PyError::Gai { .. } => "socket.gaierror".into(),
            PyError::OsOther(_) | PyError::HelperFailed => "OSError".into(),
            PyError::Key(_) => "KeyError".into(),
            PyError::Value(_) => "ValueError".into(),
            PyError::Overflow(_) => "OverflowError".into(),
            PyError::Struct(_) => "struct.error".into(),
            PyError::UnicodeDecode(_) => "UnicodeDecodeError".into(),
            PyError::UnicodeEncode(_) => "UnicodeEncodeError".into(),
            PyError::ModuleNotFound(_) => "ModuleNotFoundError".into(),
        }
    }

    /// The last line of the traceback Python prints for this exception when nothing catches it.
    pub fn traceback_line(&self) -> String {
        let message = self.to_string();
        if message.is_empty() {
            self.class_name()
        } else {
            format!("{}: {message}", self.class_name())
        }
    }
}

/// `Display` is Rust's `__str__`: `error.to_string()` and `format!("{error}")` use it. This is `str(e)` in Python.
impl fmt::Display for PyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PyError::Rcon(m)
            | PyError::Value(m)
            | PyError::Overflow(m)
            | PyError::Struct(m)
            | PyError::UnicodeDecode(m)
            | PyError::UnicodeEncode(m)
            | PyError::ModuleNotFound(m)
            | PyError::OsOther(m) => f.write_str(m),
            PyError::RconAuth => f.write_str("RCON authentication failed"),
            PyError::Os {
                errno,
                strerror,
                filename: None,
            } => write!(f, "[Errno {errno}] {strerror}"),
            PyError::Os {
                errno,
                strerror,
                filename: Some(name),
            } => {
                write!(f, "[Errno {errno}] {strerror}: {}", name.repr())
            }
            PyError::Timeout => f.write_str("timed out"),
            PyError::Gai { code, message } => write!(f, "[Errno {code}] {message}"),
            // str(KeyError('rcon.password')) is the key's repr.
            PyError::Key(key) => f.write_str(&key.repr()),
            PyError::HelperFailed => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn traceback_lines_match_python() {
        let refused = PyError::from_io(&io::Error::from_raw_os_error(111));
        assert_eq!(
            refused.traceback_line(),
            "ConnectionRefusedError: [Errno 111] Connection refused"
        );
        assert!(refused.is_caught());
        let missing = PyError::from_io_with_path(
            &io::Error::from_raw_os_error(2),
            Some(&PyStr::from("/x/server.properties")),
        );
        assert_eq!(
            missing.traceback_line(),
            "FileNotFoundError: [Errno 2] No such file or directory: '/x/server.properties'"
        );
        assert_eq!(
            PyError::Key(PyStr::from("rcon.password")).traceback_line(),
            "KeyError: 'rcon.password'"
        );
        assert!(!PyError::Key(PyStr::new()).is_caught());
        assert_eq!(
            PyError::RconAuth.traceback_line(),
            "RconAuthError: RCON authentication failed"
        );
        let timeout = PyError::from_io(&io::Error::from(io::ErrorKind::WouldBlock));
        assert_eq!(timeout.traceback_line(), "TimeoutError: timed out");
    }
}
