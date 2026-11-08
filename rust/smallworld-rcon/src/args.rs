//! The command line, parsed exactly as mc_rcon.py's `argparse` setup parses it.
//!
//! `argparse` has rules most people never notice: `--rad 5` means `--radius 5` (unique prefixes), `--radius=5`
//! works, `-5` is a value but `-x` is an unknown option, `--` ends the options, `-h` anywhere prints help. Ansible
//! and the `mc` helper only use the plain forms, but a drop-in must accept and refuse exactly what the original
//! does, with the same `error:` line and exit status 2. So this file is a small copy of argparse's algorithm
//! (Python 3.13's `_parse_known_args`), limited to what mc_rcon.py's parsers use. The usage and help texts are
//! argparse-like, not byte for byte (no script reads them).

use crate::pystr::{PyInt, PyStr};
use std::fmt::Write as _;

/// What the command line asks for. Python's `args.cmd` plus the attributes each subcommand has; in Rust each
/// variant carries exactly its own fields, so `match` can't read `radius` from a `Cmd`.
#[derive(Debug, PartialEq, Eq)]
pub enum Command {
    /// `cmd COMMAND...`: run a console command, print the reply.
    Cmd { words: Vec<PyStr> },
    /// `stop`: save-all flush, stop, wait for `$MAINPID` (systemd ExecStop).
    Stop,
    /// `countdown SECONDS`: announce a restart in game.
    Countdown { seconds: PyInt },
    /// `worldborder --dimension D --radius R`: idempotent.
    Worldborder { dimension: PyStr, radius: PyInt },
    /// `gamerule --name N --value V --dimension D [--dimension D ...]`: idempotent.
    Gamerule {
        name: PyStr,
        value: PyStr,
        dimensions: Vec<PyStr>,
    },
}

/// How parsing ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Parsed {
    /// Run this command.
    Run(Command),
    /// `-h`/`--help`: print this text to stdout, exit 0.
    Help(String),
    /// A usage error: print this text to stderr, exit 2.
    Error(String),
}

/// The subcommands, in mc_rcon.py's order (argparse lists the choices in this order).
const SUBCOMMANDS: [&str; 5] = ["cmd", "stop", "countdown", "worldborder", "gamerule"];

/// The options a parser knows. Each has its option strings in argparse's registration order.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Opt {
    Help,
    Dimension,
    Radius,
    Name,
    Value,
}

impl Opt {
    /// The name argparse uses in messages: `argument -h/--help: ...`.
    fn display(self) -> &'static str {
        match self {
            Opt::Help => "-h/--help",
            Opt::Dimension => "--dimension",
            Opt::Radius => "--radius",
            Opt::Name => "--name",
            Opt::Value => "--value",
        }
    }
}

/// A positional argument and how many strings it takes (argparse's `nargs`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Positional {
    /// The subcommand (`nargs=PARSER`): one choice, then everything after it goes to the subparser.
    Subcommand,
    /// `cmd`'s `command` (`nargs="+"`).
    Words,
    /// `countdown`'s `seconds` (one value, `type=int`).
    Seconds,
}

impl Positional {
    fn name(self) -> &'static str {
        match self {
            Positional::Subcommand => "cmd",
            Positional::Words => "command",
            Positional::Seconds => "seconds",
        }
    }
}

/// One `ArgumentParser`: the top level or a subcommand's.
struct Spec {
    /// "" for the top level, else the subcommand.
    sub: &'static str,
    /// (option string, option), in registration order.
    options: &'static [(&'static str, Opt)],
    positional: Option<Positional>,
    /// Required options, in registration order (for "the following arguments are required").
    required: &'static [Opt],
    usage: &'static str,
    help: &'static str,
}

const HELP_OPTS: [(&str, Opt); 2] = [("-h", Opt::Help), ("--help", Opt::Help)];

