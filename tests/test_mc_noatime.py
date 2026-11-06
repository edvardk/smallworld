"""P16: ansible/library/mc_noatime.py (server.yml performance.noatime): fstab options + live remount."""

import json
import os
import subprocess
import sys

import mc_noatime
import pytest
from conftest import REPO

DEBIAN = (
    "# /etc/fstab: static file system information.\n"
    "#\n"
    "# <file system> <mount point>   <type>  <options>       <dump>  <pass>\n"
    "UUID=0b4d0c3e-1111-2222-3333-444455556666 /               ext4    errors=remount-ro 0       1\n"
    "UUID=ABCD-1234  /boot/efi       vfat    umask=0077      0       1\n"
    "/swapfile none swap sw 0 0\n"
)


def test_adds_noatime_and_keeps_the_rest_byte_for_byte() -> None:
    new, found, changed = mc_noatime.rewrite_fstab(DEBIAN, "/")
    assert found and changed
    old_lines, new_lines = DEBIAN.splitlines(), new.splitlines()
    assert [i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b] == [3]
    assert new_lines[3] == (
        "UUID=0b4d0c3e-1111-2222-3333-444455556666 /               ext4    errors=remount-ro,noatime 0       1"
    )
    # idempotent: a second pass changes nothing
    assert mc_noatime.rewrite_fstab(new, "/") == (new, True, False)


@pytest.mark.parametrize(
    ("opts", "want"),
    [
        ("defaults", "defaults,noatime"),
        ("defaults,relatime", "defaults,noatime"),
        ("rw,strictatime,discard", "rw,discard,noatime"),
        ("noatime,errors=remount-ro", "noatime,errors=remount-ro"),
        ("atime,lazytime", "lazytime,noatime"),
    ],
)
def test_other_atime_modes_are_replaced(opts: str, want: str) -> None:
    assert mc_noatime.with_noatime(opts) == (want, want != opts)


def test_no_entry_is_left_alone_and_duplicates_refused() -> None:
    assert mc_noatime.rewrite_fstab("# UNCONFIGURED FSTAB FOR BASE SYSTEM\n", "/") == (
        "# UNCONFIGURED FSTAB FOR BASE SYSTEM\n",
        False,
        False,
    )
    assert mc_noatime.rewrite_fstab(DEBIAN, "/opt")[1] is False  # not its own mount point
    assert mc_noatime.rewrite_fstab("# /dev/sda1 / ext4 defaults 0 1\n", "/")[1] is False  # commented out
    with pytest.raises(ValueError, match="2 fstab entries for /"):
        mc_noatime.rewrite_fstab(DEBIAN + "/dev/vdb1 / ext4 defaults 0 1\n", "/")


def test_escaped_mount_points() -> None:
    text = "/dev/vdb1 /srv/mc\\040data ext4 defaults 0 2\n"
    new, found, changed = mc_noatime.rewrite_fstab(text, "/srv/mc data")
    assert found and changed and new == "/dev/vdb1 /srv/mc\\040data ext4 defaults,noatime 0 2\n"


def mi(mountpoint: str, opts: str, root: str = "/", fs: str = "ext4 /dev/vda1 rw") -> str:
    """One proc(5) mountinfo line."""
    esc = mountpoint.replace(" ", "\\040")
    return f"30 1 254:1 {root} {esc} {opts} shared:1 - {fs}\n"


DEBIAN_MOUNTS = (
    mi("/", "rw,relatime")
    + mi("/run", "rw,nosuid,nodev", fs="tmpfs tmpfs rw")
    + mi("/boot/efi", "rw,relatime", fs="vfat /dev/vda15 rw")
)


def test_find_mount_takes_the_deepest_and_the_topmost() -> None:
    assert mc_noatime.find_mount("/opt/minecraft/server", DEBIAN_MOUNTS) == ("/", "/", "rw,relatime")
    assert mc_noatime.find_mount("/run/mc-telemetry", DEBIAN_MOUNTS)[0] == "/run"
    assert mc_noatime.find_mount("/running", DEBIAN_MOUNTS)[0] == "/"  # a prefix of the name is not a parent
    stacked = DEBIAN_MOUNTS + mi("/", "rw,noatime")  # remounted / mounted over: the last entry counts
    assert mc_noatime.find_mount("/srv", stacked) == ("/", "/", "rw,noatime")
    spaced = DEBIAN_MOUNTS + mi("/srv/mc data", "rw,relatime")
    assert mc_noatime.find_mount("/srv/mc data/world", spaced)[0] == "/srv/mc data"
    assert mc_noatime.find_mount("/srv", "") is None


