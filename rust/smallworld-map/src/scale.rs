//! A region's 512×512 block columns → its map pixels at scales 0..=4, exactly as vanilla's `MapItem.update` computes
//! them (docs/reference.md "World map").
//!
//! A scale-`s` pixel covers `i × i` blocks (`i = 2^s`), aligned to multiples of `i` in world space, so a footprint
//! never leaves its chunk or its region. Per pixel: the most frequent colour of the footprint (ties: the colour seen
//! first, walking x outer and z inner, `LinkedHashMultiset` + `copyHighestCountFirst`), `d1` = mean height, and the
//! mean water depth (integer division). The brightness compares `d1` with the northern pixel's (`d0`) and dithers on
//! the parity of the pixel's world grid coordinates `(gx + gz) & 1`, which is what vanilla draws for any map whose
//! centre is on an even pixel (vanilla's own grid, and mc-brand's pocket map since v1.4).
//!
//! A pixel whose chunk isn't fully generated has no data (0). When the northern pixel has no data, its own `d1`
//! stands in for `d0` (only the dither remains).

use crate::chunk::Column;
use crate::palette::WATER;

pub const REGION: usize = 512;
pub const SCALES: u32 = 5;

/// A region's columns; `valid[i]` is false where the chunk isn't `full` (or failed to read).
#[derive(Clone)]
pub struct RegionColumns {
    pub cols: Vec<Column>,
    pub valid: Vec<bool>,
}

impl Default for RegionColumns {
    fn default() -> Self {
        RegionColumns {
            cols: vec![Column::default(); REGION * REGION],
            valid: vec![false; REGION * REGION],
        }
    }
}

impl RegionColumns {
    /// Stores one chunk's columns (`cx`, `cz` = chunk within the region, 0..32).
    pub fn set_chunk(&mut self, cx: usize, cz: usize, columns: Option<&[Column; 256]>) {
        for z in 0..16 {
            let row = (cz * 16 + z) * REGION + cx * 16;
            for x in 0..16 {
                match columns {
                    Some(c) => {
                        self.cols[row + x] = c[x + z * 16];
                        self.valid[row + x] = true;
                    }
                    None => {
                        self.cols[row + x] = Column::default();
                        self.valid[row + x] = false;
                    }
                }
            }
        }
    }

    #[must_use]
    pub fn any_valid(&self) -> bool {
        self.valid.iter().any(|&v| v)
    }
}

/// One pixel's aggregate: `(colour, d1, l2)`, `None` without data.
fn aggregate(cols: &RegionColumns, px: usize, pz: usize, i: usize) -> Option<(u8, f64, i64)> {
    let x0 = px * i;
    let z0 = pz * i;
    if !cols.valid[z0 * REGION + x0] {
        return None;
    }
    let mut counts = [0u32; 64];
    let mut order = [0u8; 256];
    let mut seen = 0usize;
    let mut sum_y: i64 = 0;
    let mut sum_depth: i64 = 0;
    for x in x0..x0 + i {
        for z in z0..z0 + i {
            let c = cols.cols[z * REGION + x];
            let k = usize::from(c.colour & 63);
            if counts[k] == 0 {
                order[seen] = k as u8;
                seen += 1;
            }
            counts[k] += 1;
            sum_y += i64::from(c.y);
            sum_depth += i64::from(c.depth);
        }
    }
    // Highest count first; among equal counts the first inserted wins.
    let mut best = order[0];
    for &k in &order[1..seen] {
        if counts[usize::from(k)] > counts[usize::from(best)] {
            best = k;
        }
    }
    let n = (i * i) as i64;
    Some((best, sum_y as f64 / n as f64, sum_depth / n))
}

/// `MapColor.Brightness` id: 0 LOW, 1 NORMAL, 2 HIGH.
#[must_use]
pub fn brightness(colour: u8, d1: f64, d0: f64, l2: i64, parity: i32, i: usize) -> u8 {
    if colour == WATER {
        let d2 = l2 as f64 * 0.1 + f64::from(parity) * 0.2;
        if d2 < 0.5 {
            2
        } else if d2 > 0.9 {
            0
        } else {
            1
        }
    } else {
        let d3 = (d1 - d0) * 4.0 / (i + 4) as f64 + (f64::from(parity) - 0.5) * 0.4;
        if d3 > 0.6 {
            2
        } else if d3 < -0.6 {
            0
        } else {
            1
        }
    }
}

/// One pixel's aggregate as cached for a neighbour: `(colour, d1, l2)`, `None` without data.
pub type Cell = Option<(u8, f64, i64)>;

/// A region's pixels at one scale plus the edge rows its neighbours need later.
pub struct Rendered {
    /// `(512 >> s)²` packed ids (`colour * 4 + brightness`, 0 = no data or NONE), rows north to south.
    pub pixels: Vec<u8>,
    /// The top row's aggregates, to re-shade it when the region to the north changes.
    pub top: Vec<Cell>,
    /// The bottom row's `d1`, the `d0` of the top row of the region to the south.
    pub bottom: Vec<Option<f64>>,
}

fn pack(colour: u8, b: u8) -> u8 {
    if colour == 0 { 0 } else { colour * 4 + b }
}

