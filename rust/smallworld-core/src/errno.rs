//! Error numbers as Python reports them on the server.
//!
//! Python formats an `OSError` as `[Errno 111] Connection refused`, with the C library's text for the number.
//! The server's Python uses glibc, but this binary is linked statically against musl, whose texts differ for some
//! numbers (`ELOOP` is "Too many levels of symbolic links" in glibc, "Symbolic link loop" in musl). So the
//! glibc texts are copied here, and Python's subclass of `OSError` for each number too.
//!
//! The table was generated with `python3 -c 'import os; print([os.strerror(i) for i in range(1, 134)])'` on
//! glibc; `tests/test_rust_rcon.py` checks it against `os.strerror` on every run.

/// `GLIBC_STRERROR[n - 1]` is glibc's `strerror(n)` for Linux error numbers 1 to 133.
const GLIBC_STRERROR: [&str; 133] = [
    "Operation not permitted",
    "No such file or directory",
    "No such process",
    "Interrupted system call",
    "Input/output error",
    "No such device or address",
    "Argument list too long",
    "Exec format error",
    "Bad file descriptor",
    "No child processes",
    "Resource temporarily unavailable",
    "Cannot allocate memory",
    "Permission denied",
    "Bad address",
    "Block device required",
    "Device or resource busy",
    "File exists",
    "Invalid cross-device link",
    "No such device",
    "Not a directory",
    "Is a directory",
    "Invalid argument",
    "Too many open files in system",
    "Too many open files",
    "Inappropriate ioctl for device",
    "Text file busy",
    "File too large",
    "No space left on device",
    "Illegal seek",
    "Read-only file system",
    "Too many links",
    "Broken pipe",
    "Numerical argument out of domain",
    "Numerical result out of range",
    "Resource deadlock avoided",
    "File name too long",
    "No locks available",
    "Function not implemented",
    "Directory not empty",
    "Too many levels of symbolic links",
    "Unknown error 41",
    "No message of desired type",
    "Identifier removed",
    "Channel number out of range",
    "Level 2 not synchronized",
    "Level 3 halted",
    "Level 3 reset",
    "Link number out of range",
    "Protocol driver not attached",
    "No CSI structure available",
    "Level 2 halted",
    "Invalid exchange",
    "Invalid request descriptor",
    "Exchange full",
    "No anode",
    "Invalid request code",
    "Invalid slot",
    "Unknown error 58",
    "Bad font file format",
    "Device not a stream",
    "No data available",
    "Timer expired",
    "Out of streams resources",
    "Machine is not on the network",
    "Package not installed",
    "Object is remote",
    "Link has been severed",
    "Advertise error",
    "Srmount error",
    "Communication error on send",
    "Protocol error",
    "Multihop attempted",
    "RFS specific error",
    "Bad message",
    "Value too large for defined data type",
    "Name not unique on network",
    "File descriptor in bad state",
    "Remote address changed",
    "Can not access a needed shared library",
    "Accessing a corrupted shared library",
    ".lib section in a.out corrupted",
    "Attempting to link in too many shared libraries",
    "Cannot exec a shared library directly",
    "Invalid or incomplete multibyte or wide character",
    "Interrupted system call should be restarted",
    "Streams pipe error",
    "Too many users",
    "Socket operation on non-socket",
    "Destination address required",
    "Message too long",
    "Protocol wrong type for socket",
    "Protocol not available",
    "Protocol not supported",
    "Socket type not supported",
    "Operation not supported",
    "Protocol family not supported",
    "Address family not supported by protocol",
    "Address already in use",
    "Cannot assign requested address",
    "Network is down",
    "Network is unreachable",
    "Network dropped connection on reset",
    "Software caused connection abort",
    "Connection reset by peer",
    "No buffer space available",
    "Transport endpoint is already connected",
    "Transport endpoint is not connected",
    "Cannot send after transport endpoint shutdown",
    "Too many references: cannot splice",
    "Connection timed out",
    "Connection refused",
    "Host is down",
    "No route to host",
    "Operation already in progress",
    "Operation now in progress",
    "Stale file handle",
    "Structure needs cleaning",
    "Not a XENIX named type file",
    "No XENIX semaphores available",
    "Is a named type file",
    "Remote I/O error",
    "Disk quota exceeded",
    "No medium found",
    "Wrong medium type",
    "Operation canceled",
    "Required key not available",
    "Key has expired",
    "Key has been revoked",
    "Key was rejected by service",
    "Owner died",
    "State not recoverable",
    "Operation not possible due to RF-kill",
    "Memory page has hardware error",
];

/// glibc's text for an error number, as Python's `os.strerror` returns it.
pub fn strerror(errno: i32) -> String {
    usize::try_from(errno)
        .ok()
        .and_then(|n| n.checked_sub(1))
        .and_then(|i| GLIBC_STRERROR.get(i))
        .map_or_else(|| format!("Unknown error {errno}"), |s| (*s).to_string())
}

/// The `OSError` subclass Python raises for an error number (PEP 3151), e.g. `ConnectionRefusedError`.
pub fn oserror_class(errno: i32) -> &'static str {
    match errno {
        1 | 13 => "PermissionError",         // EPERM, EACCES
        2 => "FileNotFoundError",            // ENOENT
        3 => "ProcessLookupError",           // ESRCH
        4 => "InterruptedError",             // EINTR
        10 => "ChildProcessError",           // ECHILD
        11 | 114 | 115 => "BlockingIOError", // EAGAIN, EALREADY, EINPROGRESS
        17 => "FileExistsError",             // EEXIST
        20 => "NotADirectoryError",          // ENOTDIR
        21 => "IsADirectoryError",           // EISDIR
        32 | 108 => "BrokenPipeError",       // EPIPE, ESHUTDOWN
        103 => "ConnectionAbortedError",     // ECONNABORTED
        104 => "ConnectionResetError",       // ECONNRESET
        110 => "TimeoutError",               // ETIMEDOUT
        111 => "ConnectionRefusedError",     // ECONNREFUSED
        _ => "OSError",
    }
}

/// `type(e).__name__` for the `OSError` Python raises for this I/O error (a file operation: never a timeout).
pub fn io_class(e: &std::io::Error) -> &'static str {
    e.raw_os_error().map_or("OSError", oserror_class)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glibc_texts_and_classes() {
        assert_eq!(strerror(111), "Connection refused");
        assert_eq!(strerror(40), "Too many levels of symbolic links");
        assert_eq!(strerror(0), "Unknown error 0");
        assert_eq!(strerror(500), "Unknown error 500");
        assert_eq!(oserror_class(111), "ConnectionRefusedError");
        assert_eq!(oserror_class(2), "FileNotFoundError");
        assert_eq!(oserror_class(22), "OSError");
    }
}
