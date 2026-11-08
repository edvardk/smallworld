//! smallworld-core: what Smallworld's Rust programs share.
//!
//! - `rcon`, `mcrcon`, `properties`: mc_rcon.py's RCON client, `connect()` and server.properties reader
//!   (moved here from smallworld-rcon, whose behaviour and differential tests are unchanged);
//! - `pystr`, `error`, `errno`: Python's text rules (code points, `isspace`, `repr`) and exception classes;
//! - `paths`: the fixed paths the Python scripts hardcode;
//! - `fsx`: state files written and read like mc_panel.py does (`atomic_write`, ...);
//! - `flock`: the job lock and pending.json's lock, interlocking with Python's `fcntl.flock`;
//! - `audit`: the journald audit entry (native protocol over the socket, no libsystemd);
//! - `pyjson` (feature `json`): `json.dumps` byte for byte, `read_json`.
//!
//! Without features the crate is std only: smallworld-rcon uses it that way and stays dependency-free.

#![forbid(unsafe_code)]

pub mod audit;
pub mod errno;
pub mod error;
pub mod flock;
pub mod fsx;
pub mod mcrcon;
pub mod paths;
pub mod properties;
#[cfg(feature = "json")]
pub mod pyjson;
pub mod pystr;
pub mod rcon;