const TOP: Spec = Spec {
    sub: "",
    options: &HELP_OPTS,
    positional: Some(Positional::Subcommand),
    required: &[],
    usage: "[-h] {cmd,stop,countdown,worldborder,gamerule} ...",
    help: "Minimal Source-RCON client for the local Minecraft server, plus the operations built on it.\n\n\
           Connection details (port, password) are read from server.properties, so no extra secret files exist.\n\
           A drop-in for the command line of scripts/mc_rcon.py (docs/rust/smallworld-rcon.md).\n\n    \
           smallworld-rcon cmd list                      # run a command, print the reply\n    \
           smallworld-rcon stop                          # save-all flush, stop, wait for $MAINPID (systemd ExecStop)\n    \
           smallworld-rcon countdown 60                  # announce a restart at 60/30/10s (skipped if nobody online)\n    \
           smallworld-rcon worldborder --dimension minecraft:overworld --radius 5000   # idempotent\n\n\
           positional arguments:\n  {cmd,stop,countdown,worldborder,gamerule}\n\n\
           options:\n  -h, --help            show this help message and exit\n",
};

fn spec_for(sub: &str) -> Spec {
    const WB: [(&str, Opt); 4] = [
        ("-h", Opt::Help),
        ("--help", Opt::Help),
        ("--dimension", Opt::Dimension),
        ("--radius", Opt::Radius),
    ];
    const GR: [(&str, Opt); 5] = [
        ("-h", Opt::Help),
        ("--help", Opt::Help),
        ("--name", Opt::Name),
        ("--value", Opt::Value),
        ("--dimension", Opt::Dimension),
    ];
    match sub {
        "cmd" => Spec {
            sub: "cmd",
            options: &HELP_OPTS,
            positional: Some(Positional::Words),
            required: &[],
            usage: "[-h] command [command ...]",
            help: "positional arguments:\n  command\n",
        },
        "stop" => Spec {
            sub: "stop",
            options: &HELP_OPTS,
            positional: None,
            required: &[],
            usage: "[-h]",
            help: "",
        },
        "countdown" => Spec {
            sub: "countdown",
            options: &HELP_OPTS,
            positional: Some(Positional::Seconds),
            required: &[],
            usage: "[-h] seconds",
            help: "positional arguments:\n  seconds\n",
        },
        "worldborder" => Spec {
            sub: "worldborder",
            options: &WB,
            positional: None,
            required: &[Opt::Dimension, Opt::Radius],
            usage: "[-h] --dimension DIMENSION --radius RADIUS",
            help: "",
        },
        _ => Spec {
            sub: "gamerule",
            options: &GR,
            positional: None,
            required: &[Opt::Name, Opt::Value, Opt::Dimension],
            usage: "[-h] --name NAME --value VALUE --dimension DIMENSION",
            help: "",
        },
    }
}

impl Spec {
    /// "smallworld-rcon" or "smallworld-rcon worldborder", as argparse's `prog`.
    fn prog(&self, prog: &str) -> String {
        if self.sub.is_empty() {
            prog.to_string()
        } else {
            format!("{prog} {}", self.sub)
        }
    }

    fn usage_line(&self, prog: &str) -> String {
        format!("usage: {} {}\n", self.prog(prog), self.usage)
    }

    /// `parser.error(message)`: usage, then `prog: error: message`.
    fn error(&self, prog: &str, message: &str) -> Parsed {
        Parsed::Error(format!(
            "{}{}: error: {message}\n",
            self.usage_line(prog),
            self.prog(prog)
        ))
    }

    fn help_text(&self, prog: &str) -> String {
        let mut text = self.usage_line(prog);
        text.push('\n');
        text.push_str(self.help);
        if self.sub.is_empty() {
            return text;
        }
        if !self.help.is_empty() {
            text.push('\n');
        }
        text.push_str("options:\n  -h, --help            show this help message and exit\n");
        for (s, _) in self.options.iter().skip(2) {
            let meta = s.trim_start_matches('-').to_uppercase();
            let _ = writeln!(text, "  {s} {meta}");
        }
        text
    }
}

