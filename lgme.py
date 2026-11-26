#!/usr/bin/env python3
"""LGME-CLI (Linux Gaming Made Easy): add, set up and play games on Linux.

https://github.com/ItzAditya43/LGME-CLI

LGME works with:
  * Steam games (it asks the Steam client to install and launch them),
  * game archives you already have, or a direct download link you trust
    (ZIP, 7z, RAR, TAR),
  * game folders that are already extracted (including an existing prefix),
  * Windows installers (setup.exe).

Windows games run with Proton (GE-Proton preferred) or Wine.
Run `lgme` with no arguments for a guided menu.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

VERSION = "1.0.0"
APP = "lgme"
HOME = Path.home()
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / APP
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / APP
APPLICATIONS_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share") / "applications"
DEFAULT_GAMES_DIR = HOME / "Games" / "LGME"
FLATPAK_STEAM = "com.valvesoftware.Steam"
GE_PROTON_API = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest"
USER_AGENT = f"LGME-CLI/{VERSION}"
REPO = "ItzAditya43/LGME-CLI"
RELEASE_DOWNLOAD = f"https://github.com/{REPO}/releases/latest/download/lgme"

ASSUME_YES = False  # set by -y/--yes

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


class UserError(Exception):
    """A problem we can explain to the user in plain language."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


def _color_enabled(stream=sys.stdout) -> bool:
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def _paint(code: str, text: str, stream=sys.stdout) -> str:
    return f"\033[{code}m{text}\033[0m" if _color_enabled(stream) else text


def bold(text: str) -> str:
    return _paint("1", text)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def good(msg: str) -> None:
    say(_paint("32", "✓ ") + msg)


def warn(msg: str) -> None:
    say(_paint("33", "! ") + msg)


def bad(msg: str) -> None:
    say(_paint("31", "✗ ") + msg)


def tip(msg: str) -> None:
    say(_paint("36", "→ ") + msg)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


