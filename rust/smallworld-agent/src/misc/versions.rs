//! mc_agent.py `read_product` (regex-checked product-version.json), `product_started` (read once at start),
//! `product_state` (installed + running, for the inventory). Owner: misc. Stub.

use crate::types::Obj;

/// `read_product(path)`. Stub.
pub fn read_product(_path: &std::path::Path) -> Option<Obj> {
    None
}
