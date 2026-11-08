//! mc_agent.py `lookup_urls`, `Lookup` (Java name -> UUID from the box: api.mojang.com, then
//! api.minecraftservices.com, 3 s timeout each, 20 per minute, cached 10 min, 200 entries; only the name is
//! sent). Owner: misc. Stub.

pub const MOJANG_LOOKUPS: [&str; 2] = [
    "https://api.mojang.com/users/profiles/minecraft/",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/",
];
pub const LOOKUP_TIMEOUT_S: f64 = 3.0;
pub const LOOKUP_TTL_S: f64 = 600.0;
pub const LOOKUP_PER_MINUTE: usize = 20;