def test_find_mount_sees_a_bind_mount_on_the_same_filesystem() -> None:
    """/opt/minecraft bound from /data/minecraft on the root filesystem: same st_dev as its parent, so
    os.path.ismount says no and the old lookup ended at /. mountinfo lists it (root = the source directory)."""
    table = DEBIAN_MOUNTS + mi("/opt/minecraft", "rw,relatime", root="/data/minecraft")
    assert mc_noatime.find_mount("/opt/minecraft/server", table) == (
        "/opt/minecraft",
        "/data/minecraft",
        "rw,relatime",
    )


BIND_FSTAB = DEBIAN + "/data/minecraft /opt/minecraft none bind 0 0\n"
BIND_MOUNTS = DEBIAN_MOUNTS + mi("/opt/minecraft", "rw,relatime", root="/data/minecraft")


@pytest.mark.parametrize(
    ("fstab", "mounts", "want"),
    [
        # the first deploy: fstab gains noatime, the mount lacks it -> one remount
        (
            DEBIAN,
            DEBIAN_MOUNTS,
            {"fstab_changed": True, "remount": True, "command": ["mount", "-o", "remount,noatime", "/"]},
        ),
        # the second deploy: both have it -> nothing
        (
            DEBIAN.replace("errors=remount-ro", "errors=remount-ro,noatime"),
            mi("/", "rw,noatime"),
            {"fstab_changed": False, "remount": False, "live_noatime": True},
        ),
        # fstab has it but the mount doesn't (the remount didn't stick, or a hand edit): never remount again
        (
            DEBIAN.replace("errors=remount-ro", "errors=remount-ro,noatime"),
            DEBIAN_MOUNTS,
            {"fstab_changed": False, "remount": False, "live_noatime": False},
        ),
        # fstab lacks it, the mount already has it (mounted by hand): fstab only
        (DEBIAN, mi("/", "rw,noatime"), {"fstab_changed": True, "remount": False, "live_noatime": True}),
        # a bind entry: only that mount's flags change
        (
            BIND_FSTAB,
            BIND_MOUNTS,
            {
                "mountpoint": "/opt/minecraft",
                "bind": True,
                "root": "/data/minecraft",
                "command": ["mount", "-o", "remount,bind,noatime", "/opt/minecraft"],
            },
        ),
    ],
)
def test_remount_decisions(fstab: str, mounts: str, want: dict) -> None:
    got = mc_noatime.plan(fstab, mounts, "/opt/minecraft/server")
    assert got["found"] is True
    for key, value in want.items():
        assert got[key] == value, key
    assert (got["command"] is None) is (not got["remount"])


def test_plan_rewrites_the_bind_entry_and_never_the_root_one() -> None:
    got = mc_noatime.plan(BIND_FSTAB, BIND_MOUNTS, "/opt/minecraft/server")
    old, new = BIND_FSTAB.splitlines(), got["new_text"].splitlines()
    assert [i for i, (a, b) in enumerate(zip(old, new, strict=True)) if a != b] == [len(old) - 1]
    assert new[-1] == "/data/minecraft /opt/minecraft none bind,noatime 0 0"


def test_plan_without_an_entry_or_a_table() -> None:
    got = mc_noatime.plan("# UNCONFIGURED FSTAB\n", mi("/", "rw,relatime", fs="overlay overlay rw"), "/opt/x")
    assert got == {
        "mountpoint": "/",
        "found": False,
        "fstab_changed": False,
        "new_text": "# UNCONFIGURED FSTAB\n",
    }
    with pytest.raises(ValueError, match="no mount holds"):
        mc_noatime.plan(DEBIAN, "", "/opt/x")


def _fake_mount(tmp_path, effect: str) -> dict:
    """A `mount` on PATH that logs its argv and, like the kernel, updates the mountinfo file (effect: apply /
    ignore = rc 0 but the option doesn't show / fail = rc 32)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "mount"
    script.write_text(
        f"""#!{sys.executable}
