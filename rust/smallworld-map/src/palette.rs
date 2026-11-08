//! The block palette: every block state's map colour, generated at build time from the pinned server jar
//! (`scripts/map_palette.py` → `server-config/keys/<version>/map-palette.tsv`, format in docs/reference.md
//! "World map").
//!
//! ```text
//! M  key    value                                  metadata (format, minecraft_version, data_version, ...)
//! D  block  properties  colour  fluid  surface     a block's default state
//! S  block  properties  colour  fluid  surface     a state of that block whose values differ from the D line
//! ```
//!
//! `properties` is `k=v` pairs sorted by name and joined with commas, `-` for none. `colour` is the state's
//! `MapColor` id (0 = NONE: the map looks through it), `fluid` 1 when it holds a fluid, `surface` the colour the map
//! records when it stops on it (a fluid without a sturdy top face counts as its fluid's block).

use std::collections::HashMap;
use std::fmt;

/// What the map needs to know about one block state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct StateInfo {
    pub colour: u8,
    pub fluid: bool,
    pub surface: u8,
}

/// `MapColor.STONE`, what vanilla draws for a column at or below the world's floor (`Blocks.BEDROCK`).
pub const BEDROCK_FALLBACK: u8 = 11;
/// `MapColor.WATER`.
pub const WATER: u8 = 12;

struct Block {
    default_props: Vec<(String, String)>,
    default_key: String,
    default: StateInfo,
    states: HashMap<String, StateInfo>,
}

pub struct Palette {
    blocks: HashMap<String, Block>,
    pub data_version: i32,
    pub minecraft_version: String,
    /// FNV-1a of the file: a different palette invalidates every cached column.
    pub fingerprint: u64,
    pub bedrock: u8,
}

#[derive(Debug)]
pub struct PaletteError(pub String);

impl fmt::Display for PaletteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "palette: {}", self.0)
    }
}

impl std::error::Error for PaletteError {}

