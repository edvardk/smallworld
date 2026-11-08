//! mc_rcon.py `PersistentRcon` (one connection per server dir, shared by the metrics loop and `status`;
//! exponential backoff 1 s doubling to 30 s). The e2e `rcon` round checks the agent keeps one connection.
//! Built on smallworld_core::rcon::Rcon. Owner: data. Stub.
