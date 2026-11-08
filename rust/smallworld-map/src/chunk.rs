//! One chunk's NBT (DataVersion 5023, 26.x) → the 256 block columns vanilla's map looks at.
//!
//! Read: `DataVersion`, `Status`, `xPos`/`zPos`/`yPos`, `Heightmaps.WORLD_SURFACE` and `sections[].{Y,
//! block_states}`. Palette entries are 26.x's forms: a bare block id (its default state; `{"": id}` inside a
//! heterogeneous list) or `{id, properties?}` (missing properties take the default's value). The pre-26 form
//! `{Name, Properties}` is accepted too.
//!
//! The walk per column is `MapItem.update`'s (docs/reference.md "World map"): start one above the WORLD_SURFACE
//! height; at or below the floor + 1 the column is bedrock (STONE); otherwise step down while the state's map colour
//! is NONE and y > floor; if the state found holds a fluid, count the fluid blocks below it (the first one
//! unconditionally) and record the state's `surface` colour, else its own colour.

use crate::nbt::{self, Compound, Value, long_at};
use crate::palette::{Palette, StateInfo};

/// One block column as the map sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Column {
    /// MapColor id recorded for the column.
    pub colour: u8,
    /// The y the walk stopped at (vanilla's `k3`, summed into the pixel's height).
    pub y: i16,
    /// Fluid depth below the surface block.
    pub depth: u16,
}

/// What a chunk contributes.
#[derive(Debug)]
pub enum Outcome {
    /// `Status == minecraft:full`: 256 columns, index `x + z * 16`.
    Full {
        columns: Box<[Column; 256]>,
        data_version: i32,
        unknown: u32,
        x: i32,
        z: i32,
    },
    /// A proto-chunk (any other status): nothing to draw, not an error.
    NotFull { x: i32, z: i32 },
    /// Written by another game version than the palette's (an older chunk the server hasn't upgraded yet, or a
    /// newer game): skipped, as tests/map_reference.py does.
    OtherVersion(i32),
    /// Missing or malformed fields.
    Invalid(&'static str),
}

/// `ceil(log2(n))` for n >= 1.
#[must_use]
pub fn ceil_log2(n: usize) -> u32 {
    if n <= 1 {
        0
    } else {
        usize::BITS - (n - 1).leading_zeros()
    }
}

/// A `SimpleBitStorage` view: values never span longs.
#[derive(Clone, Copy)]
struct Bits<'a> {
    raw: &'a [u8],
    bits: u32,
    per_long: usize,
}

impl<'a> Bits<'a> {
    fn new(raw: &'a [u8], bits: u32, size: usize) -> Option<Bits<'a>> {
        if bits == 0 || bits > 32 {
            return None;
        }
        let per_long = (64 / bits) as usize;
        let longs = size.div_ceil(per_long);
        (raw.len() == longs * 8).then_some(Bits {
            raw,
            bits,
            per_long,
        })
    }

    fn get(&self, i: usize) -> usize {
        let v = long_at(self.raw, i / self.per_long) >> ((i % self.per_long) as u32 * self.bits);
        (v & ((1u64 << self.bits) - 1)) as usize
    }
}

struct Section<'a> {
    states: Vec<StateInfo>,
    data: Option<Bits<'a>>,
}

impl Section<'_> {
    fn at(&self, i: usize) -> StateInfo {
        match &self.data {
            None => self.states[0],
            Some(b) => self.states[b.get(i)],
        }
    }
}

fn utf8(s: &[u8]) -> Option<&str> {
    std::str::from_utf8(s).ok()
}