#[must_use]
pub fn fnv1a(data: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in data {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

fn parse_props(s: &str) -> Result<Vec<(String, String)>, PaletteError> {
    if s == "-" {
        return Ok(Vec::new());
    }
    s.split(',')
        .map(|kv| {
            kv.split_once('=')
                .map(|(k, v)| (k.to_owned(), v.to_owned()))
                .ok_or_else(|| PaletteError(format!("bad properties {s:?}")))
        })
        .collect()
}

fn join_props<'a>(props: impl Iterator<Item = (&'a str, &'a str)>) -> String {
    let mut out = String::new();
    for (k, v) in props {
        if !out.is_empty() {
            out.push(',');
        }
        out.push_str(k);
        out.push('=');
        out.push_str(v);
    }
    out
}

impl Palette {
    /// Parses the palette file's text.
    ///
    /// # Errors
    /// A malformed line, an S line before its block's D line, or a missing `data_version`.
    pub fn parse(text: &str) -> Result<Palette, PaletteError> {
        let mut blocks: HashMap<String, Block> = HashMap::new();
        let mut data_version = None;
        let mut minecraft_version = String::new();
        for (n, line) in text.lines().enumerate() {
            if line.starts_with('#') || line.is_empty() {
                continue;
            }
            let f: Vec<&str> = line.split('\t').collect();
            let bad = || PaletteError(format!("line {}: {line:?}", n + 1));
            match f[0] {
                "M" if f.len() == 3 => match f[1] {
                    "data_version" => data_version = Some(f[2].parse::<i32>().map_err(|_| bad())?),
                    "minecraft_version" => f[2].clone_into(&mut minecraft_version),
                    "format" if f[2] != "1" => {
                        return Err(PaletteError(format!("unknown format {}", f[2])));
                    }
                    _ => {}
                },
                "D" | "S" if f.len() == 6 => {
                    let info = StateInfo {
                        colour: f[3].parse().map_err(|_| bad())?,
                        fluid: f[4] == "1",
                        surface: f[5].parse().map_err(|_| bad())?,
                    };
                    if info.colour > 63 || info.surface > 63 {
                        return Err(bad());
                    }
                    let key = if f[2] == "-" {
                        String::new()
                    } else {
                        f[2].to_owned()
                    };
                    if f[0] == "D" {
                        blocks.insert(
                            f[1].to_owned(),
                            Block {
                                default_props: parse_props(f[2])?,
                                default_key: key,
                                default: info,
                                states: HashMap::new(),
                            },
                        );
                    } else {
                        blocks
                            .get_mut(f[1])
                            .ok_or_else(bad)?
                            .states
                            .insert(key, info);
                    }
                }
                _ => return Err(bad()),
            }
        }
        let data_version = data_version.ok_or_else(|| PaletteError("no data_version".into()))?;
        let bedrock = blocks
            .get("minecraft:bedrock")
            .map_or(BEDROCK_FALLBACK, |b| b.default.colour);
        Ok(Palette {
            blocks,
            data_version,
            minecraft_version,
            fingerprint: fnv1a(text.as_bytes()),
            bedrock,
        })
    }

    /// A block's default state (a bare id in a 26.x palette). `None`: unknown block.
    #[must_use]
    pub fn default_state(&self, name: &str) -> Option<StateInfo> {
        self.blocks.get(name).map(|b| b.default)
    }

    /// A state given as `{id, properties}`: properties missing from the entry take the default state's value
    /// (`StateDefinition.appendPropertyCodec`: `fieldOf(name).orElseGet(default)`), unknown ones are ignored.
    #[must_use]
    pub fn state<'a>(&self, name: &str, props: &[(&'a str, &'a str)]) -> Option<StateInfo> {
        let block = self.blocks.get(name)?;
        if props.is_empty() {
            return Some(block.default);
        }
        let merged = block.default_props.iter().map(|(k, v)| {
            let given = props
                .iter()
                .find(|(pk, _)| pk == k)
                .map_or(v.as_str(), |(_, pv)| pv);
            (k.as_str(), given)
        });
        let key = join_props(merged);
        if key == block.default_key {
            return Some(block.default);
        }
        Some(block.states.get(&key).copied().unwrap_or(block.default))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEXT: &str = "# x\nM\tformat\t1\nM\tdata_version\t5023\n\
        D\tminecraft:air\t-\t0\t0\t0\n\
        D\tminecraft:oak_stairs\tfacing=north,half=bottom,shape=straight,waterlogged=false\t13\t0\t13\n\
        S\tminecraft:oak_stairs\tfacing=north,half=bottom,shape=straight,waterlogged=true\t13\t1\t13\n\
        D\tminecraft:oak_slab\ttype=bottom,waterlogged=false\t13\t0\t13\n\
        S\tminecraft:oak_slab\ttype=bottom,waterlogged=true\t13\t1\t12\n";

    #[test]
    fn merges_missing_properties_over_the_default() {
        let p = Palette::parse(TEXT).unwrap();
        assert_eq!(p.data_version, 5023);
        assert_eq!(p.bedrock, BEDROCK_FALLBACK);
        let slab = p
            .state("minecraft:oak_slab", &[("waterlogged", "true")])
            .unwrap();
        assert_eq!(
            slab,
            StateInfo {
                colour: 13,
                fluid: true,
                surface: 12
            }
        );
        let other = p
            .state("minecraft:oak_slab", &[("type", "top"), ("bogus", "1")])
            .unwrap();
        assert_eq!(other.surface, 13);
        assert_eq!(p.default_state("minecraft:air"), Some(StateInfo::default()));
        assert!(p.state("minecraft:nope", &[]).is_none());
    }

    #[test]
    fn refuses_malformed() {
        assert!(Palette::parse("D\tx\n").is_err());
        assert!(Palette::parse("S\tminecraft:a\t-\t1\t0\t1\nM\tdata_version\t1\n").is_err());
        assert!(Palette::parse("D\tminecraft:a\t-\t1\t0\t1\n").is_err());
    }
}
