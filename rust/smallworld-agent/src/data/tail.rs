//! `ConsoleTail` (mc_agent.py 940-1005): `journalctl -f -u minecraft.service -o json`, scrubbed lines in WS
//! batches of `TAIL_BATCH` (50), for 10-180 s. Owner: data (needs ship's Scrubber and link's WsSender). Stub.

pub const TAIL_MAX_S: i64 = 180;
pub const TAIL_BATCH: usize = 50;