/// How argparse classified one command-line string.
#[derive(Clone, Debug)]
struct OptMatch {
    /// The option, or `None` for an unknown one (it goes to the "unrecognized arguments").
    opt: Option<Opt>,
    /// The option string it matched (`--radius` for `--rad=5`).
    option_string: String,
    /// `Some("=")` or `Some("")` when the string carried its value (`--radius=5`, `-hfoo`).
    sep: Option<String>,
    explicit: Option<PyStr>,
}

/// `_negative_number_matcher`: `^-\d+$|^-\d*\.\d+$` (`$` also matches before a final newline).
fn looks_negative(arg: &PyStr) -> bool {
    let cps = arg.code_points();
    let mut s: Vec<char> = cps.iter().filter_map(|&c| char::from_u32(c)).collect();
    if s.len() != cps.len() || s.first() != Some(&'-') {
        return false;
    }
    if s.last() == Some(&'\n') {
        s.pop();
    }
    let rest = &s[1..];
    let digits = |p: &[char]| p.iter().all(char::is_ascii_digit);
    if !rest.is_empty() && digits(rest) {
        return true;
    }
    match rest.iter().position(|&c| c == '.') {
        Some(dot) => digits(&rest[..dot]) && rest.len() > dot + 1 && digits(&rest[dot + 1..]),
        None => false,
    }
}

/// Splits at the first `=`: (before, Some(after)) or (all, None).
fn partition_eq(arg: &PyStr) -> (PyStr, Option<PyStr>) {
    let cps = arg.code_points();
    match cps.iter().position(|&c| c == u32::from('=')) {
        Some(i) => (
            PyStr::from_code_points(&cps[..i]),
            Some(PyStr::from_code_points(&cps[i + 1..])),
        ),
        None => (arg.clone(), None),
    }
}

impl Spec {
    fn find(&self, s: &PyStr) -> Option<Opt> {
        self.options
            .iter()
            .find(|(name, _)| s.eq_str(name))
            .map(|(_, o)| *o)
    }

    /// `_parse_optional`: `None` = a positional value; else the option(s) it could be.
    fn parse_optional(&self, arg: &PyStr) -> Option<Vec<OptMatch>> {
        let cps = arg.code_points();
        if cps.first() != Some(&u32::from('-')) {
            return None; // empty, or no leading '-'
        }
        if let Some(opt) = self.find(arg) {
            return Some(vec![OptMatch {
                opt: Some(opt),
                option_string: arg.display(),
                sep: None,
                explicit: None,
            }]);
        }
        if cps.len() == 1 {
            return None; // "-" alone is a value
        }
        let (before, after) = partition_eq(arg);
        if let (Some(explicit), Some(opt)) = (after.clone(), self.find(&before)) {
            let m = OptMatch {
                opt: Some(opt),
                option_string: before.display(),
                sep: Some("=".into()),
                explicit: Some(explicit),
            };
            return Some(vec![m]);
        }
        let tuples = self.option_tuples(arg);
        if !tuples.is_empty() {
            return Some(tuples);
        }
        if looks_negative(arg) {
            return None; // these parsers have no options that look like negative numbers
        }
        if cps.contains(&u32::from(' ')) {
            return None;
        }
        Some(vec![OptMatch {
            opt: None,
            option_string: arg.display(),
            sep: None,
            explicit: None,
        }])
    }

