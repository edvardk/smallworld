#!/usr/bin/python
"""Ansible module: merge settings into a Minecraft config file without fighting the server.

Paper/Purpur/Bukkit rewrite their config files on startup (comments, ordering, new defaults), so a
full-file template would differ byte-for-byte on every run and restart the server on every deploy.
This module compares *parsed values* instead: it only writes (and reports changed) when one of the
managed keys actually differs. Everything else in the file is left to the server.
"""

from __future__ import annotations

DOCUMENTATION = r"""
module: mc_config
short_description: Idempotently merge settings into a YAML or Java .properties file
options:
  path: {description: File to manage., type: path, required: true}
  format: {description: File format., type: str, choices: [yaml, properties], required: true}
  settings: {description: Nested dict (yaml) or flat dict (properties) of values to enforce., type: dict, required: true}
  create: {description: Create the file if missing (otherwise no-op until the server generates it)., type: bool, default: false}
  stage:
    description: >-
      v1.3 deferred restart. A staged copy of the file, outside the server dir. When it exists it is compared
      (and merged) instead of path; without check mode the merged result is written there, never to path. A
      file that already has the settings is not staged. Applied while the server is stopped by
      scripts/restart_window.py apply (minecraft.service ExecStartPre).
    type: path
extends_documentation_fragment: [files]
"""

import copy
import os
import tempfile

from ansible.module_utils.basic import AnsibleModule

try:
    import yaml

    HAS_YAML = True
except ImportError:  # pragma: no cover
    HAS_YAML = False


def deep_merge(base, overlay):
    """Return base with overlay applied recursively (dicts merge, everything else replaces)."""
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def diff_paths(before, after, prefix=""):
    """Dotted paths whose values differ (for the module's diff output)."""
    paths = []
    for k in sorted(set(before or {}) | set(after or {}), key=str):
        b, a = (before or {}).get(k), (after or {}).get(k)
        p = f"{prefix}{k}"
        if isinstance(a, dict) and isinstance(b, dict):
            paths += diff_paths(b, a, p + ".")
        elif a != b:
            paths.append(p)
    return paths


# --- Java .properties (subset written by Minecraft) ---


def prop_unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "u" and i + 5 < len(s):
                out.append(chr(int(s[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def prop_escape(s, key=False):
    out = []
    for i, c in enumerate(s):
        if c in "\\=:#!":
            out.append("\\" + c)
        elif c == " " and (key or i == 0):
            out.append("\\ ")
        elif c in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[c])
        elif ord(c) < 0x20 or ord(c) > 0x7E:
            out.append("\\u%04x" % ord(c))
        else:
            out.append(c)
    return "".join(out)


def split_prop(line):
    """Split 'key=value' honouring escapes. Returns (key, value) or None for comments/blank."""
    s = line.strip()
    if not s or s[0] in "#!":
        return None
    i = 0
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] in "=: \t":
            break
        i += 1
    key = s[:i]
    rest = s[i:].lstrip(" \t")
    if rest[:1] in ("=", ":"):
        rest = rest[1:].lstrip(" \t")
    return prop_unescape(key), prop_unescape(rest)


def to_prop_str(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def apply_properties(text, settings):
    lines = text.splitlines()
    want = {str(k): to_prop_str(v) for k, v in settings.items()}
    current = {}
    for ln in lines:
        kv = split_prop(ln)
        if kv:
            current[kv[0]] = kv[1]
    changed = [k for k, v in want.items() if current.get(k) != v]
    if not changed:
        return False, text, []
    seen = set()
    out = []
    for ln in lines:
        kv = split_prop(ln)
        if kv and kv[0] in want:
            out.append(f"{prop_escape(kv[0], key=True)}={prop_escape(want[kv[0]])}")
            seen.add(kv[0])
        else:
            out.append(ln)
    for k in want:
        if k not in seen:
            out.append(f"{prop_escape(k, key=True)}={prop_escape(want[k])}")
    return True, "\n".join(out) + "\n", sorted(changed)


def apply_yaml(text, settings):
    current = yaml.safe_load(text) if text.strip() else {}
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise ValueError("top level of YAML file is not a mapping")
    merged = deep_merge(current, settings)
    if merged == current:
        return False, text, []
    body = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)
    return True, body, diff_paths(current, merged)


def write_atomic(path, content):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".mc_config.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if not os.path.exists(path):
            os.chmod(tmp, 0o640)
        else:
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def staged(fmt, live, staged_text, settings):
    """(changed, content to stage, keys) for the deferred restart: the staged copy is the base when there is
    one; a live file that already has the settings needs no staged copy (changed False)."""
    base = staged_text if staged_text is not None else live
    return apply_yaml(base, settings) if fmt == "yaml" else apply_properties(base, settings)


def stage(module, path, stage_path):
    """Compare against / write the staged copy (see `stage` in DOCUMENTATION); never touches path."""
    p = module.params
    staged_text = None
    if os.path.exists(stage_path):
        with open(stage_path, encoding="utf-8") as f:
            staged_text = f.read()
    exists = os.path.exists(path)
    if staged_text is None and not exists and not p["create"]:
        module.exit_json(changed=False, skipped_missing=True, msg=f"{path} does not exist yet")
    live = ""
    if exists:
        with open(path, encoding="utf-8") as f:
            live = f.read()
    try:
        changed, content, keys = staged(p["format"], live, staged_text, p["settings"])
    except Exception as e:
        module.fail_json(msg=f"failed to parse {stage_path if staged_text is not None else path}: {e}")
    if changed and not module.check_mode:
        os.makedirs(os.path.dirname(stage_path), mode=0o750, exist_ok=True)
        write_atomic(stage_path, content)
    module.exit_json(
        changed=changed, path=stage_path, changed_keys=keys, diff={"before": "", "after": "\n".join(keys)}
    )


def main():
    module = AnsibleModule(
        argument_spec={
            "path": {"type": "path", "required": True},
            "format": {"type": "str", "required": True, "choices": ["yaml", "properties"]},
            "settings": {"type": "dict", "required": True, "no_log": True},  # may hold the RCON password
            "create": {"type": "bool", "default": False},
            "stage": {"type": "path", "default": None},
        },
        add_file_common_args=True,
        supports_check_mode=True,
    )
    p = module.params
    path = p["path"]
    if p["format"] == "yaml" and not HAS_YAML:
        module.fail_json(msg="PyYAML is required on the target (python3-yaml)")
    if p["stage"]:
        stage(module, path, p["stage"])

    exists = os.path.exists(path)
    if not exists and not p["create"]:
        module.exit_json(changed=False, skipped_missing=True, msg=f"{path} does not exist yet")

    text = ""
    if exists:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    try:
        if p["format"] == "yaml":
            changed, content, keys = apply_yaml(text, p["settings"])
        else:
            changed, content, keys = apply_properties(text, p["settings"])
    except Exception as e:
        module.fail_json(msg=f"failed to parse {path}: {e}")

    if changed and not module.check_mode:
        write_atomic(path, content)

    file_args = module.load_file_common_arguments(p)
    if exists or (changed and not module.check_mode):
        changed = module.set_fs_attributes_if_different(file_args, changed)
    module.exit_json(
        changed=changed, path=path, changed_keys=keys, diff={"before": "", "after": "\n".join(keys)}
    )


if __name__ == "__main__":
    main()
