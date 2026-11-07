"""mcctl's own settings file (v1.3): the config repos it remembers, so `-C` is optional.

`~/.config/mcctl/config.toml` (`$XDG_CONFIG_HOME/mcctl/` when set; Windows: `%APPDATA%\\mcctl\\`):

    # mcctl's remembered config repos (written by mcctl; no secrets)
    current = "/home/me/my-server"
    repos = ["/home/me/my-server", "/home/me/other-server"]

Only absolute paths of config repos are kept here, never a credential. Read with tomllib; written by hand (a
string is JSON-quoted, which is a valid TOML basic string) so no TOML writer is needed. A missing, unreadable
or corrupt file reads as "nothing remembered"; writing replaces it.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

FILE_NAME = "config.toml"
MAX_REPOS = 10
HEADER = "# mcctl's remembered config repos (written by mcctl; no secrets)\n"


@dataclass
class Settings:
    current: Path | None = None
    repos: list[Path] = field(default_factory=list)
    problem: str = ""  # why the file couldn't be read ("" = fine or absent)


def config_dir(environ: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    env = os.environ if environ is None else environ
    if (platform or sys.platform) == "win32":
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "mcctl"
    xdg = env.get("XDG_CONFIG_HOME", "")
    base_path = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base_path / "mcctl"


def config_file(environ: Mapping[str, str] | None = None, platform: str | None = None) -> Path:
    return config_dir(environ, platform) / FILE_NAME


def looks_like_config_repo(path: Path) -> bool:
    """Cheap check (no validation): the two files every config repo has."""
    try:
        return (path / "server.yml").is_file() and (path / ".github" / "workflows" / "server.yml").is_file()
    except OSError:
        return False


def load(path: Path | None = None) -> Settings:
    path = path or config_file()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return Settings(problem=f"{path}: {e}")
    current = data.get("current")
    repos = data.get("repos")
    out = Settings()
    if isinstance(current, str) and current:
        out.current = Path(current)
    if isinstance(repos, list):
        out.repos = [Path(r) for r in repos if isinstance(r, str) and r]
    if out.current is not None and out.current not in out.repos:
        out.repos.insert(0, out.current)
    return out


def dumps(settings: Settings) -> str:
    lines = [HEADER]
    if settings.current is not None:
        lines.append(f"current = {json.dumps(str(settings.current))}\n")
    repos = ", ".join(json.dumps(str(r)) for r in settings.repos)
    lines.append(f"repos = [{repos}]\n")
    return "".join(lines)


def save(settings: Settings, path: Path | None = None) -> Path:
    path = path or config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(settings), encoding="utf-8")
    os.replace(tmp, path)
    return path


def remember(repo: Path, path: Path | None = None) -> Path:
    """Make `repo` the current config repo (first in the list; at most MAX_REPOS kept)."""
    repo = repo.expanduser().resolve()
    settings = load(path)  # a corrupt file is replaced
    repos = [repo, *(r for r in settings.repos if r != repo)][:MAX_REPOS]
    return save(Settings(current=repo, repos=repos), path)


def remembered(path: Path | None = None) -> Path | None:
    """The current remembered config repo, if it still looks like one."""
    current = load(path).current
    return current if current is not None and looks_like_config_repo(current) else None
