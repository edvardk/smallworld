//! mc_agent.py's WebSocket client (RFC 6455, client side; 1972-2149): `ws_accept`, `encode_frame`, `WebSocket`
//! (connect over rustls, handshake + accept check, masking, fragmentation, 1 MiB cap, ping/pong, close).
//! Owner: link. Stub.
//!
//! Design constraint (PORTING.md "risks"): the console tail sends while the session waits in `recv`. A rustls
//! stream is one state machine: one I/O thread owns it; senders go through a channel; short read timeouts.

/// mc_agent.py `WS_GUID`.
pub const WS_GUID: &str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
pub const WS_MAX_MESSAGE: usize = 1 << 20;
pub const WS_RECV_TIMEOUT_S: f64 = 15.0;
pub const WS_PING_S: f64 = 45.0;
pub const WS_DEAD_S: f64 = 120.0;

/// Opcodes.
pub const OP_CONT: u8 = 0x0;
pub const OP_TEXT: u8 = 0x1;
pub const OP_BIN: u8 = 0x2;
pub const OP_CLOSE: u8 = 0x8;
pub const OP_PING: u8 = 0x9;
pub const OP_PONG: u8 = 0xA;

/// `ws_accept(key)`: base64(sha1(key + GUID)). Stub.
pub fn ws_accept(_key: &str) -> String {
    String::new()
}

/// `encode_frame(opcode, payload, mask)`: one final, masked frame. Stub.
pub fn encode_frame(_opcode: u8, _payload: &[u8], _mask: [u8; 4]) -> Vec<u8> {
    Vec::new()
}

/// A sender for the session's text messages (the console tail's batches, results).
pub trait WsSender: Send + Sync {
    /// `ws.send_text(text)`; false if the session is gone.
    fn send_text(&self, text: &str) -> bool;
}