/// Region `(rx, rz)` at scale `s`. `north` is the bottom row `d1` of the region at `rz - 1` at this scale (`None`
/// when that region has no data).
#[must_use]
pub fn render(
    cols: &RegionColumns,
    north: Option<&[Option<f64>]>,
    rx: i32,
    rz: i32,
    s: u32,
) -> Rendered {
    let i = 1usize << s;
    let n = REGION >> s;
    let mut pixels = vec![0u8; n * n];
    let mut top = vec![None; n];
    let mut bottom = vec![None; n];
    for px in 0..n {
        let mut d0 = north.and_then(|row| row[px]);
        let gx = rx * n as i32 + px as i32;
        for pz in 0..n {
            let cell = aggregate(cols, px, pz, i);
            if pz == 0 {
                top[px] = cell;
            }
            let Some((colour, d1, l2)) = cell else {
                d0 = None;
                continue;
            };
            let gz = rz * n as i32 + pz as i32;
            let b = brightness(colour, d1, d0.unwrap_or(d1), l2, (gx + gz) & 1, i);
            pixels[pz * n + px] = pack(colour, b);
            d0 = Some(d1);
            if pz == n - 1 {
                bottom[px] = Some(d1);
            }
        }
    }
    Rendered {
        pixels,
        top,
        bottom,
    }
}

/// The top row of region `(rx, rz)` at scale `s` from its cached aggregates and the northern region's bottom row.
#[must_use]
pub fn shade_top(top: &[Cell], north: Option<&[Option<f64>]>, rx: i32, rz: i32, s: u32) -> Vec<u8> {
    let i = 1usize << s;
    let n = REGION >> s;
    (0..n)
        .map(|px| {
            let Some((colour, d1, l2)) = top[px] else {
                return 0;
            };
            let d0 = north.and_then(|row| row[px]).unwrap_or(d1);
            let parity = (rx * n as i32 + px as i32 + rz * n as i32) & 1;
            pack(colour, brightness(colour, d1, d0, l2, parity, i))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn flat(colour: u8, y: i16, depth: u16) -> RegionColumns {
        let mut r = RegionColumns::default();
        r.cols.fill(Column { colour, y, depth });
        r.valid.fill(true);
        r
    }

    #[test]
    fn water_depth_thresholds() {
        // Scale 0: d2 = depth*0.1 + parity*0.2; HIGH < 0.5 < NORMAL <= 0.9 < LOW.
        assert_eq!(brightness(WATER, 0.0, 0.0, 4, 0, 1), 2);
        assert_eq!(brightness(WATER, 0.0, 0.0, 4, 1, 1), 1);
        assert_eq!(brightness(WATER, 0.0, 0.0, 5, 0, 1), 1);
        assert_eq!(brightness(WATER, 0.0, 0.0, 9, 0, 1), 1);
        assert_eq!(brightness(WATER, 0.0, 0.0, 9, 1, 1), 0);
        assert_eq!(brightness(WATER, 0.0, 0.0, 10, 0, 1), 0);
    }

    #[test]
    fn flat_land_dithers_to_normal() {
        let r = flat(1, 64, 0);
        let first = render(&r, None, 0, 0, 0);
        let px = render(&r, Some(&first.bottom), 0, 1, 0).pixels;
        assert!(px.iter().all(|&p| p == 5));
        // Without a north region the top row uses its own d1: still NORMAL.
        assert_eq!(render(&r, None, 0, 0, 2).pixels[0], 5);
        // shade_top agrees with the full render's top row.
        let north = render(&r, None, 0, 0, 1).bottom;
        let full = render(&r, Some(&north), 0, 1, 1);
        assert_eq!(
            shade_top(&full.top, Some(&north), 0, 1, 1),
            full.pixels[..256]
        );
    }

    #[test]
    fn slopes_shade() {
        let mut r = flat(1, 64, 0);
        // Row z=1 one block higher than z=0: HIGH (d3 = 0.8 ± 0.2) at scale 0.
        for x in 0..REGION {
            r.cols[REGION + x].y = 65;
        }
        let px = render(&r, None, 0, 0, 0).pixels;
        assert_eq!(px[REGION], 6);
        assert_eq!(px[REGION + 1], 6);
        // And the row after it, back down: LOW.
        assert_eq!(px[2 * REGION], 4);
    }

    #[test]
    fn majority_ties_go_to_the_first_seen() {
        let mut r = flat(1, 64, 0);
        // Scale 1 pixel (0,0): x=0 column -> colours 7, 7 ; x=1 -> 2, 2. 7 seen first.
        r.cols[0].colour = 7;
        r.cols[REGION].colour = 7;
        r.cols[1].colour = 2;
        r.cols[REGION + 1].colour = 2;
        assert_eq!(render(&r, None, 0, 0, 1).pixels[0] >> 2, 7);
        // A clear majority wins regardless of order.
        r.cols[REGION + 1].colour = 7;
        assert_eq!(render(&r, None, 0, 0, 1).pixels[0] >> 2, 7);
    }

    #[test]
    fn invalid_chunks_have_no_data() {
        let mut r = flat(1, 64, 0);
        r.set_chunk(0, 0, None);
        let px = render(&r, None, 0, 0, 0).pixels;
        assert_eq!(px[0], 0);
        assert_eq!(px[16], 5);
    }
}