    /// `_get_option_tuples`: unique-prefix matches (`--rad` for `--radius`), and `-hVALUE` for short options.
    fn option_tuples(&self, arg: &PyStr) -> Vec<OptMatch> {
        let cps = arg.code_points();
        let (prefix, explicit) = partition_eq(arg);
        let sep = explicit.as_ref().map(|_| "=".to_string());
        let mut out = Vec::new();
        let double = cps.get(1) == Some(&u32::from('-'));
        let short_prefix = PyStr::from_code_points(&cps[..2]);
        for (name, opt) in self.options {
            if !double && short_prefix.eq_str(name) {
                out.push(OptMatch {
                    opt: Some(*opt),
                    option_string: (*name).into(),
                    sep: Some(String::new()),
                    explicit: Some(PyStr::from_code_points(&cps[2..])),
                });
            } else if PyStr::from(*name).starts_with_py(&prefix) {
                out.push(OptMatch {
                    opt: Some(*opt),
                    option_string: (*name).into(),
                    sep: sep.clone(),
                    explicit: explicit.clone(),
                });
            }
        }
        out
    }
}

/// What one parser collected (argparse's namespace).
#[derive(Default)]
struct Namespace {
    subcommand: Option<String>,
    words: Vec<PyStr>,
    seconds: Option<PyInt>,
    dimension: Option<PyStr>,
    dimensions: Vec<PyStr>,
    radius: Option<PyInt>,
    name: Option<PyStr>,
    value: Option<PyStr>,
    seen: Vec<Opt>,
    seen_positional: bool,
    /// Unrecognized strings the subparser left (argparse's `_UNRECOGNIZED_ARGS_ATTR`).
    sub_extras: Vec<PyStr>,
}

/// `type=int`'s conversion, with argparse's message.
fn to_int(what: &str, s: &PyStr) -> Result<PyInt, String> {
    PyInt::parse(s).map_err(|_| format!("argument {what}: invalid int value: {}", s.repr()))
}

/// A parse of one parser over its strings: argparse's `_parse_known_args`.
struct Run<'a> {
    spec: &'a Spec,
    prog: &'a str,
    args: &'a [PyStr],
    /// 'A' (a value), 'O' (an option) or '-' (the `--`), one per string.
    pattern: Vec<u8>,
    options: Vec<Option<Vec<OptMatch>>>,
    ns: Namespace,
    extras: Vec<PyStr>,
    positional_done: bool,
}

impl<'a> Run<'a> {
    fn new(spec: &'a Spec, prog: &'a str, args: &'a [PyStr]) -> Self {
        let mut pattern = Vec::new();
        let mut options = Vec::new();
        let mut after_dashdash = false;
        for arg in args {
            if after_dashdash {
                pattern.push(b'A');
                options.push(None);
            } else if arg.eq_str("--") {
                pattern.push(b'-');
                options.push(None);
                after_dashdash = true; // everything after `--` is a value
            } else {
                let m = spec.parse_optional(arg);
                pattern.push(if m.is_some() { b'O' } else { b'A' });
                options.push(m);
            }
        }
        Run {
            spec,
            prog,
            args,
            pattern,
            options,
            ns: Namespace::default(),
            extras: Vec::new(),
            positional_done: false,
        }
    }

    /// An error from this parser. Parsing methods return `Result<_, Parsed>`: the `Err` side is how parsing
    /// stopped early (an error, or help), so `?` ends the whole parse, like argparse's `parser.exit()`.
    fn fail(&self, message: &str) -> Parsed {
        self.spec.error(self.prog, message)
    }

    /// How many strings from `start` the positional takes (its nargs pattern), or 0.
    fn positional_count(&self, start: usize) -> usize {
        let p = &self.pattern[start..];
        let mut i = 0;
        while p.get(i) == Some(&b'-') {
            i += 1; // `-*`
        }
        if p.get(i) != Some(&b'A') {
            return 0;
        }
        i += 1;
        match self.spec.positional {
            // `(-*A[-AO]*)`: everything
            Some(Positional::Subcommand) => p.len(),
            // `(-*A[A-]*)`: up to the next option
            Some(Positional::Words) => {
                i + p[i..]
                    .iter()
                    .take_while(|&&c| c == b'A' || c == b'-')
                    .count()
            }
            // `(-*A-*)`
            Some(Positional::Seconds) => i + p[i..].iter().take_while(|&&c| c == b'-').count(),
            None => 0,
        }
    }