import sys
from pathlib import Path
d = Path({str(tmp_path)!r})
with open(d / "mount.log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if {effect!r} == "fail":
    sys.stderr.write("mount: permission denied\\n")
    sys.exit(32)
if {effect!r} == "apply":
    mi = d / "mountinfo"
    mi.write_text(mi.read_text().replace("rw,relatime", "rw,noatime"))
"""
    )
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}


def _run_module(tmp_path, fstab: str, mounts: str, check: bool = False, env: dict | None = None) -> dict:
    (tmp_path / "fstab").write_text(fstab)
    (tmp_path / "mountinfo").write_text(mounts)
    return _rerun(tmp_path, check, env)


def _rerun(tmp_path, check: bool = False, env: dict | None = None) -> dict:
    (tmp_path / "srv").mkdir(exist_ok=True)
    args = {
        "ANSIBLE_MODULE_ARGS": {
            "path": str(tmp_path / "srv"),
            "fstab": str(tmp_path / "fstab"),
            "mountinfo": str(tmp_path / "mountinfo"),
            "_ansible_check_mode": check,
        }
    }
    (tmp_path / "args.json").write_text(json.dumps(args))
    out = subprocess.run(
        [sys.executable, str(REPO / "ansible/library/mc_noatime.py"), str(tmp_path / "args.json")],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return json.loads(out.stdout)


def _warned(res: dict) -> str:
    """The module's warnings as text (their shape differs between ansible-core versions)."""
    return json.dumps(res.get("warnings", []))


def _mounted(tmp_path) -> str:
    """A mountinfo that puts tmp_path's real path on its own mount (so fstab entries can name it)."""
    return DEBIAN_MOUNTS + mi(os.path.realpath(tmp_path), "rw,relatime")


def test_module_end_to_end_without_an_entry_changes_nothing(tmp_path) -> None:
    pytest.importorskip("ansible")
    # tmp_path's mount point has no entry in this fstab (the e2e container's case): untouched, not changed.
    res = _run_module(tmp_path, "# UNCONFIGURED FSTAB\n", mi("/", "rw,relatime", fs="overlay overlay rw"))
    assert res["changed"] is False and res["found"] is False and "left as it is" in res["msg"]
    assert (tmp_path / "fstab").read_text() == "# UNCONFIGURED FSTAB\n"


def test_module_check_mode_reports_without_writing(tmp_path) -> None:
    pytest.importorskip("ansible")
    mp = os.path.realpath(tmp_path)
    fstab = f"/dev/vda1 {mp} ext4 defaults 0 1\n"
    env = _fake_mount(tmp_path, "apply")
    res = _run_module(tmp_path, fstab, _mounted(tmp_path), check=True, env=env)
    assert res["changed"] is True and res["fstab_changed"] is True and res["remounted"] is True
    assert (tmp_path / "fstab").read_text() == fstab  # check mode: nothing written, nothing remounted
    assert not (tmp_path / "mount.log").exists() and not res.get("warnings")


def test_module_remounts_once_then_changes_nothing(tmp_path) -> None:
    pytest.importorskip("ansible")
    mp = os.path.realpath(tmp_path)
    env = _fake_mount(tmp_path, "apply")
    res = _run_module(tmp_path, f"/dev/vda1 {mp} ext4 defaults 0 1\n", _mounted(tmp_path), env=env)
    assert res["changed"] and res["fstab_changed"] and res["remounted"] and res["live_noatime"]
    assert not res.get("warnings")
    assert (tmp_path / "fstab").read_text() == f"/dev/vda1 {mp} ext4 defaults,noatime 0 1\n"
    assert (tmp_path / "mount.log").read_text() == f"-o remount,noatime {mp}\n"
    res = _rerun(tmp_path, env=env)  # the second deploy
    assert res["changed"] is False and res["live_noatime"] is True
    assert (tmp_path / "mount.log").read_text().count("\n") == 1


@pytest.mark.parametrize("effect", ["ignore", "fail"])
def test_module_tries_one_remount_only(tmp_path, effect: str) -> None:
    """A filesystem that doesn't take noatime live (or a remount that fails): reported, fstab written, the
    deploy goes on, and the next run doesn't remount again (it would change something on every deploy)."""
    pytest.importorskip("ansible")
    mp = os.path.realpath(tmp_path)
    env = _fake_mount(tmp_path, effect)
    res = _run_module(tmp_path, f"/dev/vda1 {mp} ext4 defaults 0 1\n", _mounted(tmp_path), env=env)
    assert "failed" not in res and res["changed"] is True and res["live_noatime"] is False
    assert "applies at the next boot" in _warned(res)
    assert ("remount failed, rc 32: mount: permission denied" in _warned(res)) is (effect == "fail")
    assert "noatime" in (tmp_path / "fstab").read_text()
    res = _rerun(tmp_path, env=env)
    assert res["changed"] is False and res["remounted"] is False and res["live_noatime"] is False
    assert "applies at the next boot" in _warned(res)
    assert (tmp_path / "mount.log").read_text().count("\n") == 1  # one attempt, ever


def test_module_bind_mount_remounts_with_bind(tmp_path) -> None:
    pytest.importorskip("ansible")
    mp = os.path.realpath(tmp_path)
    env = _fake_mount(tmp_path, "apply")
    table = DEBIAN_MOUNTS + mi(mp, "rw,relatime", root="/data/minecraft")
    res = _run_module(tmp_path, DEBIAN + f"/data/minecraft {mp} none bind 0 0\n", table, env=env)
    assert res["changed"] and res["bind"] is True and res["mountpoint"] == mp
    assert (tmp_path / "mount.log").read_text() == f"-o remount,bind,noatime {mp}\n"
    assert (tmp_path / "fstab").read_text().endswith(f"/data/minecraft {mp} none bind,noatime 0 0\n")
    assert "errors=remount-ro 0" in (tmp_path / "fstab").read_text()  # the root entry is not touched
