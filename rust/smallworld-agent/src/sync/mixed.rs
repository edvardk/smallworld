//! Mixed writers on one pending.json (tests only): Python's `mc_panel.Pending` in other processes and a Rust
//! thread record whitelist changes while Rust sync rounds open and merge pull requests against a fake GitHub.
//! Nothing recorded is lost, the file is always valid JSON, and the round never holds pending.json.lock across
//! a network call (a record from inside a GitHub request would deadlock).

use super::github::{FixedToken, GitHub, Transport};
use super::players::parse_players;
use super::round::{Round, sync_pending};
use crate::http::{HttpError, Response};
use crate::panel::pending::Pending;
use crate::types::{AResult, Value};
use base64::Engine as _;
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::time::{Duration, Instant};

const WRITER: &str = concat!(
    "import sys, time\n",
    "from pathlib import Path\n",
    "sys.path.insert(0, sys.argv[1])\n",
    "import mc_panel\n",
    "w, n = int(sys.argv[2]), int(sys.argv[3])\n",
    "pend = mc_panel.Pending(Path(sys.argv[4]))\n",
    "for j in range(n):\n",
    "    u = f'c1100000-0000-4000-8000-{w:04x}{j:08x}'\n",
    "    pend.record('add', f'Py{w}_{j:02d}', u, 'mcctl:py', time.time())\n",
    "    if j % 5 == 0:\n",
    "        pend.record('add', f'Py{w}_{j:02d}', u, 'mcctl:py', time.time())\n",
    "    time.sleep(0.01)\n",
);

#[derive(Default)]
struct Repo {
    main: String,
    branches: BTreeMap<String, String>,
    prs: BTreeMap<i64, String>,
    calls: usize,
}

#[allow(clippy::unnecessary_wraps)]
fn ok(v: &Value) -> Result<Response, HttpError> {
    Ok(Response {
        status: 200,
        body: v.to_string().into_bytes(),
    })
}

fn b64(s: &str) -> String {
    base64::engine::general_purpose::STANDARD.encode(s)
}

/// A GitHub that merges every pull request the first time its state is asked for.
fn fake_github(repo: Arc<Mutex<Repo>>, on_first: Box<dyn Fn() + Send + Sync>) -> Transport {
    Arc::new(move |method, url, _h, body, _t, _cap| {
        let path = url
            .strip_prefix("http://127.0.0.1:1")
            .unwrap_or(url)
            .to_string();
        let first = {
            let mut r = repo.lock().unwrap();
            r.calls += 1;
            r.calls == 1
        };
        if first {
            on_first();
        }
        std::thread::sleep(Duration::from_millis(3)); // the network
        let body: Option<Value> = body.map(|b| serde_json::from_slice(b).unwrap());
        let mut r = repo.lock().unwrap();
        match (method, path.as_str()) {
            ("GET", "/repos/o/cfg/git/ref/heads/main") => ok(&json!({"object": {"sha": "head"}})),
            ("GET", p) if p.starts_with("/repos/o/cfg/contents/players.yml") => {
                ok(&json!({"content": b64(&r.main), "sha": "blob"}))
            }
            ("POST", "/repos/o/cfg/git/refs") => ok(&json!({})),
            ("PUT", "/repos/o/cfg/contents/players.yml") => {
                let b = body.unwrap();
                let content = base64::engine::general_purpose::STANDARD
                    .decode(b["content"].as_str().unwrap())
                    .unwrap();
                let branch = b["branch"].as_str().unwrap().to_string();
                r.branches
                    .insert(branch, String::from_utf8(content).unwrap());
                ok(&json!({}))
            }
            ("POST", "/repos/o/cfg/pulls") => {
                let n = i64::try_from(r.prs.len()).unwrap() + 1;
                let head = body.unwrap()["head"].as_str().unwrap().to_string();
                r.prs.insert(n, head);
                ok(&json!({"number": n, "html_url": format!("https://github.test/pull/{n}")}))
            }
            ("GET", p) if p.starts_with("/repos/o/cfg/pulls/") => {
                let n: i64 = p.rsplit('/').next().unwrap().parse().unwrap();
                let head = r.prs[&n].clone();
                r.main = r.branches[&head].clone();
                ok(&json!({"state": "closed", "merged": true}))
            }
            _ => Err(HttpError::Status(404, Vec::new())),
        }
    })
}

fn scripts_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts")
}

fn python() -> Option<&'static str> {
    std::process::Command::new("python3")
        .arg("-c")
        .arg("import sys; sys.exit(sys.version_info < (3, 11))")
        .status()
        .ok()
        .filter(std::process::ExitStatus::success)
        .map(|_| "python3")
}