    /// `consume_positionals(start)`: the index after what the positional took.
    fn consume_positionals(&mut self, start: usize) -> Result<usize, Parsed> {
        let Some(positional) = self.spec.positional else {
            return Ok(start);
        };
        if self.positional_done {
            return Ok(start);
        }
        let count = self.positional_count(start);
        if count == 0 {
            return Ok(start);
        }
        let mut values: Vec<PyStr> = self.args[start..start + count].to_vec();
        // Strip out the first '--' (for the subcommand: only when it comes first).
        let strip = match positional {
            Positional::Subcommand => self.pattern[start] == b'-',
            _ => self.pattern[start..start + count].contains(&b'-'),
        };
        if strip && let Some(i) = values.iter().position(|v| v.eq_str("--")) {
            values.remove(i);
        }
        self.positional_done = true;
        self.ns.seen_positional = true;
        self.take_positional(positional, values)?;
        Ok(start + count)
    }

    fn take_positional(
        &mut self,
        positional: Positional,
        values: Vec<PyStr>,
    ) -> Result<(), Parsed> {
        match positional {
            Positional::Subcommand => {
                let choice = &values[0];
                let Some(sub) = SUBCOMMANDS.iter().find(|s| choice.eq_str(s)) else {
                    let choices: Vec<String> =
                        SUBCOMMANDS.iter().map(|s| format!("'{s}'")).collect();
                    let msg = format!(
                        "argument cmd: invalid choice: {} (choose from {})",
                        choice.repr(),
                        choices.join(", ")
                    );
                    return Err(self.fail(&msg));
                };
                let spec = spec_for(sub);
                let mut sub_run = Run::new(&spec, self.prog, &values[1..]);
                sub_run.parse()?;
                self.ns.subcommand = Some((*sub).to_string());
                self.ns.words = sub_run.ns.words;
                self.ns.seconds = sub_run.ns.seconds;
                self.ns.dimension = sub_run.ns.dimension;
                self.ns.dimensions = sub_run.ns.dimensions;
                self.ns.radius = sub_run.ns.radius;
                self.ns.name = sub_run.ns.name;
                self.ns.value = sub_run.ns.value;
                self.ns.sub_extras = sub_run.extras;
            }
            Positional::Words => self.ns.words = values,
            Positional::Seconds => {
                // `-*A-*` with the first `--` removed leaves one string here.
                let value = to_int("seconds", &values[0]).map_err(|m| self.fail(&m))?;
                self.ns.seconds = Some(value);
            }
        }
        Ok(())
    }

    /// `take_action` for an option.
    fn take_option(&mut self, opt: Opt, value: Option<PyStr>) -> Result<(), Parsed> {
        self.ns.seen.push(opt);
        match (opt, value) {
            (Opt::Help, _) => return Err(Parsed::Help(self.spec.help_text(self.prog))),
            (Opt::Dimension, Some(v)) if self.spec.sub == "gamerule" => self.ns.dimensions.push(v), // action="append"
            (Opt::Dimension, Some(v)) => self.ns.dimension = Some(v),
            (Opt::Radius, Some(v)) => {
                let r = to_int("--radius", &v).map_err(|m| self.fail(&m))?;
                self.ns.radius = Some(r);
            }
            (Opt::Name, Some(v)) => self.ns.name = Some(v),
            (Opt::Value, Some(v)) => self.ns.value = Some(v),
            (_, None) => {}
        }
        Ok(())
    }

