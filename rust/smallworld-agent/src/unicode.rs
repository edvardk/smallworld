//! The Unicode pieces of Python's `unicodedata` and `re` the agent relies on: NFKC, general categories,
//! decimal digit values. The tables are the crates' (pinned), Python's are the interpreter's (Debian bookworm
//! 3.11: Unicode 14, noble 3.12: 15.0, trixie 3.13: 15.1); parity is defined over code points assigned in the
//! oldest table (PORTING.md "Unicode").

use unicode_general_category::{GeneralCategory, get_general_category};
use unicode_normalization::UnicodeNormalization;

/// `unicodedata.normalize("NFKC", text)`.
pub fn nfkc(text: &str) -> String {
    text.nfkc().collect()
}

/// `unicodedata.category(ch)`'s two-letter code.
pub fn category(ch: char) -> &'static str {
    use GeneralCategory as G;
    match get_general_category(ch) {
        G::UppercaseLetter => "Lu",
        G::LowercaseLetter => "Ll",
        G::TitlecaseLetter => "Lt",
        G::ModifierLetter => "Lm",
        G::OtherLetter => "Lo",
        G::NonspacingMark => "Mn",
        G::SpacingMark => "Mc",
        G::EnclosingMark => "Me",
        G::DecimalNumber => "Nd",
        G::LetterNumber => "Nl",
        G::OtherNumber => "No",
        G::ConnectorPunctuation => "Pc",
        G::DashPunctuation => "Pd",
        G::OpenPunctuation => "Ps",
        G::ClosePunctuation => "Pe",
        G::InitialPunctuation => "Pi",
        G::FinalPunctuation => "Pf",
        G::OtherPunctuation => "Po",
        G::MathSymbol => "Sm",
        G::CurrencySymbol => "Sc",
        G::ModifierSymbol => "Sk",
        G::OtherSymbol => "So",
        G::SpaceSeparator => "Zs",
        G::LineSeparator => "Zl",
        G::ParagraphSeparator => "Zp",
        G::Control => "Cc",
        G::Format => "Cf",
        G::Surrogate => "Cs",
        G::PrivateUse => "Co",
        // Unassigned, and any category a later version of the crate adds (#[non_exhaustive]).
        _ => "Cn",
    }
}

/// mc_agent.py `BAD_CATEGORIES`: control, format, surrogate, private use, unassigned, line/paragraph separators.
pub const BAD_CATEGORIES: [&str; 7] = ["Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"];

/// `unicodedata.category(ch) in BAD_CATEGORIES`.
pub fn is_bad_category(ch: char) -> bool {
    BAD_CATEGORIES.contains(&category(ch))
}

/// The value `int()` gives a decimal digit (`\d` in a Python str pattern is any `Nd` character; `int("٣")` is
/// 3). Unicode keeps every `Nd` digit in runs of ten, 0 to 9, laid end to end where several are adjacent.
pub fn decimal_value(ch: char) -> Option<u32> {
    if category(ch) != "Nd" {
        return None;
    }
    let mut start = u32::from(ch);
    while let Some(prev) = start.checked_sub(1).and_then(char::from_u32) {
        if category(prev) != "Nd" {
            break;
        }
        start -= 1;
    }
    Some((u32::from(ch) - start) % 10)
}

/// `int(text)` for a run of `\d` characters (Python accepts any `Nd` digits). `None` if empty or too big.
pub fn parse_decimal(text: &str) -> Option<i64> {
    let mut n: i64 = 0;
    let mut any = false;
    for ch in text.chars() {
        n = n
            .checked_mul(10)?
            .checked_add(i64::from(decimal_value(ch)?))?;
        any = true;
    }
    any.then_some(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn categories_and_nfkc() {
        assert_eq!(category('a'), "Ll");
        assert_eq!(category('\u{200b}'), "Cf");
        assert_eq!(category('\u{2028}'), "Zl");
        assert_eq!(category('\u{e000}'), "Co");
        assert!(is_bad_category('\u{7}'));
        assert!(!is_bad_category(' '));
        assert_eq!(nfkc("\u{ff53}\u{ff41}\u{ff59}"), "say");
        assert_eq!(nfkc("\u{fdfa}").chars().count(), 18);
    }

    #[test]
    fn decimal_values_like_int() {
        assert_eq!(decimal_value('7'), Some(7));
        assert_eq!(decimal_value('\u{663}'), Some(3)); // ARABIC-INDIC DIGIT THREE
        assert_eq!(decimal_value('\u{1d7d9}'), Some(1)); // MATHEMATICAL DOUBLE-STRUCK DIGIT ONE (2nd run)
        assert_eq!(decimal_value('x'), None);
        assert_eq!(parse_decimal("12\u{663}"), Some(123));
        assert_eq!(parse_decimal(""), None);
    }
}
