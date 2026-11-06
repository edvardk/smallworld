#!/usr/bin/python
"""Ansible module (P16, server.yml performance.noatime): mount the filesystem holding a path with noatime.

Finds the mount that holds `path` in /proc/self/mountinfo (the deepest mount point above it, the topmost of a
stack: a bind mount of a directory on the same filesystem counts, which `os.path.ismount` misses), then
  1. remounts it live when the running options lack noatime, and only when its /etc/fstab entry is about to
     gain it (`mount -o remount,noatime <mount point>`; `remount,bind,noatime` for a bind entry, which changes
     that mount's flags only);
  2. rewrites the options of its fstab entry: adds `noatime`, drops the other atime modes (`relatime`,
     `strictatime`, `atime`); every other option, the other fields and the line's spacing stay as they are.
One attempt: a remount that fails (or that the kernel doesn't reflect) is reported as a warning, fstab is
written anyway and the deploy goes on.

Idempotent, decided by fstab + the mount options: once the fstab entry has noatime, the module never remounts
again. A filesystem that still doesn't report noatime after the one remount (or an fstab that already had
noatime while the mount lacks it) is reported (`live_noatime: false`, a warning), not retried on every deploy:
the fstab option applies at the next boot.

Never adds an fstab entry: a mount point without one (a container's overlay root, a systemd mount unit) is
left alone and reported (`found: false`). More than one entry for the mount point is an error (which one
would win is not this module's call). Check mode reports what would change.
"""

from __future__ import annotations

DOCUMENTATION = r"""
module: mc_noatime
short_description: Mount the filesystem holding a path with noatime (fstab + live remount)
options:
  path: {description: A path on the filesystem to change (its mount point is looked up)., type: path, required: true}
  fstab: {description: The fstab file., type: path, default: /etc/fstab}
  mountinfo: {description: The live mount table (proc(5) mountinfo)., type: path, default: /proc/self/mountinfo}
"""

import os
import re
import tempfile

from ansible.module_utils.basic import AnsibleModule

ATIME_MODES = ("relatime", "strictatime", "atime")
OPTION = "noatime"
# An fstab line: fields separated by blanks; the options are the 4th field.
FIELDS = re.compile(r"^(\s*\S+\s+)(\S+)(\s+\S+\s+)(\S+)(.*)$", re.S)


def unescape(field):
    """fstab/mounts escape blanks and backslashes as octal (\\040 = space)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text):
    """[(mount point, root within its filesystem, per-mount options)] in table order (proc(5) mountinfo)."""
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            mounts.append((unescape(parts[4]), unescape(parts[3]), parts[5]))
    return mounts


def _under(path, mountpoint):
    return mountpoint == "/" or path == mountpoint or path.startswith(mountpoint + "/")


def find_mount(path, mountinfo_text):
    """(mount point, root, options) of the mount that holds path: the deepest mount point above it, the last
    (topmost) entry of a stack. None if the table is empty."""
    best = None
    for mp, root, opts in parse_mountinfo(mountinfo_text):
        if _under(path, mp) and (best is None or len(mp) >= len(best[0])):
            best = (mp, root, opts)
    return best


def with_noatime(options):
    """(new options, changed): noatime added, other atime modes dropped, everything else kept in order."""
    opts = [o for o in options.split(",") if o]
    kept = [o for o in opts if o not in ATIME_MODES]
    if OPTION not in kept:
        kept.append(OPTION)
    new = ",".join(kept)
    return new, new != options


def rewrite_fstab(text, mountpoint):
    """(new text, found, changed). Raises ValueError for more than one entry of the mount point."""
    lines = text.splitlines(keepends=True)
    hits = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        m = FIELDS.match(line)
        if m and unescape(m.group(2)) == mountpoint:
            hits.append((i, m))
    if not hits:
        return text, False, False
    if len(hits) > 1:
        raise ValueError(f"{len(hits)} fstab entries for {mountpoint}: fix /etc/fstab by hand")
    i, m = hits[0]
    new_opts, changed = with_noatime(m.group(4))
    if changed:
        lines[i] = m.group(1) + m.group(2) + m.group(3) + new_opts + m.group(5)
    return "".join(lines), True, changed


def entry_options(text, mountpoint):
    """The options of the mount point's (single, uncommented) fstab entry, or None."""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = FIELDS.match(line)
        if m and unescape(m.group(2)) == mountpoint:
            return m.group(4)
    return None


def plan(fstab_text, mountinfo_text, path):
    """What to do, without doing it. Raises ValueError for duplicate fstab entries or an empty mount table."""
    mount = find_mount(path, mountinfo_text)
    if mount is None:
        raise ValueError("no mount holds " + path + " (empty mount table?)")
    mp, root, live = mount
    new_text, found, fstab_changed = rewrite_fstab(fstab_text, mp)
    out = {"mountpoint": mp, "found": found, "fstab_changed": fstab_changed, "new_text": new_text}
    if not found:
        return out
    opts = entry_options(fstab_text, mp).split(",")
    bind = "bind" in opts or "rbind" in opts
    live_noatime = OPTION in live.split(",")
    remount = fstab_changed and not live_noatime
    out.update(
        bind=bind,
        root=root,
        live_noatime=live_noatime,
        remount=remount,
        command=["mount", "-o", ("remount,bind," if bind else "remount,") + OPTION, mp] if remount else None,
    )
    return out


def write_atomic(path, content):
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mc-fstab-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def main():
    module = AnsibleModule(
        argument_spec={
            "path": {"type": "path", "required": True},
            "fstab": {"type": "path", "default": "/etc/fstab"},
            "mountinfo": {"type": "path", "default": "/proc/self/mountinfo"},
        },
        supports_check_mode=True,
    )
    p = module.params
    path = os.path.realpath(p["path"])
    try:
        with open(p["fstab"], encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    with open(p["mountinfo"], encoding="utf-8") as f:
        table = f.read()
    try:
        todo = plan(text, table, path)
    except ValueError as e:
        module.fail_json(msg=str(e), path=path)
    mp = todo["mountpoint"]
    if not todo["found"]:
        module.exit_json(
            changed=False,
            found=False,
            mountpoint=mp,
            msg=f"no {p['fstab']} entry for {mp}: left as it is",
        )
    live_noatime, why = todo["live_noatime"], ""
    if todo["remount"] and not module.check_mode:
        rc, _out, err = module.run_command(todo["command"])
        with open(p["mountinfo"], encoding="utf-8") as f:
            now = find_mount(path, f.read())
        live_noatime = now is not None and now[0] == mp and OPTION in now[2].split(",")
        why = (
            f" (remount failed, rc {rc}: {err.strip()})"
            if rc
            else " (remounted, the kernel doesn't report it)"
        )
    if todo["fstab_changed"] and not module.check_mode:
        write_atomic(p["fstab"], todo["new_text"])
    if not live_noatime and not (todo["remount"] and module.check_mode):
        # One attempt only: the fstab entry has noatime now, so later runs report this instead of remounting.
        module.warn(
            f"{mp} is not mounted with noatime{why}; its fstab entry has it, so it applies at the next boot"
        )
    module.exit_json(
        changed=todo["fstab_changed"] or todo["remount"],
        found=True,
        mountpoint=mp,
        bind=todo["bind"],
        fstab_changed=todo["fstab_changed"],
        remounted=todo["remount"],
        live_noatime=live_noatime,
    )


if __name__ == "__main__":
    main()