    /// `consume_optional(start)`: the index after the option (and its value).
    fn consume_optional(&mut self, start: usize) -> Result<usize, Parsed> {
        let tuples = self.options[start].clone().unwrap_or_default();
        if tuples.len() > 1 {
            let names: Vec<&str> = tuples.iter().map(|t| t.option_string.as_str()).collect();
            let msg = format!(
                "ambiguous option: {} could match {}",
                self.args[start].display(),
                names.join(", ")
            );
            return Err(self.fail(&msg));
        }
        let OptMatch {
            opt,
            mut option_string,
            mut sep,
            mut explicit,
        } = tuples[0].clone();
        let Some(mut opt) = opt else {
            self.extras.push(self.args[start].clone());
            return Ok(start + 1);
        };
        let mut actions: Vec<(Opt, Option<PyStr>)> = Vec::new();
        let stop;
        loop {
            let takes_value = opt != Opt::Help;
            if let Some(arg) = explicit.clone() {
                let single_dash = option_string.as_bytes().get(1) != Some(&b'-');
                let arg_cps = arg.code_points();
                if !takes_value && single_dash && !arg.is_empty() {
                    // `-hX`: X may be more single-letter options; `-h=X` or `-h-X` is an error.
                    if sep.as_deref().is_some_and(|s| !s.is_empty()) || arg_cps[0] == u32::from('-')
                    {
                        let msg = format!(
                            "argument {}: ignored explicit argument {}",
                            opt.display(),
                            arg.repr()
                        );
                        return Err(self.fail(&msg));
                    }
                    actions.push((opt, None));
                    let mut next = PyStr::from("-");
                    next.push(arg_cps[0]);
                    if let Some(next_opt) = self.spec.find(&next) {
                        opt = next_opt;
                        option_string = next.display();
                        let rest = PyStr::from_code_points(&arg_cps[1..]);
                        if rest.is_empty() {
                            sep = None;
                            explicit = None;
                        } else if rest.code_points()[0] == u32::from('=') {
                            sep = Some("=".into());
                            explicit = Some(PyStr::from_code_points(&rest.code_points()[1..]));
                        } else {
                            sep = Some(String::new());
                            explicit = Some(rest);
                        }
                        continue;
                    }
                    let mut unknown = PyStr::from("-");
                    unknown.push_py(&arg);
                    self.extras.push(unknown);
                    stop = start + 1;
                    break;
                } else if takes_value {
                    actions.push((opt, Some(arg)));
                    stop = start + 1;
                    break;
                }
                let msg = format!(
                    "argument {}: ignored explicit argument {}",
                    opt.display(),
                    arg.repr()
                );
                return Err(self.fail(&msg));
            }
            // No value in the string: an option with a value takes the next string, if it is a value ('A').
            if takes_value {
                if self.pattern.get(start + 1) != Some(&b'A') {
                    return Err(self.fail(&format!(
                        "argument {}: expected one argument",
                        opt.display()
                    )));
                }
                actions.push((opt, Some(self.args[start + 1].clone())));
                stop = start + 2;
            } else {
                actions.push((opt, None));
                stop = start + 1;
            }
            break;
        }
        for (opt, value) in actions {
            self.take_option(opt, value)?;
        }
        Ok(stop)
    }

    /// The main loop of `_parse_known_args`: positionals and options alternately, then the required check.
    fn parse(&mut self) -> Result<(), Parsed> {
        let option_indices: Vec<usize> = (0..self.args.len())
            .filter(|&i| self.options[i].is_some())
            .collect();
        let max_option = option_indices.last().copied();
        let mut start = 0;
        while let Some(max) = max_option.filter(|&m| start <= m) {
            let next_option = (start..=max)
                .find(|i| option_indices.contains(i))
                .unwrap_or(max + 1);
            if start != next_option {
                let end = self.consume_positionals(start)?;
                if end > start {
                    start = end;
                    continue;
                }
            }
            if !option_indices.contains(&start) {
                self.extras
                    .extend_from_slice(&self.args[start..next_option]);
                start = next_option;
            }
            start = self.consume_optional(start)?;
        }
        let stop = self.consume_positionals(start)?;
        self.extras.extend_from_slice(&self.args[stop..]);

        let mut missing: Vec<&str> = Vec::new();
        if let Some(p) = self.spec.positional
            && !self.ns.seen_positional
        {
            missing.push(p.name());
        }
        for opt in self.spec.required {
            if !self.ns.seen.contains(opt) {
                missing.push(opt.display());
            }
        }
        if !missing.is_empty() {
            return Err(self.fail(&format!(
                "the following arguments are required: {}",
                missing.join(", ")
            )));
        }
        Ok(())
    }
}