/// One palette entry → its state (`None`: unknown to the palette, drawn as air).
fn entry_state(palette: &Palette, entry: &Value<'_>) -> Result<Option<StateInfo>, &'static str> {
    match nbt::unwrap_list_element(entry) {
        Value::String(s) => Ok(utf8(s).and_then(|n| palette.default_state(n))),
        Value::Compound(c) => {
            let name = c.string("id").ok_or("palette entry without an id")?;
            let Some(name) = utf8(name) else {
                return Ok(None);
            };
            let mut pairs: Vec<(&str, &str)> = Vec::new();
            if let Some(p) = c.compound("properties") {
                for (k, v) in &p.entries {
                    match (utf8(k), v) {
                        (Some(k), Value::String(v)) => {
                            pairs.push((k, utf8(v).unwrap_or("\u{fffd}")));
                        }
                        _ => return Ok(None),
                    }
                }
            }
            Ok(palette.state(name, &pairs))
        }
        _ => Err("unsupported palette entry"),
    }
}

fn section<'a>(
    palette: &Palette,
    bs: &Compound<'a>,
    unknown: &mut u32,
) -> Result<Section<'a>, &'static str> {
    let entries = bs.list("palette").ok_or("block_states without a palette")?;
    if entries.is_empty() {
        return Err("empty block palette");
    }
    let mut states: Vec<StateInfo> = Vec::with_capacity(entries.len());
    for e in entries {
        states.push(entry_state(palette, e)?.unwrap_or_else(|| {
            *unknown += 1;
            StateInfo::default()
        }));
    }
    if states.len() == 1 {
        return Ok(Section { states, data: None });
    }
    let raw = bs.long_array("data").ok_or("block_states without data")?;
    let bits = ceil_log2(states.len()).max(4);
    let data = Bits::new(raw, bits, 4096).ok_or("block_states data length")?;
    if (0..4096).any(|i| data.get(i) >= states.len()) {
        return Err("block index outside the palette");
    }
    Ok(Section {
        states,
        data: Some(data),
    })
}

/// Heightmap bits from its long count; when the count fits several widths (52 longs: 11 or 12 bits, ...) the
/// dimension's height decides (`ceil_log2(height + 1)`), and if that isn't one of them the chunk is malformed.
fn heightmap_bits(longs: usize, height: usize) -> Option<u32> {
    let fits: Vec<u32> = (1..=32u32)
        .filter(|&b| 256usize.div_ceil((64 / b) as usize) == longs)
        .collect();
    if fits.len() == 1 {
        return Some(fits[0]);
    }
    let want = ceil_log2(height + 1);
    fits.contains(&want).then_some(want)
}

