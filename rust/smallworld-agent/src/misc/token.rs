//! mc_panel.py `TOKEN_RE` (`[A-Za-z0-9_.-]{20,4096}`), `token_shape` (never echoes the token), `TokenCache`
//! (`POST /agent/github-token`, reused until 5 min before `expires_at`, memory only, 401 invalidates once).
//! Owner: misc. Stub.

/// mc_panel.py `TOKEN_RE`'s pattern.
pub const TOKEN_PATTERN: &str = "^[A-Za-z0-9_.-]{20,4096}$";