/// Parses `args` (without the program name). `prog` is the program's name for messages (argparse uses the base
/// name of `argv[0]`).
pub fn parse(prog: &str, args: &[PyStr]) -> Parsed {
    let mut run = Run::new(&TOP, prog, args);
    if let Err(parsed) = run.parse() {
        return parsed;
    }
    // Strings neither parser knew: the top level reports them (its own first, then the subparser's).
    let mut unknown = std::mem::take(&mut run.extras);
    unknown.append(&mut run.ns.sub_extras);
    if !unknown.is_empty() {
        let joined = PyStr::join(&unknown, " ").display();
        return TOP.error(prog, &format!("unrecognized arguments: {joined}"));
    }
    let ns = run.ns;
    let command = match ns.subcommand.as_deref() {
        Some("cmd") => Command::Cmd { words: ns.words },
        Some("stop") => Command::Stop,
        Some("countdown") => Command::Countdown {
            seconds: ns.seconds.unwrap_or_else(|| PyInt::from_i128(0)),
        },
        Some("worldborder") => Command::Worldborder {
            dimension: ns.dimension.unwrap_or_default(),
            radius: ns.radius.unwrap_or_else(|| PyInt::from_i128(0)),
        },
        _ => Command::Gamerule {
            name: ns.name.unwrap_or_default(),
            value: ns.value.unwrap_or_default(),
            dimensions: ns.dimensions,
        },
    };
    Parsed::Run(command)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(line: &str) -> Parsed {
        let args: Vec<PyStr> = if line.is_empty() {
            Vec::new()
        } else {
            line.split(' ').map(PyStr::from).collect()
        };
        parse("smallworld-rcon", &args)
    }

    /// The `error:` line, or "HELP", or the parsed command (Debug-formatted).
    fn outcome(line: &str) -> String {
        match run(line) {
            Parsed::Run(c) => format!("{c:?}"),
            Parsed::Help(_) => "HELP".into(),
            Parsed::Error(text) => text
                .lines()
                .last()
                .unwrap()
                .split_once(": error: ")
                .unwrap()
                .1
                .to_string(),
        }
    }

    fn words(ws: &[&str]) -> String {
        format!(
            "{:?}",
            Command::Cmd {
                words: ws.iter().map(|w| PyStr::from(*w)).collect()
            }
        )
    }

    #[test]
    fn plain_forms() {
        assert_eq!(outcome("cmd list"), words(&["list"]));
        assert_eq!(
            outcome("cmd say hello world"),
            words(&["say", "hello", "world"])
        );
        assert_eq!(outcome("stop"), "Stop");
        assert_eq!(
            outcome("countdown 60"),
            format!(
                "{:?}",
                Command::Countdown {
                    seconds: PyInt::from_i128(60)
                }
            )
        );
        assert_eq!(
            outcome("worldborder --dimension minecraft:overworld --radius 5000"),
            format!(
                "{:?}",
                Command::Worldborder {
                    dimension: PyStr::from("minecraft:overworld"),
                    radius: PyInt::from_i128(5000)
                }
            )
        );
        assert_eq!(
            outcome("gamerule --name locator_bar --value true --dimension a --dimension b"),
            format!(
                "{:?}",
                Command::Gamerule {
                    name: PyStr::from("locator_bar"),
                    value: PyStr::from("true"),
                    dimensions: vec![PyStr::from("a"), PyStr::from("b")],
                }
            )
        );
    }

    /// Each case was checked against mc_rcon.py on Python 3.13 (tests/test_rust_rcon.py repeats them live).
    #[test]
    fn argparse_edge_cases() {
        let cases: &[(&str, &str)] = &[
            ("", "the following arguments are required: cmd"),
            (
                "x",
                "argument cmd: invalid choice: 'x' (choose from 'cmd', 'stop', 'countdown', 'worldborder', 'gamerule')",
            ),
            ("cmd", "the following arguments are required: command"),
            (
                "countdown abc",
                "argument seconds: invalid int value: 'abc'",
            ),
            ("countdown 5 6", "unrecognized arguments: 6"),
            (
                "worldborder",
                "the following arguments are required: --dimension, --radius",
            ),
            (
                "worldborder --dimension",
                "argument --dimension: expected one argument",
            ),
            (
                "worldborder --dimension a --radius x",
                "argument --radius: invalid int value: 'x'",
            ),
            (
                "gamerule --name a",
                "the following arguments are required: --value, --dimension",
            ),
            ("cmd -x", "the following arguments are required: command"),
            ("cmd list -x", "unrecognized arguments: -x"),
            ("-x cmd a -y", "unrecognized arguments: -x -y"),
            ("cmd a -x b", "unrecognized arguments: -x b"),
            (
                "cmd --help=x",
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (
                "cmd -h=x",
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (
                "cmd --=x",
                "argument -h/--help: ignored explicit argument 'x'",
            ),
            (
                "worldborder --=x",
                "ambiguous option: --=x could match --help, --dimension, --radius",
            ),
            (
                "worldborder --dimension -- x --radius 5",
                "argument --dimension: expected one argument",
            ),
            ("stop --", "unrecognized arguments: --"),
            (
                "countdown -- -- 5",
                "argument seconds: invalid int value: '--'",
            ),
            (
                "worldborder --dimension x -- --radius 1",
                "the following arguments are required: --radius",
            ),
            (
                "countdown 0x5",
                "argument seconds: invalid int value: '0x5'",
            ),
            (
                "worldborder --dimension x --radius=",
                "argument --radius: invalid int value: ''",
            ),
            ("countdown x -h", "argument seconds: invalid int value: 'x'"),
            (
                "--cmd list",
                "argument cmd: invalid choice: 'list' (choose from 'cmd', 'stop', 'countdown', 'worldborder', 'gamerule')",
            ),
        ];
        for (line, want) in cases {
            assert_eq!(outcome(line), *want, "{line}");
        }
    }

    #[test]
    fn values_that_look_like_options() {
        assert_eq!(
            outcome("cmd tp @p 0 -60 0"),
            words(&["tp", "@p", "0", "-60", "0"])
        );
        assert_eq!(outcome("cmd -1.5 -.5 -"), words(&["-1.5", "-.5", "-"]));
        assert_eq!(outcome("cmd a -- -- b"), words(&["a", "--", "b"]));
        assert_eq!(outcome("cmd -- -x"), words(&["-x"]));
        assert_eq!(outcome("-- cmd list"), words(&["list"]));
        assert_eq!(
            outcome("countdown -- 5"),
            format!(
                "{:?}",
                Command::Countdown {
                    seconds: PyInt::from_i128(5)
                }
            )
        );
        assert_eq!(
            outcome("countdown -5"),
            format!(
                "{:?}",
                Command::Countdown {
                    seconds: PyInt::from_i128(-5)
                }
            )
        );
        assert_eq!(
            outcome("worldborder --dim=a=b --rad 1 --radius=2"),
            format!(
                "{:?}",
                Command::Worldborder {
                    dimension: PyStr::from("a=b"),
                    radius: PyInt::from_i128(2)
                }
            )
        );
    }

    #[test]
    fn help_anywhere() {
        for line in [
            "-h",
            "-h cmd",
            "cmd -h",
            "cmd list -h",
            "cmd -hello",
            "cmd --he",
            "stop -x -h",
            "countdown -h 5",
        ] {
            assert_eq!(outcome(line), "HELP", "{line}");
        }
    }
}