class Progress:
    """A single-line progress display that degrades gracefully when not on a TTY."""

    def __init__(self, label: str, total: int | None = None):
        self.label = label
        self.total = total or None
        self.tty = sys.stdout.isatty()
        self.last = 0.0
        self.start = time.monotonic()
        if not self.tty:
            say(f"{label}…")

    def update(self, done: int, force: bool = False) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if not force and now - self.last < 0.2:
            return
        self.last = now
        elapsed = max(now - self.start, 0.001)
        speed = human_size(done / elapsed) + "/s"
        if self.total:
            pct = min(done * 100 // self.total, 100)
            text = f"{self.label}: {pct:3d}%  {human_size(done)} of {human_size(self.total)}  {speed}"
        else:
            text = f"{self.label}: {human_size(done)}  {speed}"
        sys.stdout.write("\r\033[K" + text)
        sys.stdout.flush()

    def done(self, message: str | None = None) -> None:
        if self.tty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        good(message or f"{self.label}: done")


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirm(question: str, default: bool = False) -> bool:
    """Ask a yes/no question. Without a terminal, the default is used."""
    if ASSUME_YES:
        return True
    if not is_interactive():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        say("Please answer y (yes) or n (no).")


def ask(question: str, default: str = "") -> str:
    if not is_interactive():
        if default:
            return default
        raise UserError(f"LGME needs an answer to: {question}",
                        "Run this in a terminal, or pass the value as an option (see: lgme help).")
    shown = f" [{default}]" if default else ""
    answer = input(f"{question}{shown}: ").strip()
    return answer or default


def pick(question: str, options: list[str], default: int = 0, recommend: bool = True) -> int:
    """Show a numbered list and return the chosen index (0-based)."""
    say(question)
    for i, option in enumerate(options, 1):
        marker = "  (recommended)" if recommend and i - 1 == default else ""
        say(f"  {i}. {option}{marker}")
    while True:
        answer = input(f"Type a number and press Enter [{default + 1}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        say(f"Please type a number from 1 to {len(options)}.")


def clean_user_path(text: str) -> str:
    """Tidy a path typed, pasted or dragged into the terminal."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    if text.startswith("file://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    elif not text.startswith(("http://", "https://")):
        text = re.sub(r"\\(.)", r"\1", text)  # "My\ Game.zip" -> "My Game.zip"
    return text


# --------------------------------------------------------------------------
# Settings and game list
# --------------------------------------------------------------------------


def registry_path() -> Path:
    return CONFIG_DIR / "games.json"


def settings_path() -> Path:
    return CONFIG_DIR / "settings.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


def read_registry() -> dict[str, dict[str, Any]]:
    path = registry_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        backup = path.with_name(path.name + ".bak")
        hint = f"A backup copy may exist at {backup}. " if backup.exists() else ""
        raise UserError(
            f"Your game list file is damaged and can't be read: {path}",
            hint + f"You can fix it in a text editor, or move it away to start a fresh list "
            f"(mv '{path}' '{path}.broken'). Your game files and saves are not affected.")
    return data


def write_registry(data: dict[str, dict[str, Any]]) -> None:
    _write_json(registry_path(), data)


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        warn(f"Ignoring damaged settings file: {settings_path()}")
        return {}


def games_dir() -> Path:
    configured = os.environ.get("LGME_GAMES_DIR") or load_settings().get("games_dir")
    return Path(configured).expanduser() if configured else DEFAULT_GAMES_DIR


def prefixes_dir() -> Path:
    return DATA_DIR / "prefixes"


def logs_dir() -> Path:
    return DATA_DIR / "logs"


def slug(value: str) -> str:
    """Turn any text into an easy-to-type game name: 'My Game!' -> 'my-game'."""
    value = re.sub(r"[^a-z0-9.]+", "-", value.lower())
    value = re.sub(r"-{2,}", "-", value).strip("-.")
    return value or "game"


def find_game(registry: dict[str, Any], query: str) -> str:
    if query in registry:
        return query
    key = slug(query)
    if key in registry:
        return key
    for name, game in registry.items():
        if slug(game.get("title", "")) == key:
            return name
    matches = difflib.get_close_matches(key, list(registry), n=3, cutoff=0.5)
    matches += [n for n in registry if key and key in n and n not in matches]
    if matches:
        hint = "Did you mean: " + ", ".join(matches[:3]) + "?"
    elif registry:
        hint = "See your games with: lgme list"
    else:
        hint = "You haven't added any games yet. Start with: lgme add <file, folder or link>"
    raise UserError(f"There's no game called '{query}'.", hint)


def unique_name(registry: dict[str, Any], base: str) -> str:
    name, i = base, 2
    while name in registry or (games_dir() / name).exists():
        name, i = f"{base}-{i}", i + 1
    return name


def choose_name(registry: dict[str, Any], suggested: str, explicit: str | None,
                needs_folder: bool = False) -> str:
    """Pick a name that doesn't clash with an existing game (or folder). Never overwrites."""
    name = slug(explicit or suggested)
    if explicit is None and is_interactive() and not ASSUME_YES:
        name = slug(ask("Name for this game (you'll type it to play, e.g. 'lgme run NAME')", name))
    while name in registry or (needs_folder and (games_dir() / name).exists()):
        what = (f"You already have a game called '{name}'" if name in registry
                else f"The folder {games_dir() / name} already exists")
        alt = unique_name(registry, name)
        if is_interactive() and not ASSUME_YES:
            warn(what + ". LGME won't replace it.")
            name = slug(ask("Pick a different name", alt))
        else:
            raise UserError(what + ". LGME won't replace it.",
                            f"Choose another name with --name {alt}" +
                            (f", or remove the old one first: lgme remove {name}" if name in registry else ""))
    return name


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# Finding the game program
# --------------------------------------------------------------------------

SKIP_EXE = re.compile(
    r"(setup|install|unins|redist|vcredist|vc_redist|dxsetup|dxwebsetup|directx|dotnet|ndp\d|"
    r"physx|oalinst|prereq|crash|helper|report|easyanticheat|battleye|beservice|"
    r"cefprocess|webhelper|updater)", re.I)
SKIP_EXE_EXACT = {"7z.exe", "7za.exe", "unrar.exe", "winrar.exe", "quicksfv.exe", "notepad++.exe"}
INSTALLER_EXE = re.compile(r"^(setup|install|installer)([ _.-].*|\d*)?\.exe$", re.I)
SKIP_DIRS = {"_commonredist", "commonredist", "redist", "redists", "_redist", "redistributables",
             "directx", "vcredist", "dotnet", "prerequisites", "prereqs", "__support", "support",
             "_installer", "installers", "pfx", "drive_c", "dosdevices", "compatdata", "__macosx"}
NATIVE_SUFFIXES = {".x86_64", ".x86", ".sh", ".appimage"}
MAX_SCAN_DEPTH = 8


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def is_prefix_dir(path: Path) -> bool:
    return (path / "system.reg").is_file() or (path / "pfx" / "system.reg").is_file()


def _walk(root: Path) -> Iterable[tuple[Path, int]]:
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        dirnames[:] = sorted(d for d in dirnames
                             if d.lower() not in SKIP_DIRS and not is_prefix_dir(current / d)
                             and depth < MAX_SCAN_DEPTH)
        for filename in filenames:
            yield current / filename, depth + 1


def find_launchables(root: Path, include_installers: bool = False) -> list[Path]:
    """Likely game programs (.exe or Linux launchers), best guesses first."""
    found: list[tuple[tuple, Path]] = []
    hint = slug(root.name)
    for path, depth in _walk(root):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name.lower()
        suffix = path.suffix.lower()
        if suffix == ".exe":
            if INSTALLER_EXE.match(path.name):
                if not include_installers:
                    continue
            elif name in SKIP_EXE_EXACT or SKIP_EXE.search(name):
                continue
        elif suffix in NATIVE_SUFFIXES:
            pass
        elif suffix == "" and depth <= 3 and is_elf(path):
            pass
        else:
            continue
        similarity = difflib.SequenceMatcher(None, slug(path.stem), hint).ratio()
        found.append(((depth, -round(similarity, 1), name), path))
    return [p for _, p in sorted(found, key=lambda item: item[0])]


def find_installers(root: Path) -> list[Path]:
    return [p for p in find_launchables(root, include_installers=True)
            if p.suffix.lower() == ".exe" and INSTALLER_EXE.match(p.name)]


def game_kind(exe: Path) -> str:
    return "windows" if exe.suffix.lower() in (".exe", ".bat", ".msi") else "native"


def describe(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    label = "Windows" if game_kind(path) == "windows" else "Linux"
    return f"{rel}  [{label}]"


def resolve_requested_exe(root: Path, requested: str) -> Path:
    candidate = Path(clean_user_path(requested)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_file():
        return candidate.resolve()
    # Allow just the file name, e.g. --exe Game.exe
    by_name = [p for p, _ in _walk(root) if p.name.lower() == Path(requested).name.lower()]
    if len(by_name) == 1:
        return by_name[0].resolve()
    options = find_launchables(root)[:8]
    listing = ("Possible game programs:\n" + "\n".join(f"  {describe(p, root)}" for p in options)) if options else ""
    raise UserError(f"Couldn't find the program '{requested}' in {root}.",
                    (listing + "\n" if listing else "") +
                    "Give the path inside the game folder, e.g. --exe \"Game/Binaries/Game.exe\".")


def choose_launchable(root: Path, candidates: list[Path], requested: str | None, name: str) -> Path:
    if requested:
        return resolve_requested_exe(root, requested)
    if not candidates:
        raise UserError(
            f"Couldn't find a game program (.exe or Linux launcher) in {root}.",
            "Look inside that folder for the file you'd double-click to start the game, "
            "then run the command again with --exe \"path/to/Game.exe\".")
    if len(candidates) == 1:
        return candidates[0]
    shown = candidates[:15]
    if not is_interactive() or ASSUME_YES:
        if ASSUME_YES:
            warn(f"Several programs found; using the most likely one: {describe(shown[0], root)}")
            return shown[0]
        listing = "\n".join(f"  {describe(p, root)}" for p in shown)
        raise UserError(f"Found several programs that could start '{name}':\n{listing}",
                        "Pick one with --exe \"path/shown/above\".")
    index = pick(f"Found several programs in this game. Which one starts '{name}'?",
                 [describe(p, root) for p in shown])
    return shown[index]


def ensure_executable(path: Path) -> None:
    if game_kind(path) == "native" and not os.access(path, os.X_OK):
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# --------------------------------------------------------------------------
# Archives
# --------------------------------------------------------------------------

ARCHIVE_SUFFIX = re.compile(
    r"(\.part0*\d+)?\.(zip|7z|rar|tar|tgz|txz|tbz2|tar\.gz|tar\.xz|tar\.bz2|tar\.zst)(\.0*\d+)?$", re.I)


def archive_title(path: Path) -> str:
    return ARCHIVE_SUFFIX.sub("", path.name) or path.stem


def archive_kind(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except (OSError, tarfile.TarError, EOFError):
        pass
    if head.startswith((b"\x28\xb5\x2f\xfd", b"\x1f\x8b", b"\xfd7zXZ\x00", b"BZh")):
        return "compressed"
    return None


def later_part(path: Path) -> bool:
    match = re.search(r"\.part0*(\d+)\.rar$|\.(?:7z|zip|rar)\.0*(\d+)$", path.name, re.I)
    return bool(match and int(match.group(1) or match.group(2)) > 1)


def is_split_zip(path: Path) -> bool:
    return path.with_suffix(".z01").exists() or bool(re.search(r"\.zip\.0*1$", path.name, re.I))


def external_extractor() -> str | None:
    for tool in ("7z", "7zz", "bsdtar", "7za"):
        if shutil.which(tool):
            return tool
    return None


def safe_member_path(name: str) -> PurePosixPath | None:
    """Validate an archive member path; return it normalised, or None for the archive root."""
    normal = name.replace("\\", "/")
    path = PurePosixPath(normal)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", normal):
        raise UserError(f"The archive contains an unsafe file path: {name}",
                        "LGME stopped so nothing outside the game folder could be overwritten. "
                        "The archive may be damaged or malicious; get a fresh copy from a source you trust.")
    parts = [p for p in path.parts if p not in ("", ".")]
    return PurePosixPath(*parts) if parts else None


def archive_unpacked_size(archive: Path, kind: str) -> int | None:
    try:
        if kind == "zip":
            with zipfile.ZipFile(archive) as zf:
                return sum(i.file_size for i in zf.infolist())
        if kind == "tar" and archive.suffix.lower() == ".tar":
            with tarfile.open(archive) as tf:
                return sum(m.size for m in tf.getmembers())
        tool = external_extractor()
        if tool in ("7z", "7zz", "7za"):
            out = subprocess.run([tool, "l", "-slt", str(archive)], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=120, errors="replace")
            sizes = [int(m) for m in re.findall(r"^Size = (\d+)$", out.stdout, re.M)]
            if out.returncode == 0 and sizes:
                return sum(sizes)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, subprocess.SubprocessError):
        pass
    return None


def existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_space(where: Path, needed: int, estimated: bool = False) -> None:
    free = shutil.disk_usage(existing_parent(where)).free
    if free >= needed * 1.05:
        return
    message = (f"Not enough free disk space: this needs about {human_size(needed)}, "
               f"but only {human_size(free)} is free at {existing_parent(where)}.")
    hint = ("Free up some space, or store games on a bigger drive with: "
            "lgme settings --games-dir /path/on/bigger/drive")
    if estimated:
        warn(message + " (This is an estimate.)")
        if not confirm("Continue anyway?", default=False):
            raise UserError("Stopped because of low disk space.", hint)
        return
    raise UserError(message, hint)


def extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        progress = Progress("Unpacking", sum(i.file_size for i in infos))
        done = skipped_links = 0
        for info in infos:
            rel = safe_member_path(info.filename)
            if rel is None:
                continue
            dest = target.joinpath(*rel.parts)
            mode = info.external_attr >> 16
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            if stat.S_ISLNK(mode):
                skipped_links += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as out:
                while chunk := src.read(1024 * 1024):
                    out.write(chunk)
                    done += len(chunk)
                    progress.update(done)
            if mode & 0o111:
                dest.chmod(0o755)
        progress.done("Unpacked the archive")
        if skipped_links:
            warn(f"Skipped {skipped_links} shortcut link(s) inside the archive for safety.")


_TAR_FILTER_ERRORS = (getattr(tarfile, "FilterError", tarfile.TarError),)


def extract_tar(archive: Path, target: Path) -> None:
    progress = Progress("Unpacking")
    with tarfile.open(archive) as tf:
        def members():
            done = 0
            for member in tf:
                safe_member_path(member.name)
                done += member.size
                progress.update(done)
                yield member
        try:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(target, members=members(), filter="data")
            else:  # Python < 3.12
                safe = (m for m in members() if not (m.issym() or m.islnk() or m.isdev()))
                tf.extractall(target, members=safe)
        except _TAR_FILTER_ERRORS as exc:
            raise UserError(f"The archive contains an unsafe entry ({exc}).",
                            "LGME stopped so nothing outside the game folder could be changed.") from exc
    progress.done("Unpacked the archive")


def remove_escaping_links(target: Path) -> None:
    root = target.resolve()
    for path, _ in list(_walk(target)):
        if path.is_symlink():
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                path.unlink()


def extract_external(archive: Path, target: Path) -> None:
    tool = external_extractor()
    if tool is None:
        raise UserError("This archive type (7z/RAR) needs the 7-Zip tool, which isn't installed.",
                        "Install it with: lgme setup   (or your package manager: the package is "
                        "usually called '7zip' or 'p7zip').")
    listing = (subprocess.run([tool, "l", "-slt", "-ba", str(archive)], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, errors="replace")
               if tool != "bsdtar" else
               subprocess.run(["bsdtar", "-tf", str(archive)], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, errors="replace"))
    if listing.returncode == 0:
        names = (re.findall(r"^Path = (.*)$", listing.stdout, re.M) if tool != "bsdtar"
                 else listing.stdout.splitlines())
        for name in names:
            safe_member_path(name)
    say(f"Unpacking with {tool} (if the archive has a password, type it when asked)…")
    command = ([tool, "x", "-y", f"-o{target}", str(archive)] if tool != "bsdtar"
               else ["bsdtar", "-xf", str(archive), "-C", str(target)])
    result = subprocess.run(command, stdout=None if sys.stdout.isatty() else subprocess.DEVNULL)
    if result.returncode not in (0, 1):  # 7-Zip: 1 = warnings only
        raise UserError("Unpacking failed. The archive may be damaged, incomplete, password-protected, "
                        "or split into parts that aren't all in the same folder.",
                        "Check the download finished completely. For split archives (part1, part2…), "
                        "put all parts in one folder and add the first part.")
    remove_escaping_links(target)
    good("Unpacked the archive")


def extract_archive(archive: Path, kind: str, target: Path) -> None:
    if kind == "zip" and not is_split_zip(archive):
        try:
            extract_zip(archive, target)
            return
        except (NotImplementedError, RuntimeError, zipfile.BadZipFile, EOFError) as exc:
            if not external_extractor():
                raise UserError(f"Couldn't unpack this ZIP ({exc}).",
                                "It may be damaged or use a password/compression Python can't read. "
                                "Installing 7-Zip usually fixes this: lgme setup") from exc
            warn("This ZIP needs 7-Zip (it may use a password or special compression). Switching to 7-Zip…")
            shutil.rmtree(target)
            target.mkdir()
    elif kind == "tar":
        extract_tar(archive, target)
        return
    extract_external(archive, target)


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------


def _download_error(exc: Exception, url: str) -> UserError:
    host = urllib.parse.urlparse(url).netloc
    if isinstance(exc, urllib.error.HTTPError):
        reasons = {
            401: "the site wants you to log in first",
            403: "the site refused the download (it may only work in a browser)",
            404: "the file wasn't found; the link may have expired or be mistyped",
            410: "the file has been removed",
            429: "the site is limiting downloads; wait a while and try again",
        }
        reason = reasons.get(exc.code, f"the server answered with error {exc.code}")
        return UserError(f"Download failed: {reason}.",
                         "Try opening the link in your browser. If it downloads there, add the downloaded "
                         "file instead: lgme add ~/Downloads/<file>")
    return UserError(f"Couldn't download from {host}: {getattr(exc, 'reason', exc)}.",
                     "Check your internet connection and the link, then run the same command again "
                     "(partial downloads continue where they left off).")


def fetch(url: str, part: Path, label: str = "Downloading") -> tuple[Path, str | None]:
    """Download `url` into `part` (resuming if possible). Returns (part, server filename)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UserError("Download links must start with http:// or https://.")
    part.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    have = part.stat().st_size if part.exists() else 0
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # already complete
            return part, None
        raise _download_error(exc, url) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise _download_error(exc, url) from exc
    with response:
        ctype = response.headers.get("Content-Type", "")
        if "text/html" in ctype:
            raise UserError(
                "That link opens a web page, not a file.",
                "Many sites (cloud drives, file hosts) need you to click a download button in the browser. "
                "Download it there, then run: lgme add ~/Downloads/<file>")
        filename = None
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", disposition)
        if match:
            filename = urllib.parse.unquote(match.group(1) or match.group(2)).strip()
        resumed = response.status == 206
        if have and not resumed:
            have = 0
        length = int(response.headers.get("Content-Length") or 0)
        total = have + length if length else None
        if length:
            check_space(part.parent, length)
        progress = Progress(label, total)
        done = have
        if resumed:
            say(f"Continuing an earlier download ({human_size(have)} already done).")
        try:
            with part.open("ab" if resumed else "wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
                    done += len(chunk)
                    progress.update(done)
        except KeyboardInterrupt:
            raise UserError("Download paused.", "Run the same command again to continue where it left off.")
        except (OSError, urllib.error.URLError) as exc:
            raise UserError(f"The download was interrupted: {exc}.",
                            "Run the same command again to continue where it left off.") from exc
        if total and done < total:
            raise UserError("The download stopped before it finished.",
                            "Run the same command again to continue where it left off.")
        progress.done(f"Downloaded {human_size(done)}")
    return part, filename


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, i = path.name, "", 2
    match = ARCHIVE_SUFFIX.search(path.name)
    if match:
        stem, suffix = path.name[:match.start()], path.name[match.start():]
    while (candidate := path.with_name(f"{stem}-{i}{suffix}")).exists():
        i += 1
    return candidate


def download_archive(url: str) -> Path:
    downloads = DATA_DIR / "downloads"
    part = downloads / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".part")
    part, server_name = fetch(url, part)
    name = server_name or Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name or "download"
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name).strip(". ") or "download"
    if not archive_kind(part) and not name.lower().endswith(".exe"):
        part.unlink(missing_ok=True)
        raise UserError("The link downloaded something that isn't a game archive (ZIP, 7z, RAR or TAR).",
                        "Make sure it's a direct link to the file itself.")
    final = unique_path(downloads / name)
    part.rename(final)
    return final


# --------------------------------------------------------------------------
# Steam, Proton and Wine discovery
# --------------------------------------------------------------------------


def natural_key(text: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def steam_roots() -> list[Path]:
    candidates = [
        HOME / ".local/share/Steam",
        HOME / ".steam/steam",
        HOME / ".steam/root",
        HOME / ".var/app" / FLATPAK_STEAM / ".local/share/Steam",
        HOME / ".var/app" / FLATPAK_STEAM / "data/Steam",
    ]
    roots: list[Path] = []
    for path in candidates:
        if path.is_dir():
            resolved = path.resolve()
            if resolved not in roots:
                roots.append(resolved)
    return roots


def flatpak_steam_installed() -> bool:
    if not shutil.which("flatpak"):
        return False
    try:
        return subprocess.run(["flatpak", "info", FLATPAK_STEAM], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def steam_command() -> list[str] | None:
    if shutil.which("steam"):
        return ["steam"]
    if flatpak_steam_installed():
        return ["flatpak", "run", FLATPAK_STEAM]
    return None


def library_folders(root: Path) -> list[Path]:
    folders = [root]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        for raw in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
            path = Path(raw.replace("\\\\", "\\"))
            if path.is_dir() and path.resolve() not in folders:
                folders.append(path.resolve())
    except OSError:
        pass
    return folders


def parse_acf(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in re.findall(r'"(\w+)"\s+"([^"]*)"', text):
        values.setdefault(key, value)
    return values


def steam_manifest(appid: str) -> dict[str, str] | None:
    for root in steam_roots():
        for library in library_folders(root):
            manifest = library / "steamapps" / f"appmanifest_{appid}.acf"
            if manifest.is_file():
                data = parse_acf(manifest.read_text(errors="replace"))
                data["library"] = str(library)
                return data
    return None


def steam_installed_apps() -> list[dict[str, str]]:
    apps: dict[str, dict[str, str]] = {}
    for root in steam_roots():
        for library in library_folders(root):
            for manifest in (library / "steamapps").glob("appmanifest_*.acf"):
                data = parse_acf(manifest.read_text(errors="replace"))
                if data.get("appid") and data["appid"] not in apps:
                    apps[data["appid"]] = data
    return sorted(apps.values(), key=lambda a: a.get("name", "").lower())


def manifest_installed(manifest: dict[str, str] | None) -> bool:
    try:
        return bool(manifest) and int(manifest.get("StateFlags", "0")) & 4 == 4
    except ValueError:
        return False


def is_steam_tool(name: str) -> bool:
    return bool(re.search(r"proton|steamworks|steam linux runtime|steamvr", name, re.I))


def parse_steam_appid(text: str) -> tuple[str, str | None]:
    """Accept '413150' or a store URL. Returns (appid, title from URL or None)."""
    text = text.strip()
    if text.isdigit():
        return text, None
    match = re.search(r"(?:store\.steampowered\.com|steamcommunity\.com)/app/(\d+)(?:/([^/?#]+))?", text)
    if match:
        title = urllib.parse.unquote(match.group(2)).replace("_", " ") if match.group(2) else None
        return match.group(1), title
    raise UserError(f"'{text}' isn't a Steam App ID.",
                    "Use the number from the game's Steam store page, e.g. store.steampowered.com/app/413150 "
                    "→ 413150. You can also paste the whole store link.")


def launch_steam(url: str) -> None:
    command = steam_command()
    if command is None:
        raise UserError("Steam isn't installed.", "Install it with: lgme setup")
    subprocess.Popen(command + [url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


SYSTEM_COMPAT_DIRS = (Path("/usr/share/steam/compatibilitytools.d"),
                      Path("/usr/local/share/steam/compatibilitytools.d"))


def proton_label(path: Path) -> str:
    """Human-readable Proton version, e.g. 'GE-Proton11-6' (read from its version file)."""
    try:
        parts = (path.parent / "version").read_text(errors="replace").split()
        if len(parts) >= 2:
            return parts[1]
    except OSError:
        pass
    return path.parent.name


def is_ge_proton(path: Path) -> bool:
    return bool(re.search(r"ge-proton|proton-ge", f"{path.parent.name} {proton_label(path)}", re.I))


def compat_tool_dirs() -> list[Path]:
    dirs = [root / "compatibilitytools.d" for root in steam_roots()]
    return dirs + list(SYSTEM_COMPAT_DIRS)


def find_protons() -> list[Path]:
    """All Proton builds found, best first: GE-Proton, other custom builds, then Valve's Proton."""
    groups: list[list[Path]] = [[], [], []]
    seen: set[Path] = set()

    def add(path: Path, group: int) -> None:
        if path.is_file() and path.resolve() not in seen:
            seen.add(path.resolve())
            groups[group].append(path)

    for directory in compat_tool_dirs():
        if directory.is_dir():
            for proton in directory.glob("*/proton"):
                add(proton, 0 if is_ge_proton(proton) else 1)
    for root in steam_roots():
        for library in library_folders(root):
            for proton in (library / "steamapps" / "common").glob("Proton*/proton"):
                add(proton, 2)
    ordered: list[Path] = []
    for group in groups:
        ordered += sorted(group, key=lambda p: natural_key(proton_label(p)), reverse=True)
    return ordered


def proton_wine_bins(proton: Path) -> tuple[Path, Path] | None:
    for sub in ("files", "dist"):
        wine = proton.parent / sub / "bin" / "wine"
        if wine.is_file():
            return wine, wine.with_name("wineserver")
    return None


def prefix_kind(prefix: Path) -> str:
    """'proton' (Proton compatdata with pfx/), 'wine' (plain Wine prefix) or 'empty'."""
    if (prefix / "pfx").is_dir():
        return "proton"
    if (prefix / "system.reg").is_file() or (prefix / "drive_c").is_dir():
        return "wine"
    return "empty"


def prefix_drive_c(prefix: Path) -> Path:
    return prefix / "pfx" / "drive_c" if prefix_kind(prefix) == "proton" else prefix / "drive_c"


def find_prefix_in(folder: Path) -> Path | None:
    """Look for an existing prefix shipped with or created inside a game folder."""
    if is_prefix_dir(folder):
        return folder
    for child in sorted(folder.iterdir()) if folder.is_dir() else []:
        if child.is_dir() and not child.is_symlink() and is_prefix_dir(child):
            return child
    return None


# --------------------------------------------------------------------------
# Running games
# --------------------------------------------------------------------------

DLL_FIXES = {
    "msvcp140": "vcrun2022", "vcruntime140": "vcrun2022", "vcruntime140_1": "vcrun2022",
    "msvcp140_1": "vcrun2022", "concrt140": "vcrun2022", "msvcp120": "vcrun2013", "msvcr120": "vcrun2013",
    "msvcp110": "vcrun2012", "msvcr110": "vcrun2012", "msvcp100": "vcrun2010", "msvcr100": "vcrun2010",
    "msvcp90": "vcrun2008", "msvcr90": "vcrun2008", "d3dcompiler_47": "d3dcompiler_47",
    "d3dcompiler_43": "d3dcompiler_43", "xaudio2_7": "xact", "x3daudio1_7": "xact",
    "xinput1_3": "xinput", "mscoree": "dotnet48", "physxloader": "physx", "msxml3": "msxml3",
    "msxml6": "msxml6", "xna": "xna40",
}
COMMON_FIXES = [
    ("vcrun2022", "Microsoft Visual C++ 2015-2022 runtime (most common)"),
    ("d3dx9", "DirectX 9 extras (older games)"),
    ("d3dcompiler_47", "DirectX shader compiler"),
    ("xact", "XAudio sound (older games with no sound)"),
    ("dotnet48", ".NET Framework 4.8 (slow to install)"),
    ("corefonts", "Microsoft fonts (missing/odd text)"),
]


def analyze_log(text: str, name: str) -> list[str]:
    hints: list[str] = []
    dlls: list[str] = []
    for dll in re.findall(r"err:module:import_dll (?:Library|Loading library) ([\w.\-]+)", text, re.I):
        if dll.lower() not in dlls:
            dlls.append(dll.lower())
    if dlls:
        verbs = sorted({DLL_FIXES[d.rsplit(".", 1)[0]] for d in dlls if d.rsplit(".", 1)[0] in DLL_FIXES}
                       | {"d3dx9" for d in dlls if d.startswith("d3dx9")})
        line = f"The game is missing Windows files: {', '.join(dlls[:5])}."
        if verbs:
            line += f" Fix: lgme fix {name} {' '.join(verbs)}"
        else:
            line += " Check the game folder for a '_CommonRedist' or 'Redist' folder; it says what the game needs."
        hints.append(line)
    if re.search(r"vkCreateInstance failed|VK_ERROR_INCOMPATIBLE_DRIVER|Failed to initialize Vulkan|"
                 r"No Vulkan|vulkan.*not (supported|available)", text, re.I):
        hints.append("Your graphics driver's Vulkan support seems missing. Run: lgme doctor")
    if re.search(r"easyanticheat|battleye", text, re.I):
        hints.append("This game uses anti-cheat, which often blocks Linux unless the developer enabled it. "
                     "Check the game on protondb.com.")
    if re.search(r"could not load kernel32\.dll|wine client error|is not owned by you|"
                 r"wineserver: .*version mismatch", text, re.I):
        hints.append(f"The game's Windows environment (prefix) looks broken. Try a fresh one: "
                     f"lgme set {name} --new-prefix   (the old one is kept so saves aren't lost)")
    if re.search(r"mscoree|\.NET Framework|mono", text, re.I) and "dotnet48" not in " ".join(hints):
        hints.append(f"The game may need .NET. Try: lgme fix {name} dotnet48")
    if re.search(r"libfuse\.so\.2|fusermount", text):
        hints.append("AppImages need FUSE 2. Install the 'fuse2' (Arch) or 'libfuse2' (Ubuntu) package.")
    if re.search(r"Exec format error|cannot execute binary file", text):
        hints.append("This program isn't built for your computer. Pick another one: "
                     f"lgme set {name} --pick-exe")
    if re.search(r"Permission denied", text):
        hints.append("Linux blocked access to a file. Make sure the game folder is yours and not on a "
                     "read-only drive (NTFS drives can cause this).")
    return hints


def notify(summary: str, body: str) -> None:
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name=LGME", summary, body], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_logged(command: list[str], env: dict[str, str], cwd: Path, log_path: Path,
               verbose: bool) -> tuple[int, float]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace", buffering=1) as log:
        log.write(f"# LGME {VERSION} - {now_iso()}\n# Command: {shlex.join(command)}\n# Folder: {cwd}\n\n")
        try:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            raise UserError(f"Couldn't start the program: {exc.strerror}.",
                            "The file may be damaged or not a program. See: lgme info") from exc
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace")
                log.write(line)
                if verbose:
                    sys.stdout.write(line)
            proc.wait()
        except KeyboardInterrupt:
            say("\nStopping the game…")
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return proc.returncode, time.monotonic() - start


def game_prefix(name: str, game: dict[str, Any]) -> Path:
    return Path(game.get("prefix") or prefixes_dir() / name).expanduser()


def resolve_runner(name: str, game: dict[str, Any], proton_override: str | None = None,
                   force_wine: bool = False) -> tuple[str, Path | None]:
    """Decide how to start a game. Returns (runner, proton path)."""
    if game.get("kind") == "native":
        return "native", None
    prefix = game_prefix(name, game)
    kind = prefix_kind(prefix)
    wanted = "wine" if force_wine else game.get("runner", "auto")
    proton: Path | None = None
    pinned = None if force_wine else (proton_override or game.get("proton"))
    if pinned:
        proton = Path(pinned).expanduser()
        if proton.is_dir():
            proton = proton / "proton"
        if not proton.is_file():
            raise UserError(f"The Proton version set for this game wasn't found: {pinned}",
                            f"Pick another with: lgme set {name} --proton auto")
        wanted = "proton"
    if wanted == "auto":
        if kind == "wine":
            wanted = "wine"
        elif kind == "proton":
            wanted = "proton"
        else:
            wanted = "proton" if find_protons() else "wine"
    if wanted == "proton":
        proton = proton or next(iter(find_protons()), None)
        if proton is None:
            if kind != "proton" and shutil.which("wine"):
                warn("Proton isn't installed, so using Wine instead.")
                wanted = "wine"
            else:
                raise UserError("Proton isn't installed, so this Windows game can't start.",
                                "Install GE-Proton automatically with: lgme setup")
        elif kind == "wine":
            raise UserError(
                "This game's Windows environment (prefix) was made with Wine, so Proton can't use it.",
                f"Use Wine for this game: lgme set {name} --runner wine\n"
                f"Or start with a fresh environment: lgme set {name} --new-prefix "
                "(the old one is kept; copy saves over if needed).")
    if wanted == "wine":
        if not shutil.which("wine"):
            raise UserError("Wine isn't installed, so this Windows game can't start.",
                            "Install it with: lgme setup")
        if kind == "proton":
            raise UserError(
                "This game's Windows environment (prefix) was made by Proton, so plain Wine can't use it.",
                f"Use Proton for this game: lgme set {name} --runner proton")
    return wanted, proton


def build_launch(name: str, game: dict[str, Any], exe: Path, extra_args: list[str],
                 proton_override: str | None = None, force_wine: bool = False
                 ) -> tuple[list[str], dict[str, str], str]:
    runner, proton = resolve_runner(name, game, proton_override, force_wine)
    env = os.environ.copy()
    env.update({k: str(v) for k, v in game.get("env", {}).items()})
    args = list(game.get("args", [])) + list(extra_args)
    if runner == "native":
        ensure_executable(exe)
        return [str(exe), *args], env, "Linux (native)"
    prefix = game_prefix(name, game)
    if prefix_kind(prefix) == "empty":
        say("First launch: setting up a Windows environment for this game. This can take a minute…")
    prefix.mkdir(parents=True, exist_ok=True)
    if runner == "proton":
        assert proton is not None
        env.pop("WINEPREFIX", None)
        env["STEAM_COMPAT_DATA_PATH"] = str(prefix)
        roots = steam_roots()
        client = roots[0] if roots else DATA_DIR / "steam-client"
        client.mkdir(parents=True, exist_ok=True)
        env.setdefault("STEAM_COMPAT_CLIENT_INSTALL_PATH", str(client))
        env.setdefault("PROTON_ENABLE_NVAPI", "1")
        return [str(proton), "run", str(exe), *args], env, f"Proton ({proton_label(proton)})"
    env["WINEPREFIX"] = str(prefix)
    env.setdefault("WINEDEBUG", "fixme-all")
    return ["wine", str(exe), *args], env, "Wine"


# --------------------------------------------------------------------------
# Adding games
# --------------------------------------------------------------------------


def new_prefix_path(name: str) -> tuple[Path, bool]:
    """A prefix location for a new game. Returns (path, reused_existing)."""
    path = prefixes_dir() / name
    if path.exists() and any(path.iterdir()):
        in_use = any(game_prefix(n, g) == path for n, g in read_registry().items() if g.get("kind") != "steam")
        if not in_use and confirm(f"There's an older Windows environment (prefix) named '{name}' at {path}, "
                                  "probably from a game you removed. It may contain saves. Reuse it?",
                                  default=True):
            return path, True
        i = 2
        while (prefixes_dir() / f"{name}-{i}").exists():
            i += 1
        path = prefixes_dir() / f"{name}-{i}"
    return path, False


def inside(path: Path, base: Path) -> bool:
    try:
        path = path.resolve()
        base = base.resolve()
    except OSError:
        return False
    return path != base and base in path.parents


def register_game(name: str, entry: dict[str, Any]) -> None:
    registry = read_registry()
    if name in registry:  # last-moment safety check; choose_name already avoids this
        raise UserError(f"You already have a game called '{name}'. LGME won't replace it.")
    entry.setdefault("added", now_iso())
    registry[name] = entry
    write_registry(registry)


def after_add(name: str, entry: dict[str, Any]) -> None:
    good(f"Added '{name}'.")
    tip(f"Play it with: lgme run {name}")
    if not is_interactive() or ASSUME_YES:
        return
    offer_quick_start(name, entry)
    if confirm("Play it now?", default=False):
        cmd_run(argparse.Namespace(name=name, proton=None, wine=False, verbose=False, game_args=[]))


def offer_quick_start(name: str, entry: dict[str, Any]) -> None:
    if not game_command_path(name).exists() and not shutil.which(name):
        if confirm(f"Start it by just typing '{name}' in a terminal?", default=True):
            try:
                write_game_command(name)
            except UserError as exc:
                warn(exc.message)
    if confirm("Add it to your app menu, so you can start it like any other app?", default=True):
        write_shortcut(name, entry)


def run_installer(installer: Path, name: str) -> tuple[Path, Path, bool]:
    """Run a Windows installer in a fresh prefix and return (game exe, prefix, reused)."""
    prefix, reused = new_prefix_path(name)
    temp_game = {"kind": "windows", "prefix": str(prefix)}
    say("")
    say(bold("This is a Windows installer.") + " LGME will run it in a private Windows environment for this game.")
    tip("Click through the installer and keep the suggested install folder.")
    tip("If it offers to install extras (DirectX, Visual C++), say yes.")
    if not confirm("Start the installer now?", default=True):
        raise UserError("Cancelled before running the installer.")
    command, env, runner_label = build_launch(name, temp_game, installer, [])
    say(f"Running the installer with {runner_label}… (wait for its window)")
    code, _ = run_logged(command, env, installer.parent, logs_dir() / f"{name}-installer.log", False)
    drive_c = prefix_drive_c(prefix)
    skip_top = {"windows", "users", "programdata", "dosdevices"}
    roots: list[Path] = []
    for top in sorted(drive_c.iterdir()) if drive_c.is_dir() else []:
        if not top.is_dir() or top.name.lower() in skip_top:
            continue
        if top.name.lower().startswith("program files"):
            roots += [d for d in sorted(top.iterdir()) if d.is_dir() and d.name.lower() not in
                      {"common files", "internet explorer", "windows media player", "windows nt", "windowspowershell"}]
        else:
            roots.append(top)
    candidates = [exe for root in roots for exe in find_launchables(root) if game_kind(exe) == "windows"]
    if not candidates:
        raise UserError(
            "The installer finished, but LGME couldn't find the installed game." +
            (f" (The installer exited with code {code}.)" if code else ""),
            f"If the installer failed or was cancelled, try again. If you picked a custom folder, register it with:\n"
            f"  lgme add <game folder> --prefix '{prefix}'\nInstaller log: {logs_dir() / f'{name}-installer.log'}")
    exe = choose_launchable(drive_c, candidates, None, name)
    return exe, prefix, reused


def add_archive(archive: Path, args: argparse.Namespace, downloaded: bool = False) -> None:
    if later_part(archive):
        raise UserError("This is a later part of a split archive.",
                        "Add the first part instead (the one ending in .part1.rar or .001), "
                        "with all parts in the same folder.")
    kind = archive_kind(archive)
    if kind is None:
        raise UserError(f"{archive.name} isn't an archive LGME can open.",
                        "Supported: ZIP, 7z, RAR and TAR files, game folders, and setup .exe installers.")
    registry = read_registry()
    name = choose_name(registry, archive_title(archive), args.name, needs_folder=True)
    base = games_dir()
    base.mkdir(parents=True, exist_ok=True)
    target = base / name
    partial = base / f".{name}.partial"
    if partial.exists():
        shutil.rmtree(partial)  # leftover from an interrupted LGME unpack
    size = archive_unpacked_size(archive, kind)
    check_space(base, size or archive.stat().st_size * 3, estimated=size is None)
    partial.mkdir()
    try:
        say(f"Unpacking {archive.name} into {target}")
        extract_archive(archive, kind, partial)
        candidates = find_launchables(partial)
        installers = find_installers(partial)
        installer_mode = bool(installers) and not args.exe and not candidates
        if not installer_mode:
            exe = choose_launchable(partial, candidates, args.exe, name)
            rel = exe.resolve().relative_to(partial.resolve())
        os.rename(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    entry: dict[str, Any] = {"title": archive_title(archive), "root": str(target), "managed_files": True}
    if installer_mode:
        moved = [target / i.relative_to(partial) for i in installers]
        installer = moved[0] if len(moved) == 1 else choose_launchable(target, moved, None, name)
        try:
            exe, prefix, reused = run_installer(installer, name)
        except BaseException:
            warn(f"The unpacked installer files were kept at {target}. You can retry with: lgme add '{installer}'")
            raise
        entry.update(kind="windows", exe=str(exe), prefix=str(prefix), managed_prefix=True)
    else:
        exe = target / rel
        ensure_executable(exe)
        entry.update(kind=game_kind(exe), exe=str(exe))
        if entry["kind"] == "windows":
            prefix, _ = new_prefix_path(name)
            entry.update(prefix=str(prefix), managed_prefix=True)
    if args.runner and args.runner != "auto":
        entry["runner"] = args.runner
    register_game(name, entry)
    if downloaded:
        archive.unlink(missing_ok=True)
        say("Removed the downloaded archive to save space (the game is unpacked).")
    after_add(name, entry)


def add_folder(folder: Path, args: argparse.Namespace, chosen_exe: Path | None = None) -> None:
    registry = read_registry()
    for other, game in registry.items():
        if game.get("root") and Path(game["root"]) == folder and not args.name:
            raise UserError(f"This folder is already added as '{other}'.", f"Play it with: lgme run {other}")
    name = choose_name(registry, folder.name, args.name)
    exe = chosen_exe or choose_launchable(folder, find_launchables(folder), args.exe, name)
    ensure_executable(exe)
    entry: dict[str, Any] = {"title": folder.name, "kind": game_kind(exe), "root": str(folder),
                             "exe": str(exe), "managed_files": False}
    if entry["kind"] == "windows":
        if args.prefix:
            prefix = Path(clean_user_path(args.prefix)).expanduser().resolve()
            if not prefix.is_dir():
                raise UserError(f"The prefix folder doesn't exist: {prefix}",
                                "Leave out --prefix and LGME will create a new one.")
            if prefix_kind(prefix) == "empty":
                warn(f"{prefix} doesn't look like a Wine/Proton prefix yet; it will be set up on first launch.")
            entry.update(prefix=str(prefix), managed_prefix=False)
        else:
            found = find_prefix_in(folder)
            if found and confirm(f"Found an existing Windows environment (prefix) at {found}. It probably has "
                                 "your saves and settings. Use it?", default=True):
                entry.update(prefix=str(found), managed_prefix=False)
            else:
                prefix, _ = new_prefix_path(name)
                entry.update(prefix=str(prefix), managed_prefix=True)
    if getattr(args, "runner", None) and args.runner != "auto":
        entry["runner"] = args.runner
    register_game(name, entry)
    say(f"The game stays where it is: {folder}")
    after_add(name, entry)


def add_installer_exe(installer: Path, args: argparse.Namespace) -> None:
    registry = read_registry()
    name = choose_name(registry, re.sub(r"^setup[_-]?", "", installer.stem, flags=re.I) or installer.stem, args.name)
    exe, prefix, reused = run_installer(installer, name)
    entry = {"title": name, "kind": "windows", "root": str(exe.parent), "exe": str(exe),
             "prefix": str(prefix), "managed_prefix": True, "managed_files": False}
    register_game(name, entry)
    after_add(name, entry)


def cmd_add(args: argparse.Namespace) -> None:
    source = clean_user_path(args.source)
    if source.startswith(("http://", "https://")):
        archive = download_archive(source)
        if archive.suffix.lower() == ".exe":
            add_installer_exe(archive, args)
        else:
            add_archive(archive, args, downloaded=True)
        return
    path = Path(source).expanduser()
    if not path.exists():
        raise UserError(f"Nothing found at: {path}",
                        "Check the spelling. Tip: drag the file or folder into the terminal to paste its path.")
    path = path.resolve()
    if path.is_dir():
        add_folder(path, args)
    elif path.suffix.lower() == ".exe":
        if INSTALLER_EXE.match(path.name) or re.match(r"^setup_", path.name, re.I):
            looks_like_installer = True
        elif is_interactive() and not ASSUME_YES:
            looks_like_installer = pick(f"What is {path.name}?",
                                        ["The game itself (it's already installed in this folder)",
                                         "An installer that sets up the game"]) == 1
        else:
            looks_like_installer = False
        if looks_like_installer:
            add_installer_exe(path, args)
        else:
            add_folder(path.parent, args, chosen_exe=path)
    elif path.suffix.lower() in NATIVE_SUFFIXES or is_elf(path):
        add_folder(path.parent, args, chosen_exe=path)
    else:
        add_archive(path, args)


# --------------------------------------------------------------------------
# Steam commands
# --------------------------------------------------------------------------


def cmd_steam_install(args: argparse.Namespace) -> None:
    appid, url_title = parse_steam_appid(args.appid)
    registry = read_registry()
    for other, game in registry.items():
        if game.get("kind") == "steam" and game.get("appid") == appid:
            say(f"This Steam game is already in your list as '{other}'.")
            if not manifest_installed(steam_manifest(appid)):
                launch_steam(f"steam://install/{appid}")
                say("Opened Steam's install window for it.")
            tip(f"Play it with: lgme run {other}")
            return
    if steam_command() is None:
        raise UserError("Steam isn't installed.", "Install it with: lgme setup")
    manifest = steam_manifest(appid)
    title = (manifest or {}).get("name") or url_title or f"steam-{appid}"
    name = choose_name(registry, title, args.name)
    entry = {"kind": "steam", "appid": appid, "title": title}
    register_game(name, entry)
    if manifest_installed(manifest):
        good(f"'{title}' is already installed in Steam. Added it as '{name}'.")
    else:
        launch_steam(f"steam://install/{appid}")
        good(f"Added '{name}' and asked Steam to install it.")
        tip("Steam will open an install window. Click Install. (You need to own the game on your account.)")
        tip("Windows-only game? In Steam: right-click it → Properties → Compatibility → "
            "tick 'Force the use of a specific Steam Play compatibility tool'.")
    tip(f"When it's installed, play with: lgme run {name}")
    if is_interactive() and not ASSUME_YES:
        offer_quick_start(name, entry)


def cmd_steam_list(_: argparse.Namespace) -> None:
    apps = [a for a in steam_installed_apps() if not is_steam_tool(a.get("name", ""))]
    if not apps:
        say("No installed Steam games found." if steam_roots() else "Steam doesn't seem to be set up yet.")
        return
    added = {g.get("appid"): n for n, g in read_registry().items() if g.get("kind") == "steam"}
    say(bold("Games installed in Steam:"))
    for app in apps:
        status = f"added as '{added[app['appid']]}'" if app["appid"] in added else f"add with: lgme steam add {app['appid']}"
        state = "" if manifest_installed(app) else " (still installing/updating)"
        say(f"  {app.get('name', '?')}{state}  —  App ID {app['appid']}, {status}")


# --------------------------------------------------------------------------
# Everyday commands
# --------------------------------------------------------------------------


def game_status(game: dict[str, Any]) -> str:
    if game.get("kind") == "steam":
        manifest = steam_manifest(game["appid"])
        if manifest_installed(manifest):
            return "ready (Steam)"
        return "installing in Steam" if manifest else "not installed in Steam yet"
    if not Path(game.get("exe", "")).is_file():
        return "files missing"
    return "ready"


def cmd_list(_: argparse.Namespace) -> None:
    registry = read_registry()
    if not registry:
        say("You haven't added any games yet.")
        tip("Add one with: lgme add <archive, folder, installer or link>")
        tip("Or from Steam:  lgme steam add <App ID or store link>")
        return
    width = max(len(n) for n in registry)
    say(bold(f"{'NAME':<{width}}  {'TYPE':<8}  STATUS"))
    for name, game in sorted(registry.items()):
        kind = {"steam": "Steam", "native": "Linux"}.get(game.get("kind", ""), "Windows")
        status = game_status(game)
        painted = _paint("32", status) if status.startswith("ready") else _paint("33", status)
        say(f"{name:<{width}}  {kind:<8}  {painted}")
    say("")
    tip("Play a game with: lgme run NAME    •    Details: lgme info NAME")


def cmd_info(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    game = registry[name]
    say(bold(name) + (f"  ({game['title']})" if game.get("title") and slug(game["title"]) != name else ""))
    say(f"  Status:   {game_status(game)}")
    if game.get("kind") == "steam":
        say(f"  Steam:    App ID {game['appid']}")
        manifest = steam_manifest(game["appid"])
        if manifest:
            say(f"  Location: {Path(manifest['library']) / 'steamapps/common' / manifest.get('installdir', '')}")
        return
    say(f"  Type:     {'Linux (native)' if game.get('kind') == 'native' else 'Windows'}")
    say(f"  Program:  {game.get('exe')}")
    say(f"  Folder:   {game.get('root')}" + ("  (managed by LGME)" if game.get("managed_files") else ""))
    if game.get("kind") != "native":
        prefix = game_prefix(name, game)
        say(f"  Prefix:   {prefix}  ({prefix_kind(prefix)})")
        try:
            runner, proton = resolve_runner(name, game)
            say(f"  Runs with: {'Proton ' + proton_label(proton) if proton else runner.capitalize()}"
                + ("" if game.get("runner", "auto") == "auto" and not game.get("proton") else "  (set by you)"))
        except UserError as exc:
            say(f"  Runs with: can't start yet: {exc.message}")
    if game.get("env"):
        say("  Settings: " + " ".join(f"{k}={v}" for k, v in game["env"].items()))
    if game.get("args"):
        say(f"  Options:  {shlex.join(game['args'])}")
    if game.get("last_played"):
        say(f"  Last played: {game['last_played'].replace('T', ' ')}")
    log = logs_dir() / f"{name}.log"
    if log.exists():
        say(f"  Last log: {log}")


def cmd_run(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    game = registry[name]
    if game.get("kind") == "steam":
        manifest = steam_manifest(game["appid"])
        if manifest and not manifest_installed(manifest):
            warn("Steam hasn't finished installing or updating this game yet. Opening it in Steam…")
        elif not manifest:
            warn("This game isn't installed in Steam yet. Steam will offer to install it.")
        launch_steam(f"steam://rungameid/{game['appid']}")
        good(f"Asked Steam to start {game.get('title', name)}.")
        return
    exe = Path(game.get("exe", ""))
    if not exe.is_file():
        raise UserError(f"The game's program file is missing: {exe}",
                        "Was the game folder moved, renamed or deleted? If you moved it, point LGME at "
                        f"the new place: lgme set {name} --exe \"/new/path/to/{exe.name}\"")
    command, env, label = build_launch(name, game, exe, args.game_args, args.proton, args.wine)
    log_path = logs_dir() / f"{name}.log"
    say(f"Starting {bold(game.get('title') or name)} with {label}…")
    if not args.verbose:
        say(f"(Game messages are saved to {log_path}. Close the game to come back here.)")
    code, seconds = run_logged(command, env, exe.parent, log_path, args.verbose)
    registry = read_registry()
    if name in registry:
        registry[name]["last_played"] = now_iso()
        write_registry(registry)
    if code == 0 and seconds >= 15:
        good(f"{name} closed. Played for {int(seconds // 60)} min.")
        return
    hints = analyze_log(log_path.read_text(errors="replace"), name)
    if code != 0 or hints or seconds < 15:
        warn(f"The game stopped after {int(seconds)} seconds" + (f" (exit code {code})." if code else "."))
        for hint in hints:
            tip(hint)
        if not hints:
            tip(f"See what happened: lgme logs {name}")
            tip(f"Check your setup: lgme doctor    •    Look up the game on protondb.com for fixes")
        if not sys.stdout.isatty():
            notify(f"{name} stopped", hints[0] if hints else f"See: lgme logs {name}")


def cmd_logs(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    log = logs_dir() / f"{name}.log"
    if not log.exists():
        raise UserError(f"No log yet for '{name}'.", f"A log is saved each time you run: lgme run {name}")
    lines = log.read_text(errors="replace").splitlines()
    say(bold(f"Last {min(args.lines, len(lines))} lines of {log}:"))
    for line in lines[-args.lines:]:
        say("  " + line)
    hints = analyze_log("\n".join(lines), name)
    if hints:
        say("")
        say(bold("What this might mean:"))
        for hint in hints:
            tip(hint)


def cmd_set(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    game = registry[name]
    if game.get("kind") == "steam":
        raise UserError("Steam games are configured in Steam itself.",
                        "In Steam: right-click the game → Properties.")
    changed = []
    root = Path(game.get("root") or Path(game.get("exe", ".")).parent)
    if args.pick_exe or args.exe:
        exe = (resolve_requested_exe(root, args.exe) if args.exe
               else choose_launchable(root, find_launchables(root), None, name))
        ensure_executable(exe)
        game["exe"], game["kind"] = str(exe), game_kind(exe)
        if game["kind"] == "windows" and not game.get("prefix"):
            game["prefix"] = str(prefixes_dir() / name)
        changed.append(f"program → {exe}")
    if args.prefix:
        prefix = Path(clean_user_path(args.prefix)).expanduser().resolve()
        if not prefix.is_dir():
            raise UserError(f"The prefix folder doesn't exist: {prefix}")
        game.update(prefix=str(prefix), managed_prefix=False)
        changed.append(f"prefix → {prefix}")
    if args.new_prefix:
        old = game_prefix(name, game)
        i = 2
        while (prefix := prefixes_dir() / f"{name}-{i}").exists() or prefix == old:
            i += 1
        game.update(prefix=str(prefix), managed_prefix=True)
        changed.append(f"fresh prefix → {prefix} (old one kept at {old})")
    if args.runner:
        if args.runner == "auto":
            game.pop("runner", None)
        else:
            game["runner"] = args.runner
        changed.append(f"runner → {args.runner}")
    if args.proton:
        if args.proton == "auto":
            game.pop("proton", None)
        else:
            protons = find_protons()
            match = next((p for p in protons if args.proton.lower() in
                          (p.parent.name.lower(), proton_label(p).lower())), None)
            path = match or Path(args.proton).expanduser()
            if path.is_dir():
                path = path / "proton"
            if not path.is_file():
                listing = ", ".join(proton_label(p) for p in protons) or "none installed"
                raise UserError(f"Proton '{args.proton}' wasn't found.", f"Installed versions: {listing}")
            game["proton"] = str(path)
        changed.append(f"Proton → {args.proton}")
    for item in args.env or []:
        if "=" not in item:
            raise UserError(f"'{item}' isn't a setting. Use the form NAME=value, e.g. --env DXVK_HUD=fps")
        key, value = item.split("=", 1)
        game.setdefault("env", {})[key] = value
        changed.append(f"setting {key}={value}")
    for key in args.unset_env or []:
        if game.get("env", {}).pop(key, None) is not None:
            changed.append(f"removed setting {key}")
    if args.args is not None:
        game["args"] = shlex.split(args.args)
        changed.append(f"launch options → {args.args or '(none)'}")
    if not changed:
        raise UserError("Nothing to change.", "See the options with: lgme set --help")
    registry[name] = game
    write_registry(registry)
    for line in changed:
        good(line)


def cmd_rename(args: argparse.Namespace) -> None:
    registry = read_registry()
    old = find_game(registry, args.old)
    new = slug(args.new)
    if new in registry:
        raise UserError(f"You already have a game called '{new}'.")
    game = registry.pop(old)
    if game.get("kind") != "steam" and game.get("kind") != "native":
        game["prefix"] = str(game_prefix(old, game))  # keep using the same prefix
    registry[new] = game
    write_registry(registry)
    old_log = logs_dir() / f"{old}.log"
    if old_log.exists():
        old_log.rename(logs_dir() / f"{new}.log")
    if desktop_file(old).exists():
        desktop_file(old).unlink()
        write_shortcut(new, game, quiet=True)
    if owns_command(game_command_path(old)):
        remove_game_command(old, quiet=True)
        try:
            write_game_command(new, quiet=True)
        except UserError as exc:
            warn(exc.message)
    good(f"Renamed '{old}' to '{new}'.")


def dir_size(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(dirpath) / f).lstat().st_size
            except OSError:
                pass
    return total


def cmd_remove(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    game = registry[name]
    if game.get("kind") == "steam":
        if not confirm(f"Remove '{name}' from LGME's list? (Steam keeps the game.)", default=True):
            raise UserError("Nothing was removed.")
        del registry[name]
        write_registry(registry)
        remove_shortcut(name, quiet=True)
        remove_game_command(name, quiet=True)
        good(f"Removed '{name}' from your list.")
        if confirm("Also open Steam to uninstall the game files?", default=False):
            launch_steam(f"steam://uninstall/{game['appid']}")
        return

    root = Path(game.get("root", ""))
    prefix = game_prefix(name, game) if game.get("kind") != "native" else None
    files_ok = bool(game.get("managed_files")) and inside(root, games_dir()) and root.is_dir()
    prefix_ok = bool(prefix and prefix.is_dir() and inside(prefix, prefixes_dir()))
    shared = prefix and any(n != name and game_prefix(n, g) == prefix for n, g in registry.items()
                            if g.get("kind") not in ("steam", "native"))
    if shared:
        prefix_ok = False

    say(bold(f"Removing '{name}'"))
    if not confirm(f"Remove '{name}' from your game list?", default=True):
        raise UserError("Nothing was removed.")
    delete_files = False
    if files_ok:
        size = human_size(dir_size(root))
        delete_files = args.delete_files or (not ASSUME_YES and confirm(
            f"Also delete the game files ({size} at {root})?", default=False))
    elif root.is_dir():
        say(f"Your game folder is left untouched: {root}")
    delete_prefix = False
    if prefix_ok:
        delete_prefix = args.delete_saves or (not ASSUME_YES and confirm(
            f"Also delete its Windows environment at {prefix}? This usually holds your SAVES and settings.",
            default=False))
    elif prefix and prefix.is_dir():
        say(f"The Windows environment (prefix) is left untouched: {prefix}")

    del registry[name]
    write_registry(registry)
    remove_shortcut(name, quiet=True)
    remove_game_command(name, quiet=True)
    good(f"Removed '{name}' from your list.")
    if delete_files:
        shutil.rmtree(root)
        good(f"Deleted the game files at {root}")
    elif files_ok:
        say(f"Game files kept at {root}")
    if delete_prefix and prefix:
        shutil.rmtree(prefix)
        good(f"Deleted the Windows environment at {prefix}")
    elif prefix_ok and prefix:
        say(f"Saves/settings kept at {prefix} (reused if you add a game with the same name).")


# --------------------------------------------------------------------------
# App menu shortcuts
# --------------------------------------------------------------------------


def desktop_file(name: str) -> Path:
    return APPLICATIONS_DIR / f"lgme-{name}.desktop"


def desktop_quote(arg: str) -> str:
    if re.fullmatch(r"[\w./+:@%-]+", arg):
        return arg
    escaped = re.sub(r'(["`$\\])', r"\\\1", arg)
    return f'"{escaped}"'


def launcher_command() -> list[str]:
    installed = shutil.which("lgme")
    if installed:
        return [installed]
    return [sys.executable, str(Path(__file__).resolve())]


def write_shortcut(name: str, game: dict[str, Any], quiet: bool = False) -> Path:
    path = desktop_file(name)
    exec_line = " ".join(desktop_quote(a) for a in launcher_command() + ["run", name])
    title = (game.get("title") or name).replace("\n", " ")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={title}\nComment=Play {title} (added with LGME)\n"
        f"Exec={exec_line.replace('%', '%%')}\nIcon=applications-games\nTerminal=false\n"
        "Categories=Game;\n")
    path.chmod(0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(path.parent)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not quiet:
        good(f"Added '{title}' to your app menu.")
    return path


def remove_shortcut(name: str, quiet: bool = False) -> None:
    path = desktop_file(name)
    if path.exists():
        path.unlink()
        if not quiet:
            good("Removed it from your app menu.")
    elif not quiet:
        say("There was no app-menu shortcut for it.")


BIN_DIR_NAME = ".local/bin"
COMMAND_MARKER = "# Created by LGME"


def bin_dir() -> Path:
    return HOME / BIN_DIR_NAME


def game_command_path(name: str) -> Path:
    return bin_dir() / name


def owns_command(path: Path) -> bool:
    try:
        return path.is_file() and COMMAND_MARKER in path.read_text(errors="replace")[:300]
    except OSError:
        return False


def path_hint() -> None:
    if str(bin_dir()) in os.environ.get("PATH", "").split(os.pathsep):
        return
    shell = Path(os.environ.get("SHELL", "")).name
    warn(f"{bin_dir()} isn't in your PATH yet. Do this once, then open a new terminal:")
    if shell == "fish":
        say(f"  fish_add_path {bin_dir()}")
    else:
        rc = "~/.zshrc" if shell == "zsh" else "~/.bashrc"
        say(f"  echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> {rc}")


def write_game_command(name: str, quiet: bool = False) -> Path:
    """Create a tiny command so typing just NAME in a terminal starts the game."""
    path = game_command_path(name)
    if path.exists() or path.is_symlink():
        if not owns_command(path):
            raise UserError(f"There's already a command called '{name}' ({path}); LGME won't replace it.",
                            f"Rename the game first (lgme rename {name} NEW), or just use: lgme {name}")
    else:
        other = shutil.which(name)
        if other:
            raise UserError(f"'{name}' is already a command on your system ({other}); LGME won't hide it.",
                            f"Rename the game first (lgme rename {name} NEW), or just use: lgme {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    launcher = " ".join(shlex.quote(a) for a in launcher_command())
    path.write_text(f"#!/bin/sh\n{COMMAND_MARKER}: starts the game '{name}'.\n"
                    f"# Remove it with: lgme command {name} --remove\n"
                    f"exec {launcher} run {shlex.quote(name)} -- \"$@\"\n")
    path.chmod(0o755)
    if not quiet:
        good(f"You can now start it by typing: {bold(name)}")
        path_hint()
    return path


def remove_game_command(name: str, quiet: bool = False) -> None:
    path = game_command_path(name)
    if owns_command(path):
        path.unlink()
        if not quiet:
            good(f"Removed the '{name}' command.")
    elif not quiet:
        say(f"There was no LGME command called '{name}'.")


def cmd_command(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    if args.remove:
        remove_game_command(name)
    else:
        write_game_command(name)


def cmd_shortcut(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    if args.remove:
        remove_shortcut(name)
    else:
        write_shortcut(name, registry[name])


# --------------------------------------------------------------------------
# winetricks fixes
# --------------------------------------------------------------------------


def cmd_fix(args: argparse.Namespace) -> None:
    registry = read_registry()
    name = find_game(registry, args.name)
    game = registry[name]
    if game.get("kind") in ("steam", "native"):
        hint = ("For Steam games, use protontricks (it works with Steam's Proton)." if game.get("kind") == "steam"
                else "Linux games don't need Windows components.")
        raise UserError("'lgme fix' installs Windows components for imported Windows games.", hint)
    verbs = list(args.components)
    if not verbs:
        if not is_interactive():
            raise UserError("Say which components to install, e.g.: lgme fix NAME vcrun2022",
                            "Common ones: " + ", ".join(v for v, _ in COMMON_FIXES))
        index = pick("Which Windows component does the game need?", [f"{v}: {d}" for v, d in COMMON_FIXES])
        verbs = [COMMON_FIXES[index][0]]
    if not shutil.which("winetricks"):
        raise UserError("This needs 'winetricks', which isn't installed.", "Install it with: lgme setup")
    runner, proton = resolve_runner(name, game)
    prefix = game_prefix(name, game)
    env = os.environ.copy()
    if runner == "proton":
        if prefix_kind(prefix) != "proton":
            raise UserError("Start the game once first, so its Windows environment gets created.",
                            f"Run: lgme run {name}   then try this again.")
        bins = proton_wine_bins(proton) if proton else None
        if bins is None:
            raise UserError("Couldn't find Wine inside this Proton version.",
                            "Try a GE-Proton version: lgme setup")
        env.update(WINEPREFIX=str(prefix / "pfx"), WINE=str(bins[0]), WINESERVER=str(bins[1]))
    else:
        prefix.mkdir(parents=True, exist_ok=True)
        env["WINEPREFIX"] = str(prefix)
    say(f"Installing {', '.join(verbs)} for {name}. Installer windows may pop up; accept them.")
    result = subprocess.run(["winetricks", "-q", *verbs], env=env)
    if result.returncode != 0:
        raise UserError("winetricks reported a problem.",
                        "Scroll up for its message. Component names must match winetricks names "
                        "(see: winetricks list-all).")
    good("Done. Try the game again: lgme run " + name)


# --------------------------------------------------------------------------
# Setup check and guided setup
# --------------------------------------------------------------------------

PACKAGES = {
    "pacman": {"steam": ["steam"], "wine": ["wine"], "winetricks": ["winetricks"], "7z": ["7zip"],
               "vulkaninfo": ["vulkan-tools"]},
    "apt": {"steam": ["steam-installer"], "wine": ["wine"], "winetricks": ["winetricks"], "7z": ["p7zip-full"],
            "vulkaninfo": ["vulkan-tools"]},
    "dnf": {"steam": ["steam"], "wine": ["wine"], "winetricks": ["winetricks"],
            "7z": ["p7zip", "p7zip-plugins"], "vulkaninfo": ["vulkan-tools"]},
    "zypper": {"steam": ["steam"], "wine": ["wine"], "winetricks": ["winetricks"], "7z": ["7zip"],
               "vulkaninfo": ["vulkan-tools"]},
}
INSTALL_COMMAND = {"pacman": ["pacman", "-S", "--needed"], "apt": ["apt", "install"],
                   "dnf": ["dnf", "install"], "zypper": ["zypper", "install"]}
DISTRO_NOTES = {
    "pacman": "On Arch, Steam needs the [multilib] repository enabled in /etc/pacman.conf "
              "(see wiki.archlinux.org/title/Official_repositories#multilib).",
    "apt": "On Ubuntu/Debian, Steam and Wine need 32-bit support: sudo dpkg --add-architecture i386 && sudo apt update",
    "dnf": "On Fedora, Steam comes from the RPM Fusion 'nonfree' repository (rpmfusion.org).",
}


def package_manager() -> str | None:
    for pm in ("pacman", "apt", "dnf", "zypper"):
        if shutil.which(pm):
            return pm
    return None


def vulkan_status() -> tuple[str, str]:
    if not shutil.which("vulkaninfo"):
        return "unknown", "can't check (vulkan-tools isn't installed)"
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=20,
                             errors="replace")
    except (OSError, subprocess.SubprocessError):
        return "bad", "the Vulkan check failed to run"
    devices = re.findall(r"deviceName\s*=\s*(.+)", out.stdout)
    real = [d.strip() for d in devices if "llvmpipe" not in d.lower()]
    if out.returncode == 0 and real:
        return "ok", real[0]
    if devices:
        return "warn", "only software rendering (llvmpipe) found; games will be very slow"
    return "bad", "no working Vulkan graphics driver found"


def collect_checks() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def add(key: str, label: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail, "fix": fix})

    steam = steam_command()
    add("steam", "Steam", "ok" if steam else "warn",
        " ".join(steam) if steam else "not installed (only needed for Steam games)", "Install Steam")
    protons = find_protons()
    ge = [p for p in protons if is_ge_proton(p)]
    if ge:
        add("proton", "GE-Proton", "ok", proton_label(ge[0]))
    elif protons:
        add("proton", "Proton", "ok", proton_label(protons[0]) + " (GE-Proton is recommended)",
            "Download GE-Proton")
    else:
        add("proton", "Proton", "bad", "not found (best way to run Windows games)", "Download GE-Proton")
    wine = shutil.which("wine")
    wine_version = ""
    if wine:
        try:
            wine_version = subprocess.run(["wine", "--version"], capture_output=True, text=True,
                                          timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    add("wine", "Wine", "ok" if wine else ("warn" if protons else "bad"),
        wine_version or wine or "not installed (backup for when Proton isn't available)", "Install Wine")
    add("winetricks", "winetricks", "ok" if shutil.which("winetricks") else "warn",
        shutil.which("winetricks") or "not installed (used by 'lgme fix' for missing DLLs)",
        "Install winetricks")
    tool = external_extractor()
    add("7z", "7-Zip", "ok" if tool else "warn",
        tool or "not installed (needed for .7z and .rar archives)", "Install 7-Zip")
    status, detail = vulkan_status()
    add("vulkaninfo", "Graphics (Vulkan)", status, detail,
        "Install vulkan-tools" if status == "unknown" else "")
    base = games_dir()
    free = shutil.disk_usage(existing_parent(base)).free
    add("space", "Free space", "ok" if free > 20 * 1024 ** 3 else "warn",
        f"{human_size(free)} free for games in {base}")
    on_path = shutil.which("lgme")
    add("command", "'lgme' command", "ok" if on_path else "warn",
        on_path or "not on your PATH yet (you must type ./lgme in this folder)",
        "Make 'lgme' work from anywhere")
    return checks


def print_checks(checks: list[dict[str, str]]) -> None:
    say(bold("LGME setup check") + "\n")
    for check in checks:
        line = f"{check['label']}: {check['detail']}"
        {"ok": good, "warn": warn, "bad": bad}.get(check["status"], warn)(line)
    say("")


def cmd_doctor(_: argparse.Namespace) -> None:
    checks = collect_checks()
    print_checks(checks)
    problems = [c for c in checks if c["status"] != "ok"]
    if not problems:
        good("Everything looks ready. Have fun!")
        return
    if any(c["fix"] for c in problems):
        tip("Run 'lgme setup' and LGME will offer to fix these for you.")
    if any(c["key"] == "vulkaninfo" and c["status"] == "bad" for c in problems):
        tip("For graphics, install your GPU's Vulkan driver: AMD/Intel → mesa vulkan drivers; "
            "NVIDIA → the proprietary NVIDIA driver. Search '<your distro> vulkan driver'.")


def install_packages(pm: str, packages: list[str]) -> bool:
    command = ["sudo", *INSTALL_COMMAND[pm], *packages]
    say("This will run: " + bold(shlex.join(command)))
    if not confirm("Run it now? (It will ask for your password.)", default=True):
        tip("You can run that command yourself later.")
        return False
    result = subprocess.run(command)
    if result.returncode != 0:
        warn("The package install didn't finish.")
        if pm in DISTRO_NOTES:
            tip(DISTRO_NOTES[pm])
        return False
    return True


def install_ge_proton() -> None:
    say("Looking up the latest GE-Proton release on GitHub…")
    request = urllib.request.Request(GE_PROTON_API, headers={"User-Agent": USER_AGENT,
                                                            "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise UserError(f"Couldn't reach GitHub to get GE-Proton ({getattr(exc, 'reason', exc)}).",
                        "Check your internet connection, or try again in a while (GitHub limits requests).")
    tag = release.get("tag_name", "")
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    tarball = next((n for n in assets if n.endswith(".tar.gz")), None)
    checksum = next((n for n in assets if n.endswith(".sha512sum")), None)
    if not tag or not tarball:
        raise UserError("The latest GE-Proton release has no download file LGME recognises.",
                        "Install it manually from github.com/GloriousEggroll/proton-ge-custom/releases")
    roots = steam_roots()
    native = [r for r in roots if ".var/app" not in str(r)]
    dest_dir = (native or roots or [HOME / ".local/share/Steam"])[0] / "compatibilitytools.d"
    if (dest_dir / tag / "proton").is_file():
        good(f"You already have the latest GE-Proton ({tag}).")
        return
    say(f"Latest version: {tag}")
    downloads = DATA_DIR / "downloads"
    part, _ = fetch(assets[tarball], downloads / f"{tarball}.part", "Downloading GE-Proton")
    if checksum:
        with urllib.request.urlopen(urllib.request.Request(assets[checksum], headers={"User-Agent": USER_AGENT}),
                                    timeout=30) as response:
            expected = response.read().decode().split()[0].lower()
        digest = hashlib.sha512()
        with part.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise UserError("The GE-Proton download was corrupted (checksum didn't match), so it was deleted.",
                            "Run 'lgme setup' again to retry.")
        good("Download verified")
    dest_dir.mkdir(parents=True, exist_ok=True)
    check_space(dest_dir, part.stat().st_size * 4, estimated=True)
    staging = dest_dir / f".{tag}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        extract_tar(part, staging)
        inner = [p for p in staging.iterdir() if p.is_dir()]
        source = inner[0] if len(inner) == 1 else staging
        if (dest_dir / source.name).exists():
            raise UserError(f"{dest_dir / source.name} already exists; LGME won't overwrite it.")
        os.rename(source, dest_dir / source.name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        part.unlink(missing_ok=True)
    good(f"Installed {tag} into {dest_dir}")
    tip("Restart Steam if it's open, so it can see the new Proton version too.")


def link_command() -> None:
    target = Path(__file__).resolve().with_name("lgme")
    if not target.is_file():
        target = Path(__file__).resolve()
    link = bin_dir() / "lgme"
    if link.exists() or link.is_symlink():
        if link.resolve() == target.resolve():
            good(f"'lgme' already points here ({link}).")
        else:
            warn(f"{link} already exists and points somewhere else; LGME won't replace it.")
            return
    else:
        bin_dir().mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        good(f"Created {link}")
    if str(bin_dir()) in os.environ.get("PATH", "").split(os.pathsep):
        good("You can now type 'lgme' from any folder.")
    path_hint()


def cmd_setup(args: argparse.Namespace) -> None:
    if args.update_proton:
        install_ge_proton()
        return
    checks = collect_checks()
    print_checks(checks)
    todo = [c for c in checks if c["status"] != "ok" and c["fix"]]
    if not todo:
        good("Nothing to fix. You're ready to play!")
        return
    pm = package_manager()
    wanted: list[str] = []
    for check in todo:
        if not confirm(f"{check['fix']}?", default=True):
            continue
        if check["key"] == "proton":
            try:
                install_ge_proton()
            except UserError as exc:
                bad(exc.message)
                if exc.hint:
                    tip(exc.hint)
        elif check["key"] == "command":
            link_command()
        else:
            wanted.append(check["key"])
    if wanted:
        if pm is None:
            if "steam" in wanted and shutil.which("flatpak"):
                if confirm("Install Steam from Flathub with flatpak?", default=True):
                    subprocess.run(["flatpak", "install", "-y", "flathub", FLATPAK_STEAM])
                wanted.remove("steam")
            if wanted:
                warn("LGME doesn't know your package manager. Please install these yourself: "
                     + ", ".join(wanted))
        else:
            packages = [p for key in wanted for p in PACKAGES[pm].get(key, [])]
            if packages:
                install_packages(pm, packages)
    say("")
    tip("Run 'lgme doctor' any time to check again.")


def cmd_settings(args: argparse.Namespace) -> None:
    settings = load_settings()
    if args.games_dir:
        path = Path(clean_user_path(args.games_dir)).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            raise UserError(f"LGME can't write to {path}.", "Pick a folder you own.")
        settings["games_dir"] = str(path)
        _write_json(settings_path(), settings)
        good(f"New games will be unpacked into {path}")
        say("(Games you already added stay where they are.)")
        return
    say(bold("LGME folders"))
    say(f"  New games:      {games_dir()}")
    say(f"  Prefixes/saves: {prefixes_dir()}")
    say(f"  Game list:      {registry_path()}")
    say(f"  Logs:           {logs_dir()}")
    say(f"  Downloads:      {DATA_DIR / 'downloads'}")
    tip("Change where new games go: lgme settings --games-dir /path")


def cmd_update(args: argparse.Namespace) -> None:
    me = Path(__file__).resolve()
    if (me.parent / ".git").exists() or (me.parent / "tests").is_dir():
        raise UserError("This copy of LGME runs from a source folder.", "Update it with: git pull")
    if not os.access(me.parent, os.W_OK):
        raise UserError(f"LGME can't update itself at {me} (no permission).",
                        "Reinstall with the install command from the README.")
    part = me.with_name(".lgme-update.part")
    part.unlink(missing_ok=True)
    fetch(RELEASE_DOWNLOAD, part, "Downloading the latest LGME")
    text = part.read_text(errors="replace")
    match = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    try:
        if not text.startswith("#!") or not match:
            raise SyntaxError("not an LGME file")
        compile(text, str(part), "exec")
    except SyntaxError as exc:
        part.unlink(missing_ok=True)
        raise UserError("The downloaded update looks damaged, so nothing was changed.",
                        "Try again in a few minutes.") from exc
    latest = match.group(1)
    if natural_key(latest) <= natural_key(VERSION) and not args.force:
        part.unlink(missing_ok=True)
        good(f"You already have the latest version ({VERSION}).")
        return
    part.chmod(0o755)
    os.replace(part, me)
    good(f"Updated LGME {VERSION} → {latest}")


# --------------------------------------------------------------------------
# Guided menu
# --------------------------------------------------------------------------


def menu_choose_game(action: str) -> str | None:
    registry = read_registry()
    if not registry:
        say("You haven't added any games yet.")
        return None
    names = sorted(registry)
    labels = [f"{n}  ({game_status(registry[n])})" for n in names]
    return names[pick(f"Which game do you want to {action}?", labels)]


def cmd_menu(_: argparse.Namespace) -> None:
    say(bold(f"LGME {VERSION}") + " — play games on Linux the easy way\n")
    if not read_registry() and not find_protons() and not shutil.which("wine"):
        warn("It looks like your computer isn't set up for Windows games yet.")
        if confirm("Run the guided setup first?", default=True):
            cmd_setup(argparse.Namespace(update_proton=False))
            say("")
    options = [
        ("Play a game", "play"),
        ("Add a game (archive, folder, installer or download link)", "add"),
        ("Add a game from Steam", "steam"),
        ("See my games", "list"),
        ("Fix a game that won't start", "fix"),
        ("Check / set up my computer", "setup"),
        ("Remove a game", "remove"),
        ("Show all commands", "help"),
        ("Quit", "quit"),
    ]
    while True:
        say("")
        choice = options[pick("What would you like to do?", [o[0] for o in options], recommend=False)][1]
        say("")
        try:
            if choice == "quit":
                return
            if choice == "play":
                name = menu_choose_game("play")
                if name:
                    cmd_run(argparse.Namespace(name=name, proton=None, wine=False, verbose=False, game_args=[]))
            elif choice == "add":
                say("Drag the game file or folder into this window (or paste a download link), then press Enter.")
                source = ask("File, folder or link")
                cmd_add(argparse.Namespace(source=source, name=None, exe=None, prefix=None, runner=None))
            elif choice == "steam":
                say("Find the game on store.steampowered.com and copy the page link (or just the App ID number).")
                appid = ask("Steam link or App ID")
                cmd_steam_install(argparse.Namespace(appid=appid, name=None))
            elif choice == "list":
                cmd_list(argparse.Namespace())
            elif choice == "fix":
                name = menu_choose_game("fix")
                if name:
                    if (logs_dir() / f"{name}.log").exists():
                        cmd_logs(argparse.Namespace(name=name, lines=15))
                    if read_registry()[name].get("kind") == "windows" and confirm(
                            "Install a missing Windows component for it?", default=True):
                        cmd_fix(argparse.Namespace(name=name, components=[]))
            elif choice == "setup":
                cmd_setup(argparse.Namespace(update_proton=False))
            elif choice == "remove":
                name = menu_choose_game("remove")
                if name:
                    cmd_remove(argparse.Namespace(name=name, delete_files=False, delete_saves=False))
            elif choice == "help":
                say(HELP_TEXT)
        except UserError as exc:
            print_error(exc)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

HELP_TEXT = """\
LGME-CLI — Linux Gaming Made Easy.

Just type  lgme  for a guided menu. Or use these commands:

  Getting ready
    lgme setup                  check your computer and install what's missing
    lgme doctor                 only check, change nothing

  Adding games
    lgme add game.zip           unpack a ZIP / 7z / RAR / TAR game archive
    lgme add ~/Games/MyGame     add a game folder that's already unpacked
    lgme add setup.exe          run a Windows installer, then add the game
    lgme add https://…/game.zip download from a direct link you trust, then add it
    lgme steam add 413150       install a Steam game (App ID or store link)
    lgme steam list             show games installed in Steam

  Playing
    lgme list                   your games
    lgme NAME                   play (same as: lgme run NAME)
    lgme command NAME           then just type NAME in a terminal to play
    lgme run NAME -- -windowed  pass options to the game after --

  When something goes wrong
    lgme logs NAME              last messages from the game, with tips
    lgme fix NAME vcrun2022     install a missing Windows component (winetricks)
    lgme set NAME --pick-exe    choose a different program to start
    lgme set NAME --runner wine use Wine instead of Proton (or proton/auto)
    lgme set NAME --new-prefix  fresh Windows environment (old one is kept)
    lgme info NAME              where everything is

  Tidying up
    lgme shortcut NAME          add to your app menu (--remove to undo)
    lgme rename OLD NEW
    lgme remove NAME            asks before deleting any files or saves
    lgme settings               see/change where games are stored
    lgme update                 get the newest version of LGME

Add -y to accept the safe default answer to every question.
"""


class FriendlyParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        message = message.replace("the following arguments are required", "missing")
        sys.stderr.write(_paint("31", "Error: ", sys.stderr) + message + "\n")
        sys.stderr.write(f"See examples with: lgme help   (or: {self.prog} --help)\n")
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    kwargs: dict[str, Any] = {}
    if sys.version_info >= (3, 14):
        kwargs["suggest_on_error"] = True
    p = FriendlyParser(prog="lgme", description="Add, set up and play games on Linux. "
                       "Run with no command for a guided menu.", **kwargs)
    p.add_argument("--version", action="version", version=f"LGME-CLI {VERSION}")
    p.add_argument("-y", "--yes", action="store_true", help="accept the safe default answer to every question")
    p.add_argument("--debug", action="store_true", help="show technical details when something fails")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                        help="accept the safe default answer to every question")
    common.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                        help="show technical details when something fails")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=FriendlyParser)

    def cmd(name: str, func, help_text: str, aliases: list[str] | None = None) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, aliases=aliases or [], help=help_text, description=help_text, parents=[common])
        sp.set_defaults(func=func)
        return sp

    add = cmd("add", cmd_add, "Add a game from an archive, folder, installer .exe or direct download link",
              ["install", "import"])
    add.add_argument("source", help="ZIP/7z/RAR/TAR file, game folder, setup.exe, or http(s) link")
    add.add_argument("--name", help="short name to use in commands (default: from the file/folder name)")
    add.add_argument("--exe", help="the program that starts the game (path inside the game folder)")
    add.add_argument("--prefix", help="existing Wine/Proton prefix to reuse (keeps your saves)")
    add.add_argument("--runner", choices=["auto", "proton", "wine"], help="how to run a Windows game")

    steam = sub.add_parser("steam", help="Install or list Steam games", parents=[common])
    steam_sub = steam.add_subparsers(dest="steam_command", metavar="ACTION", required=True,
                                     parser_class=FriendlyParser)
    s_add = steam_sub.add_parser("add", aliases=["install"], parents=[common],
                                 help="Install a Steam game by App ID or store link and add it to LGME")
    s_add.add_argument("appid", help="App ID (e.g. 413150) or store link")
    s_add.add_argument("--name", help="short name to use in commands")
    s_add.set_defaults(func=cmd_steam_install)
    s_list = steam_sub.add_parser("list", parents=[common], help="Show games installed in Steam")
    s_list.set_defaults(func=cmd_steam_list)

    cmd("list", cmd_list, "Show your games", ["ls"])

    run = cmd("run", cmd_run, "Play a game (put game options after --)", ["play", "start"])
    run.add_argument("name")
    run.add_argument("--proton", help="use a specific Proton version or path this time")
    run.add_argument("--wine", action="store_true", help="use Wine this time")
    run.add_argument("-v", "--verbose", action="store_true", help="show the game's messages here")

    info = cmd("info", cmd_info, "Show details about a game")
    info.add_argument("name")
    logs = cmd("logs", cmd_logs, "Show the last messages from a game, with tips")
    logs.add_argument("name")
    logs.add_argument("-n", "--lines", type=int, default=40)

    fix = cmd("fix", cmd_fix, "Install missing Windows components (e.g. vcrun2022, d3dx9) with winetricks")
    fix.add_argument("name")
    fix.add_argument("components", nargs="*", help="winetricks names, e.g. vcrun2022 d3dx9")

    st = cmd("set", cmd_set, "Change how a game starts")
    st.add_argument("name")
    st.add_argument("--exe", help="program that starts the game")
    st.add_argument("--pick-exe", action="store_true", help="choose the program again from a list")
    st.add_argument("--prefix", help="use this existing Wine/Proton prefix")
    st.add_argument("--new-prefix", action="store_true", help="start with a fresh prefix (old one is kept)")
    st.add_argument("--runner", choices=["auto", "proton", "wine"])
    st.add_argument("--proton", help="Proton version name (e.g. GE-Proton11-3), path, or 'auto'")
    st.add_argument("--env", action="append", metavar="NAME=VALUE", help="environment setting, e.g. DXVK_HUD=fps")
    st.add_argument("--unset-env", action="append", metavar="NAME")
    st.add_argument("--args", help="options always passed to the game, e.g. \"-windowed -nointro\" ('' to clear)")

    rn = cmd("rename", cmd_rename, "Rename a game")
    rn.add_argument("old")
    rn.add_argument("new")

    rm = cmd("remove", cmd_remove, "Remove a game (asks before deleting files or saves)", ["uninstall", "rm"])
    rm.add_argument("name")
    rm.add_argument("--delete-files", action="store_true", help="also delete game files LGME unpacked")
    rm.add_argument("--delete-saves", action="store_true", help="also delete the game's prefix (saves!)")

    gc = cmd("command", cmd_command, "Start a game by typing just its name in a terminal")
    gc.add_argument("name")
    gc.add_argument("--remove", action="store_true")

    sc = cmd("shortcut", cmd_shortcut, "Add a game to your app menu")
    sc.add_argument("name")
    sc.add_argument("--remove", action="store_true")

    cmd("doctor", cmd_doctor, "Check that your computer is ready for games", ["check"])
    setup = cmd("setup", cmd_setup, "Check your computer and install what's missing")
    setup.add_argument("--update-proton", action="store_true", help="download the newest GE-Proton")
    stg = cmd("settings", cmd_settings, "Show or change LGME's folders")
    stg.add_argument("--games-dir", help="where new games are unpacked")
    up = cmd("update", cmd_update, "Update LGME to the newest release")
    up.add_argument("--force", action="store_true", help="reinstall even if already up to date")
    cmd("help", lambda _: say(HELP_TEXT), "Show examples")
    cmd("menu", cmd_menu, "Open the guided menu")
    return p


def parser_commands(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public API for this
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def split_game_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Everything after '--' goes to the game: lgme run NAME -- -windowed."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def print_error(exc: UserError) -> None:
    sys.stdout.flush()
    sys.stderr.write(_paint("31", "Problem: ", sys.stderr) + exc.message + "\n")
    if exc.hint:
        for line in exc.hint.splitlines():
            sys.stderr.write(_paint("36", "→ ", sys.stderr) + line + "\n")


def main(argv: list[str] | None = None) -> int:
    global ASSUME_YES
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, game_args = split_game_args(argv)
    # Let values that start with '-' work: --args "-windowed" -> --args=-windowed
    for i in range(len(argv) - 1, 0, -1):
        if argv[i - 1] == "--args" and argv[i].startswith("-"):
            argv[i - 1:i + 1] = [f"--args={argv[i]}"]
    parser = build_parser()
    if argv and not argv[0].startswith("-") and argv[0] not in parser_commands(parser):
        try:  # "lgme wuchang" is the same as "lgme run wuchang"
            registry = read_registry()
            if argv[0] in registry or slug(argv[0]) in registry:
                argv = ["run", *argv]
        except UserError:
            pass
    args = parser.parse_args(argv)
    ASSUME_YES = bool(getattr(args, "yes", False))
    if not hasattr(args, "func"):
        if is_interactive():
            args.func = cmd_menu
        else:
            say(HELP_TEXT)
            return 0
    if game_args and args.func is not cmd_run:
        sys.stderr.write("Options after -- only work with 'lgme run'.\n")
        return 2
    args.game_args = game_args
    try:
        args.func(args)
    except UserError as exc:
        print_error(exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nCancelled.\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort friendly message
        if getattr(args, "debug", False):
            raise
        sys.stderr.write(_paint("31", "Problem: ", sys.stderr) +
                         f"Something unexpected went wrong ({type(exc).__name__}: {exc}).\n"
                         "→ Run the same command with --debug to see the details, and please report it.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