/// Decodes one chunk.
#[must_use]
pub fn decode(palette: &Palette, data: &[u8]) -> Outcome {
    let Ok(root) = nbt::parse(data) else {
        return Outcome::Invalid("malformed NBT");
    };
    let Some(dv) = root.int("DataVersion") else {
        return Outcome::Invalid("no DataVersion");
    };
    let (Some(x), Some(z)) = (root.int("xPos"), root.int("zPos")) else {
        return Outcome::Invalid("no xPos/zPos");
    };
    if root.string("Status") != Some(b"minecraft:full") {
        return Outcome::NotFull { x, z };
    }
    if dv != palette.data_version {
        return Outcome::OtherVersion(dv);
    }
    let Some(y_pos) = root.int("yPos") else {
        return Outcome::Invalid("no yPos");
    };
    // Vanilla's build limits: min_y >= -2032, height <= 4064 (DimensionType). Anything else is malformed, and the
    // bounds keep the arithmetic and the walk below small.
    if !(-127..=127).contains(&y_pos) {
        return Outcome::Invalid("yPos out of range");
    }
    let min_y = y_pos * 16;
    let Some(hm) = root
        .compound("Heightmaps")
        .and_then(|h| h.long_array("WORLD_SURFACE"))
    else {
        return Outcome::Invalid("no WORLD_SURFACE heightmap");
    };
    let list = root.list("sections").unwrap_or_default();

    let mut unknown = 0u32;
    let mut sections: Vec<(i32, Section<'_>)> = Vec::new();
    for s in list {
        let Value::Compound(s) = s else {
            return Outcome::Invalid("section is not a compound");
        };
        // Light-only sections (above and below the world) have no block_states.
        let Some(bs) = s.compound("block_states") else {
            continue;
        };
        let Some(sy) = s.int("Y") else {
            return Outcome::Invalid("section without Y");
        };
        match section(palette, bs, &mut unknown) {
            Ok(sec) => sections.push((sy, sec)),
            Err(why) => return Outcome::Invalid(why),
        }
    }
    let top_section = sections
        .iter()
        .map(|(y, _)| *y)
        .max()
        .unwrap_or(y_pos)
        .min(y_pos + 254);
    let height = ((top_section - y_pos + 1).max(1) * 16) as usize;
    let Some(hbits) = heightmap_bits(hm.len() / 8, height) else {
        return Outcome::Invalid("heightmap length");
    };
    let Some(heights) = Bits::new(hm, hbits, 256) else {
        return Outcome::Invalid("heightmap length");
    };

    // Sections by index (sy - y_pos); out-of-range ones can't be reached by the walk (it never goes below min_y).
    let mut by_index: Vec<Option<Section<'_>>> = Vec::new();
    for (sy, sec) in sections {
        let Ok(i) = usize::try_from(sy - y_pos) else {
            continue;
        };
        if i >= 4096 {
            continue;
        }
        if by_index.len() <= i {
            by_index.resize_with(i + 1, || None);
        }
        by_index[i] = Some(sec);
    }
    let at = |x: usize, y: i32, z: usize| -> StateInfo {
        let Ok(i) = usize::try_from((y >> 4) - y_pos) else {
            return StateInfo::default();
        };
        match by_index.get(i) {
            Some(Some(sec)) => sec.at((((y & 15) as usize) * 16 + z) * 16 + x),
            _ => StateInfo::default(),
        }
    };

    if (0..256).any(|i| heights.get(i) > height) {
        return Outcome::Invalid("heightmap above the world");
    }
    let mut columns = Box::new([Column::default(); 256]);
    for zz in 0..16 {
        for xx in 0..16 {
            columns[xx + zz * 16] = walk(
                heights.get(xx + zz * 16) as i32 + min_y,
                min_y,
                palette.bedrock,
                |y| at(xx, y, zz),
            );
        }
    }
    Outcome::Full {
        columns,
        data_version: dv,
        unknown,
        x,
        z,
    }
}

/// `MapItem.update`'s per-column walk. `top` is one above the highest WORLD_SURFACE block (the heightmap value +
/// the floor).
pub fn walk(top: i32, min_y: i32, bedrock: u8, at: impl Fn(i32) -> StateInfo) -> Column {
    let mut y = top;
    if y <= min_y + 1 {
        return Column {
            colour: bedrock,
            y: y as i16,
            depth: 0,
        };
    }
    let mut state;
    loop {
        y -= 1;
        state = at(y);
        if !(state.colour == 0 && y > min_y) {
            break;
        }
    }
    if y > min_y && state.fluid {
        let mut depth: u32 = 0;
        let mut y2 = y - 1;
        loop {
            let below = at(y2);
            y2 -= 1;
            depth += 1;
            if !(y2 > min_y && below.fluid) {
                break;
            }
        }
        return Column {
            colour: state.surface,
            y: y as i16,
            depth: depth.min(u32::from(u16::MAX)) as u16,
        };
    }
    Column {
        colour: state.colour,
        y: y as i16,
        depth: 0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(colour: u8, fluid: bool, surface: u8) -> StateInfo {
        StateInfo {
            colour,
            fluid,
            surface,
        }
    }

    #[test]
    fn walk_matches_vanilla() {
        // Empty column: bedrock at the floor.
        assert_eq!(
            walk(-64, -64, 11, |_| s(0, false, 0)),
            Column {
                colour: 11,
                y: -64,
                depth: 0
            }
        );
        assert_eq!(walk(-63, -64, 11, |_| s(0, false, 0)).colour, 11);
        // Glass (NONE) over grass: looks through.
        let c = walk(65, -64, 11, |y| {
            if y == 64 {
                s(0, false, 0)
            } else {
                s(1, false, 1)
            }
        });
        assert_eq!(
            c,
            Column {
                colour: 1,
                y: 63,
                depth: 0
            }
        );
        // Water 3 deep over sand: depth counts the surface block's fluid neighbours below (first unconditionally).
        let c = walk(64, -64, 11, |y| {
            if y >= 61 {
                s(12, true, 12)
            } else {
                s(2, false, 2)
            }
        });
        assert_eq!(
            c,
            Column {
                colour: 12,
                y: 63,
                depth: 3
            }
        );
        // Waterlogged slab records WATER; waterlogged stairs keep theirs.
        assert_eq!(
            walk(64, -64, 11, |y| if y == 63 {
                s(13, true, 12)
            } else {
                s(2, false, 2)
            })
            .colour,
            12
        );
        assert_eq!(
            walk(64, -64, 11, |y| if y == 63 {
                s(13, true, 13)
            } else {
                s(2, false, 2)
            })
            .colour,
            13
        );
        // All see-through down to the floor: stops at the floor with NONE.
        assert_eq!(
            walk(10, 0, 11, |_| s(0, false, 0)),
            Column {
                colour: 0,
                y: 0,
                depth: 0
            }
        );
    }

    #[test]
    fn bits_rules() {
        assert_eq!(ceil_log2(1), 0);
        assert_eq!(ceil_log2(6), 3);
        assert_eq!(ceil_log2(29), 5);
        assert_eq!(heightmap_bits(37, 384), Some(9));
        assert_eq!(heightmap_bits(37, 256), Some(9));
        assert_eq!(heightmap_bits(52, 4064), Some(12));
        assert_eq!(heightmap_bits(52, 2032), Some(11));
        assert_eq!(heightmap_bits(3, 384), None);
    }
}
