//! smallworld-map: Smallworld's world map pre-renderer (v1.4). Reads a 26.x world's region files and writes, per
//! dimension and scale 0..=4, 512×512 tiles of the exact map colours vanilla's `MapItem.update` would draw, for
//! mc-brand's pocket map and spawn map room (docs/reference.md "World map", docs/rust/smallworld-map.md).
//!
//! std only, no `unsafe`, never `mmap`; malformed input is counted and skipped, never a panic (tests/fuzz.rs).

#![forbid(unsafe_code)]

pub mod chunk;
pub mod compress;
pub mod nbt;
pub mod palette;
pub mod region;
pub mod render;
pub mod scale;
pub mod sha256;
pub mod state;
pub mod tiles;