#[test]
fn python_and_rust_writers_with_the_rust_sync_round_lose_nothing() {
    let Some(py) = python() else {
        eprintln!("skipped: no python3 >= 3.11");
        return;
    };
    let d = std::env::temp_dir().join(format!("swagent-mixed-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    let path = d.join("pending.json");
    let pending = Pending::new(&path);
    let repo = Arc::new(Mutex::new(Repo {
        main: "whitelist: []\nops: []\n".into(),
        ..Repo::default()
    }));
    // inside the very first GitHub request: a record by another Pending (another descriptor, another thread)
    // must finish while the request is still in flight
    let inside_ok = Arc::new(Mutex::new(None::<bool>));
    let on_first: Box<dyn Fn() + Send + Sync> = {
        let inside_ok = Arc::clone(&inside_ok);
        let inside = path.clone();
        Box::new(move || {
            let (tx, rx) = mpsc::channel::<AResult<()>>();
            let inside = inside.clone();
            std::thread::spawn(move || {
                let p = Pending::new(inside);
                let _ = tx.send(p.record(
                    "add",
                    "Inside",
                    "dddddddd-0000-4000-8000-000000000001",
                    "panel",
                    crate::sys::now(),
                ));
            });
            let done = matches!(rx.recv_timeout(Duration::from_secs(5)), Ok(Ok(())));
            *inside_ok.lock().unwrap() = Some(done);
        })
    };
    let mut gh = GitHub::new(
        Arc::new(FixedToken("t".into())),
        "o/cfg",
        "http://127.0.0.1:1",
        "main",
    );
    gh.transport = fake_github(Arc::clone(&repo), on_first);

    let mut procs: Vec<std::process::Child> = (1..=2)
        .map(|w| {
            std::process::Command::new(py)
                .arg("-I")
                .arg("-c")
                .arg(WRITER)
                .arg(scripts_dir())
                .arg(w.to_string())
                .arg("30")
                .arg(&path)
                .spawn()
                .unwrap()
        })
        .collect();
    let done = Arc::new(AtomicBool::new(false));
    let rust_writer = {
        let path = path.clone();
        let done = Arc::clone(&done);
        std::thread::spawn(move || {
            let p = Pending::new(path);
            for j in 0..30u32 {
                let u = format!("aaaa0000-0000-4000-8000-{j:012x}");
                p.record("add", &format!("Rs_{j:02}"), &u, "panel", crate::sys::now())
                    .unwrap();
                std::thread::sleep(Duration::from_millis(7));
            }
            done.store(true, Ordering::SeqCst);
        })
    };
    let alert = |_: &str| {};
    let log = |_: &str, _: &str, _: &str| {};
    let rcon = |_: &str| -> AResult<String> { Ok(String::new()) };
    let r = Round {
        alert: &alert,
        log: &log,
        rcon: &rcon,
        batch_s: 0.0,
        pr_check_s: 0.0,
    };
    let deadline = Instant::now() + Duration::from_mins(1);
    let mut python_done = false;
    loop {
        sync_pending(&pending, Some(&gh), crate::sys::now(), &r).unwrap();
        // the file is always whole JSON (atomic renames by both implementations)
        if let Ok(text) = std::fs::read_to_string(&path) {
            assert!(serde_json::from_str::<Value>(&text).is_ok(), "{text}");
        }
        if !python_done {
            python_done = procs.iter_mut().all(|p| p.try_wait().unwrap().is_some());
        }
        let l = pending.load().unwrap();
        if python_done
            && done.load(Ordering::SeqCst)
            && l["entries"].as_array().unwrap().is_empty()
            && l["pr"].is_null()
        {
            break;
        }
        assert!(Instant::now() < deadline, "the rounds never drained: {l:?}");
    }
    for p in &mut procs {
        assert!(p.wait().unwrap().success());
    }
    rust_writer.join().unwrap();
    assert_eq!(
        *inside_ok.lock().unwrap(),
        Some(true),
        "a record during a GitHub request blocked: the lock was held across the network call"
    );
    let main = repo.lock().unwrap().main.clone();
    let players = parse_players(&main).unwrap();
    let got: BTreeSet<String> = players["whitelist"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["uuid"].as_str().unwrap().to_string())
        .collect();
    let mut want = BTreeSet::new();
    for w in 1..=2u32 {
        for j in 0..30u32 {
            want.insert(format!("c1100000-0000-4000-8000-{w:04x}{j:08x}"));
        }
    }
    for j in 0..30u32 {
        want.insert(format!("aaaa0000-0000-4000-8000-{j:012x}"));
    }
    want.insert("dddddddd-0000-4000-8000-000000000001".into());
    assert_eq!(got, want, "every recorded change reached Git");
    assert!(
        repo.lock().unwrap().prs.len() > 1,
        "several rounds raced the writers"
    );
    std::fs::remove_dir_all(d).unwrap();
}
