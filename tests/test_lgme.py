"""Tests for LGME. They never touch your real games, Steam, Wine or network."""

from __future__ import annotations

import http.server
import io
import json
import os
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lgme as gz  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every LGME folder at a temporary home directory."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(gz, "HOME", h)
    monkeypatch.setattr(gz, "DATA_DIR", h / ".local/share/lgme")
    monkeypatch.setattr(gz, "CONFIG_DIR", h / ".config/lgme")
    monkeypatch.setattr(gz, "APPLICATIONS_DIR", h / ".local/share/applications")
    monkeypatch.setattr(gz, "DEFAULT_GAMES_DIR", h / "Games/LGME")
    monkeypatch.setattr(gz, "ASSUME_YES", False)
    monkeypatch.setattr(gz, "SYSTEM_COMPAT_DIRS", ())
    gz.clear_caches()
    monkeypatch.setattr(gz, "is_interactive", lambda: False)
    monkeypatch.delenv("LGME_GAMES_DIR", raising=False)
    return h


def fake_tools(monkeypatch, available: set[str]):
    real = gz.shutil.which
    monkeypatch.setattr(gz.shutil, "which",
                        lambda name, *a, **k: f"/usr/bin/{name}" if name in available else
                        (real(name, *a, **k) if name in {"7z", "7zz", "bsdtar", "7za"} else None))


def make_zip(path: Path, files: dict[str, bytes], modes: dict[str, int] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if modes and name in modes:
                info.external_attr = (0o100000 | modes[name]) << 16
            zf.writestr(info, data)
    return path


# ---------------------------------------------------------------- names


def test_slug_is_easy_to_type():
    assert gz.slug("Stardew Valley!") == "stardew-valley"
    assert gz.slug("  --My__Game v1.2 ") == "my-game-v1.2"
    assert gz.slug("...") == "game"


def test_find_game_matches_loosely_and_suggests(home):
    registry = {"stardew-valley": {"kind": "steam", "appid": "1"}, "hades": {"title": "Hades II"}}
    assert gz.find_game(registry, "Stardew Valley") == "stardew-valley"
    assert gz.find_game(registry, "Hades II") == "hades"
    with pytest.raises(gz.UserError) as err:
        gz.find_game(registry, "stardew")
    assert "stardew-valley" in err.value.hint


# ---------------------------------------------------------------- command line


def test_game_args_after_double_dash_and_proton_after_name():
    argv, game_args = gz.split_game_args(["run", "wuchang", "--proton", "/x/proton", "--", "-windowed"])
    args = gz.build_parser().parse_args(argv)
    assert args.name == "wuchang" and args.proton == "/x/proton"
    assert game_args == ["-windowed"]


def test_yes_flag_works_before_and_after_command():
    p = gz.build_parser()
    assert p.parse_args(["-y", "list"]).yes is True
    assert p.parse_args(["remove", "x", "-y"]).yes is True
    assert p.parse_args(["list"]).yes is False


def test_dash_values_for_launch_options(home):
    gz.write_registry({"g": {"kind": "windows", "exe": "/x/G.exe", "root": "/x"}})
    assert gz.main(["set", "g", "--args", "-windowed -nointro"]) == 0
    assert gz.read_registry()["g"]["args"] == ["-windowed", "-nointro"]


def test_clean_user_path_handles_drag_and_drop():
    assert gz.clean_user_path("'/home/a/My Game.zip' ") == "/home/a/My Game.zip"
    assert gz.clean_user_path("/home/a/My\\ Game.zip") == "/home/a/My Game.zip"
    assert gz.clean_user_path("file:///home/a/My%20Game.zip") == "/home/a/My Game.zip"


def test_steam_appid_from_store_link():
    assert gz.parse_steam_appid("413150") == ("413150", None)
    assert gz.parse_steam_appid("https://store.steampowered.com/app/413150/Stardew_Valley/") == \
        ("413150", "Stardew Valley")
    with pytest.raises(gz.UserError):
        gz.parse_steam_appid("stardew")


# ---------------------------------------------------------------- registry


def test_damaged_registry_gives_plain_error(home):
    gz.registry_path().parent.mkdir(parents=True)
    gz.registry_path().write_text("{not json")
    with pytest.raises(gz.UserError) as err:
        gz.read_registry()
    assert "damaged" in err.value.message


def test_registry_write_keeps_backup(home):
    gz.write_registry({"a": {}})
    gz.write_registry({"b": {}})
    assert json.loads(gz.registry_path().with_name("games.json.bak").read_text()) == {"a": {}}


# ---------------------------------------------------------------- archives


def test_zip_rejects_path_traversal(home, tmp_path):
    archive = make_zip(tmp_path / "evil.zip", {"../escape.txt": b"x"})
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(gz.UserError):
        gz.extract_zip(archive, target)
    assert not (tmp_path / "escape.txt").exists()


def test_zip_windows_backslashes_and_exec_bits(home, tmp_path):
    archive = make_zip(tmp_path / "g.zip", {"Game\\Data\\a.txt": b"hi", "Game/run.x86_64": b"\x7fELF"},
                       modes={"Game/run.x86_64": 0o755})
    target = tmp_path / "out"
    target.mkdir()
    gz.extract_zip(archive, target)
    assert (target / "Game/Data/a.txt").read_bytes() == b"hi"
    assert os.access(target / "Game/run.x86_64", os.X_OK)


def test_tar_rejects_path_traversal(home, tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    target = tmp_path / "out"
    target.mkdir()
    with pytest.raises(gz.UserError):
        gz.extract_tar(archive, target)
    assert not (tmp_path / "escape.txt").exists()


def test_archive_detection(tmp_path):
    assert gz.archive_kind(make_zip(tmp_path / "a.bin", {"x": b"1"})) == "zip"
    (tmp_path / "b.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + b"0" * 20)
    assert gz.archive_kind(tmp_path / "b.rar") == "rar"
    (tmp_path / "c.txt").write_text("hello")
    assert gz.archive_kind(tmp_path / "c.txt") is None
    assert gz.later_part(Path("game.part2.rar")) and not gz.later_part(Path("game.part1.rar"))
    assert gz.archive_title(Path("Cool Game v1.0.part1.rar")) == "Cool Game v1.0"


# ---------------------------------------------------------------- finding the program


def test_find_launchables_skips_helpers_redists_and_prefixes(tmp_path):
    root = tmp_path / "Cool Game"
    for rel in ["CoolGame.exe", "UnityCrashHandler64.exe", "_CommonRedist/vcredist_x64.exe",
                "prefix/pfx/system.reg", "prefix/pfx/drive_c/windows/notepad.exe",
                "Engine/Binaries/CrashReportClient.exe", "setup.exe", "Tools/Editor.exe"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"MZ")
    names = [p.name for p in gz.find_launchables(root)]
    assert names == ["CoolGame.exe", "Editor.exe"]
    assert [p.name for p in gz.find_installers(root)] == ["setup.exe"]


def test_find_prefix_in_game_folder(tmp_path):
    (tmp_path / "prefix/pfx").mkdir(parents=True)
    (tmp_path / "prefix/pfx/system.reg").write_text("")
    assert gz.find_prefix_in(tmp_path) == tmp_path / "prefix"
    assert gz.prefix_kind(tmp_path / "prefix") == "proton"


# ---------------------------------------------------------------- Proton & runners


def test_proton_versions_sorted_naturally_ge_first(home):
    tools = home / ".local/share/Steam/compatibilitytools.d"
    for name in ["GE-Proton9-27", "GE-Proton10-4", "dwproton-11.0-14"]:
        (tools / name).mkdir(parents=True)
        (tools / name / "proton").write_text("")
    assert [p.parent.name for p in gz.find_protons()] == ["GE-Proton10-4", "GE-Proton9-27", "dwproton-11.0-14"]


def test_runner_refuses_to_mix_wine_prefix_with_proton(home, monkeypatch):
    fake_tools(monkeypatch, {"wine"})
    prefix = home / "wineprefix"
    (prefix / "drive_c").mkdir(parents=True)
    proton = home / "GE-Proton10-1/proton"
    proton.parent.mkdir()
    proton.write_text("")
    game = {"kind": "windows", "prefix": str(prefix)}
    assert gz.resolve_runner("g", game) == ("wine", None)  # auto follows the prefix
    with pytest.raises(gz.UserError) as err:
        gz.resolve_runner("g", game, proton_override=str(proton))
    assert "made with Wine" in err.value.message


def test_proton_launch_uses_existing_compatdata(home, monkeypatch):
    prefix = home / "WUCHANG/prefix"
    (prefix / "pfx").mkdir(parents=True)
    proton = home / ".local/share/Steam/compatibilitytools.d/GE-Proton11-3/proton"
    proton.parent.mkdir(parents=True)
    proton.write_text("")
    exe = home / "WUCHANG/Game.exe"
    exe.write_bytes(b"MZ")
    command, env, label, wait = gz.build_launch("wuchang", {"kind": "windows", "prefix": str(prefix)},
                                                exe, ["-dx12"])
    assert command == [str(proton), "run", str(exe), "-dx12"]
    assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix)
    assert "GE-Proton11-3" in label
    # Proton hides Wine's errors by default; LGME turns on just the cheap "missing DLL" ones
    assert env["WINEDEBUG"] == "-all,err+module" and env["DXVK_LOG_PATH"] == "none"
    assert wait is None  # this fake Proton has no wineserver


def test_user_settings_override_quiet_logging(home):
    prefix = home / "p"
    proton = home / ".local/share/Steam/compatibilitytools.d/GE-Proton11-3/proton"
    (proton.parent / "files/bin").mkdir(parents=True)
    proton.write_text("")
    (proton.parent / "files/bin/wine").write_text("")
    (proton.parent / "files/bin/wineserver").write_text("")
    exe = home / "G.exe"
    exe.write_bytes(b"MZ")
    game = {"kind": "windows", "prefix": str(prefix), "env": {"WINEDEBUG": "+all"}}
    _, env, _, wait = gz.build_launch("g", game, exe, [])
    assert env["WINEDEBUG"] == "+all"
    assert wait[-2:] == [str(proton.parent / "files/bin/wineserver"), "-w"]
    assert f"WINEPREFIX={prefix / 'pfx'}" in wait


def test_log_hints_are_plain_language():
    hints = gz.analyze_log("err:module:import_dll Library MSVCP140.dll (needed by Game.exe) not found\n", "g")
    assert any("lgme fix g vcrun2022" in h for h in hints)


# ---------------------------------------------------------------- end-to-end flows


def args(**kw):
    base = dict(name=None, exe=None, prefix=None, runner=None)
    base.update(kw)
    return gz.Args(**base)


def test_add_zip_then_refuse_overwrite(home, tmp_path):
    archive = make_zip(tmp_path / "Cool Game.zip", {"Cool Game/CoolGame.exe": b"MZ", "Cool Game/data.pak": b"1"})
    gz.cmd_add(args(source=str(archive)))
    registry = gz.read_registry()
    game = registry["cool-game"]
    assert game["kind"] == "windows" and game["managed_files"]
    assert Path(game["exe"]).is_file()
    assert archive.exists()  # the user's own archive is never deleted
    with pytest.raises(gz.UserError) as err:
        gz.cmd_add(args(source=str(archive)))
    assert "won't replace" in err.value.message
    assert not list(gz.games_dir().glob(".*.partial"))


def test_failed_add_cleans_up(home, tmp_path):
    archive = make_zip(tmp_path / "empty.zip", {"readme.txt": b"no game here"})
    with pytest.raises(gz.UserError):
        gz.cmd_add(args(source=str(archive)))
    assert not (gz.games_dir() / "empty").exists()
    assert gz.read_registry() == {}


def test_add_folder_reuses_existing_prefix_and_remove_keeps_user_files(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gz, "ASSUME_YES", True)
    folder = tmp_path / "WUCHANG"
    (folder / "Game").mkdir(parents=True)
    (folder / "Game/Project_Plague.exe").write_bytes(b"MZ")
    (folder / "prefix/pfx").mkdir(parents=True)
    (folder / "prefix/pfx/system.reg").write_text("")
    gz.cmd_add(args(source=str(folder), name="wuchang"))
    game = gz.read_registry()["wuchang"]
    assert game["prefix"] == str(folder / "prefix") and not game["managed_prefix"]
    gz.cmd_remove(gz.Args(name="wuchang", delete_files=True, delete_saves=True))
    assert "wuchang" not in gz.read_registry()
    assert (folder / "Game/Project_Plague.exe").exists()  # not LGME's to delete
    assert (folder / "prefix/pfx/system.reg").exists()


def test_remove_deletes_only_when_asked(home, tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "g.zip", {"G.exe": b"MZ"})
    gz.cmd_add(args(source=str(archive)))
    root = Path(gz.read_registry()["g"]["root"])
    monkeypatch.setattr(gz, "ASSUME_YES", True)
    gz.cmd_remove(gz.Args(name="g", delete_files=False, delete_saves=False))
    assert root.exists()  # -y alone never deletes files
    gz.cmd_add(args(source=str(archive), name="g2"))
    root2 = Path(gz.read_registry()["g2"]["root"])
    gz.cmd_remove(gz.Args(name="g2", delete_files=True, delete_saves=False))
    assert not root2.exists()


def test_steam_add_never_overwrites(home, monkeypatch):
    calls = []
    monkeypatch.setattr(gz, "steam_command", lambda: ["steam"])
    monkeypatch.setattr(gz, "launch_steam", calls.append)
    gz.write_registry({"stardew-valley": {"kind": "windows", "exe": "/x.exe"}})
    with pytest.raises(gz.UserError):
        gz.cmd_steam_install(gz.Args(
            appid="https://store.steampowered.com/app/413150/Stardew_Valley/", name=None))
    assert gz.read_registry()["stardew-valley"]["kind"] == "windows"
    gz.cmd_steam_install(gz.Args(appid="413150", name="sdv"))
    assert gz.read_registry()["sdv"] == {"kind": "steam", "appid": "413150", "title": "steam-413150",
                                         "added": gz.read_registry()["sdv"]["added"]}
    assert calls == ["steam://install/413150"]


def test_steam_manifest_reading(home):
    apps = home / ".local/share/Steam/steamapps"
    apps.mkdir(parents=True)
    (apps / "appmanifest_413150.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"413150"\n\t"name"\t\t"Stardew Valley"\n\t"StateFlags"\t\t"4"\n}\n')
    manifest = gz.steam_manifest("413150")
    assert manifest["name"] == "Stardew Valley" and gz.manifest_installed(manifest)


def test_shortcut_quotes_paths_with_spaces(home, monkeypatch):
    monkeypatch.setattr(gz, "launcher_command", lambda: ["/usr/bin/python3", "/home/a/Game run env/lgme.py"])
    path = gz.write_shortcut("cool-game", {"title": "Cool Game"}, quiet=True)
    text = path.read_text()
    assert 'Exec=/usr/bin/python3 "/home/a/Game run env/lgme.py" run cool-game' in text


# ---------------------------------------------------------------- downloads


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/page":
            body, ctype = b"<html></html>", "text/html"
        else:
            body, ctype = self.server.zip_bytes, "application/zip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Web Game/WebGame.exe", b"MZ")
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.zip_bytes = buf.getvalue()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_download_link_to_web_page_is_explained(home, server):
    with pytest.raises(gz.UserError) as err:
        gz.cmd_add(args(source=f"{server}/page"))
    assert "web page" in err.value.message


def test_download_and_add(home, server):
    gz.cmd_add(args(source=f"{server}/files/web-game.zip"))
    assert "web-game" in gz.read_registry()
    assert not list((gz.DATA_DIR / "downloads").iterdir())  # downloaded copy cleaned up


def test_main_returns_friendly_error(home, capsys):
    assert gz.main(["run", "nothing-here"]) == 1
    err = capsys.readouterr().err
    assert "Problem:" in err and "Traceback" not in err


# ---------------------------------------------------------------- "just type the name"


def test_lgme_name_means_run(home, monkeypatch):
    ran = []
    monkeypatch.setattr(gz, "cmd_run", lambda a: ran.append((a.name, a.game_args)))
    gz.write_registry({"wuchang": {"kind": "windows", "exe": "/x/G.exe"}})
    assert gz.main(["wuchang", "--", "-dx12"]) == 0
    assert ran == [("wuchang", ["-dx12"])]


def test_game_command_created_safely_and_cleaned_up(home, monkeypatch):
    monkeypatch.setattr(gz, "launcher_command", lambda: ["/opt/game zip/lgme"])
    fake_tools(monkeypatch, set())
    gz.write_registry({"wuchang": {"kind": "native", "exe": "/x/run.sh", "root": "/x"}})
    path = gz.write_game_command("wuchang", quiet=True)
    text = path.read_text()
    assert "exec '/opt/game zip/lgme' run wuchang -- \"$@\"" in text and os.access(path, os.X_OK)
    gz.cmd_rename(gz.Args(old="wuchang", new="wu"))
    assert not path.exists() and gz.game_command_path("wu").exists()
    monkeypatch.setattr(gz, "ASSUME_YES", True)
    gz.cmd_remove(gz.Args(name="wu", delete_files=False, delete_saves=False))
    assert not gz.game_command_path("wu").exists()


def test_game_command_never_replaces_existing(home, monkeypatch):
    fake_tools(monkeypatch, {"steam"})
    with pytest.raises(gz.UserError):
        gz.write_game_command("steam", quiet=True)  # a real system command
    mine = gz.game_command_path("mytool")
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(gz.UserError):
        gz.write_game_command("mytool", quiet=True)
    assert mine.read_text() == "#!/bin/sh\necho mine\n"


# ---------------------------------------------------------------- bugs found in testing


def test_game_cannot_be_named_after_a_command(home, monkeypatch, tmp_path):
    (tmp_path / "list").mkdir()
    (tmp_path / "list/Game.exe").write_bytes(b"MZ")
    with pytest.raises(gz.UserError) as err:
        gz.cmd_add(args(source=str(tmp_path / "list")))
    assert "LGME's own commands" in err.value.message
    assert gz.RESERVED_NAMES >= gz.parser_commands(gz.build_parser())


def test_name_shortcut_works_after_flags(home, monkeypatch):
    ran = []
    monkeypatch.setattr(gz, "cmd_run", lambda a: ran.append(a.name))
    gz.write_registry({"wuchang": {"kind": "windows", "exe": "/x/G.exe"}})
    assert gz.main(["-y", "wuchang"]) == 0 and ran == ["wuchang"]


def test_empty_source_is_not_the_current_folder(home):
    with pytest.raises(gz.UserError):
        gz.cmd_add(args(source=""))
    assert gz.read_registry() == {}


def test_ctrl_d_is_a_clean_cancel(home, monkeypatch):
    def eof(_):
        raise EOFError
    monkeypatch.setattr(gz, "cmd_list", eof)
    assert gz.main(["list"]) == 130


def test_os_errors_are_explained(home, monkeypatch, capsys):
    def full(_):
        raise OSError(28, "No space left on device", "/games/x")
    monkeypatch.setattr(gz, "cmd_list", full)
    assert gz.main(["list"]) == 1
    err = capsys.readouterr().err
    assert "disk is full" in err and "Something unexpected" not in err


def test_logs_zero_lines_and_tail_only(home, capsys):
    gz.write_registry({"g": {"kind": "native", "exe": "/x/g", "root": "/x"}})
    log = gz.logs_dir() / "g.log"
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {i}\n" for i in range(200000)) + "err:module:import_dll Library d3dx9_43.dll\n")
    gz.cmd_logs(gz.Args(name="g", lines=0))
    out = capsys.readouterr().out
    assert "line 1" not in out and "gamezip" not in out and "d3dx9" in out
    assert len(gz.read_log_tail(log, 1000)) <= 1000


def test_big_logs_are_trimmed(tmp_path):
    log = tmp_path / "g.log"
    log.write_bytes(b"x" * (9 * 1024 * 1024) + b"\nthe end\n")
    gz.trim_log(log)
    assert log.stat().st_size <= gz.LOG_KEEP_BYTES + 200
    assert log.read_text().endswith("the end\n")


def test_hints_avoid_false_alarms():
    noise = "monotonic clock ok\nMonospace font loaded\nsome/file: Permission denied? no, fine\n"
    assert gz.analyze_log(noise, "g") == []
    assert gz.analyze_log("execve failed: Permission denied", "g")


def test_escaping_links_removed_everywhere(tmp_path):
    root = tmp_path / "game"
    (root / "redist").mkdir(parents=True)
    (root / "redist/evil").symlink_to("/etc/passwd")
    (root / "linkdir").symlink_to("/etc")
    (root / "ok").symlink_to(root / "redist")
    gz.remove_escaping_links(root)
    assert not (root / "redist/evil").is_symlink() and not (root / "linkdir").is_symlink()
    assert (root / "ok").is_symlink()


def test_game_output_goes_straight_to_log(home, tmp_path):
    script = tmp_path / "game.sh"
    script.write_text("#!/bin/sh\necho hello from game\necho oops >&2\nexit 3\n")
    script.chmod(0o755)
    log = tmp_path / "logs/game.log"
    marker = tmp_path / "waited"
    code, _ = gz.run_logged([str(script)], dict(os.environ), tmp_path, log, False,
                            ["sh", "-c", f"touch '{marker}'"])
    text = log.read_text()
    assert code == 3 and "hello from game" in text and "oops" in text and "# LGME" in text
    assert marker.exists()  # the "wait for Wine to finish" step ran


def test_verbose_run_also_logs(home, tmp_path, capsys):
    script = tmp_path / "game.sh"
    script.write_text("#!/bin/sh\necho visible\n")
    script.chmod(0o755)
    log = tmp_path / "g.log"
    code, _ = gz.run_logged([str(script)], dict(os.environ), tmp_path, log, True)
    assert code == 0 and "visible" in capsys.readouterr().out and "visible" in log.read_text()


# ---------------------------------------------------------------- lightweight


ROOT = Path(__file__).resolve().parents[1]


def test_everyday_commands_skip_heavy_modules(tmp_path):
    import subprocess
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import lgme; lgme.main(['list']); "
            "heavy = {'zipfile', 'tarfile', 'urllib.request', 'hashlib', 'difflib', 'http.client', 'ssl'}; "
            "print(sorted(heavy & set(sys.modules)))")
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path))
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, env=env)
    assert out.stdout.strip().endswith("[]"), out.stdout + out.stderr


def test_launcher_ignores_files_in_current_folder(tmp_path):
    import subprocess
    (tmp_path / "json.py").write_text("raise SystemExit('hijacked')\n")
    (tmp_path / "lgme.py").write_text("raise SystemExit('hijacked')\n")
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "c"), XDG_DATA_HOME=str(tmp_path / "d"),
               PYTHONPATH=str(tmp_path))
    out = subprocess.run([str(ROOT / "lgme"), "--version"], cwd=tmp_path, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "LGME-CLI" in out.stdout, out.stderr


def test_installer_end_to_end(tmp_path, server_root=None):
    import subprocess
    import shutil as _sh
    if not _sh.which("curl") and not _sh.which("wget"):
        pytest.skip("needs curl or wget")
    serve = tmp_path / "srv"
    serve.mkdir()
    _sh.copy(ROOT / "lgme.py", serve / "lgme")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(serve), **k)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        home = tmp_path / "home with space"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local/share"),
                   XDG_CONFIG_HOME=str(home / ".config"),
                   LGME_DOWNLOAD_URL=f"http://127.0.0.1:{httpd.server_port}/lgme")
        for _ in range(2):  # installing twice (= reinstall/upgrade) must work too
            out = subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
            assert out.returncode == 0, out.stdout + out.stderr
        stub = home / ".local/bin/lgme"
        app = home / ".local/share/lgme/app"
        assert (app / "lgme.py").is_file() and "LGME" in stub.read_text()
        out = subprocess.run([str(stub), "--version"], capture_output=True, text=True, env=env, cwd=tmp_path)
        assert out.returncode == 0 and "LGME-CLI" in out.stdout, out.stderr
        assert list((app / "__pycache__").glob("lgme.*.pyc")), "compiled code should be cached"
        # never replaces a different program called lgme
        stub.write_text("#!/bin/sh\necho something else\n")
        out = subprocess.run(["sh", str(ROOT / "install.sh")], capture_output=True, text=True, env=env)
        assert out.returncode != 0 and stub.read_text().endswith("something else\n")
    finally:
        httpd.shutdown()


def test_playing_skips_the_argument_parser(tmp_path):
    import subprocess
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import lgme; "
            "lgme.write_registry({'g': {'kind': 'native', 'exe': '/nonexistent', 'root': '/'}}); "
            "lgme.main(['g']); print('argparse' in sys.modules)")
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path))
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, env=env)
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr
    assert "program file is missing" in out.stderr  # still a friendly error


def test_huge_log_keeps_startup_errors_and_end(tmp_path):
    log = tmp_path / "g.log"
    with log.open("w") as fh:
        fh.write("err:module:import_dll Library MSVCP140.dll not found\n")
        for i in range(400000):
            fh.write(f"noise line {i} ............................\n")
        fh.write("the very end\n")
    gz.trim_log(log)
    text = log.read_text()
    assert log.stat().st_size < gz.LOG_KEEP_BYTES + gz.LOG_HEAD_BYTES + 1000
    assert "MSVCP140" in text and text.endswith("the very end\n")
    sample = gz.read_log_sample(log)
    assert any("vcrun2022" in h for h in gz.analyze_log(sample, "g"))


def test_duration_wording():
    assert gz.duration(1) == "1 second" and gz.duration(42) == "42 seconds"
    assert gz.duration(600) == "10 min" and gz.duration(3 * 3600 + 120) == "3 h 2 min"


def _fake_game(home, tmp_path, body):
    script = tmp_path / "g.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    gz.write_registry({"g": {"kind": "native", "exe": str(script), "root": str(tmp_path)}})


def test_quick_clean_exit_is_not_an_alarm(home, tmp_path, capsys):
    _fake_game(home, tmp_path, "exit 0\n")
    gz.cmd_run(gz.Args(name="g", proton=None, wine=False, verbose=False, game_args=[]))
    out = capsys.readouterr().out
    assert "closed after" in out and "stopped" not in out


def test_crash_shows_hint(home, tmp_path, capsys):
    _fake_game(home, tmp_path, "echo 'err:module:import_dll Library d3dx9_43.dll not found'\nexit 5\n")
    gz.cmd_run(gz.Args(name="g", proton=None, wine=False, verbose=False, game_args=[]))
    out = capsys.readouterr().out
    assert "exit code 5" in out and "lgme fix g d3dx9" in out
    assert gz.read_registry()["g"]["last_played"]


def test_setup_without_terminal_installs_nothing(home, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(gz.subprocess, "run", lambda *a, **k: ran.append(a) or gz.subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(gz, "install_ge_proton", lambda: ran.append("proton"))
    monkeypatch.setattr(gz, "link_command", lambda: ran.append("link"))
    gz.cmd_setup(gz.Args(update_proton=False))
    assert not any(r == "proton" or r == "link" or (r and r[0] and r[0][0] == "sudo") for r in ran)
    assert "in a terminal" in capsys.readouterr().out


def test_archive_with_only_an_installer_runs_it(home, tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "Cool Game Setup.zip", {"setup_cool_game.exe": b"MZ", "data-1.bin": b"1"})
    calls = []

    def fake_installer(installer, name):
        calls.append(installer.name)
        exe = home / "prefix/drive_c/Games/Cool/Cool.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        return exe, home / "prefix", False

    monkeypatch.setattr(gz, "run_installer", fake_installer)
    gz.cmd_add(args(source=str(archive)))
    game = gz.read_registry()["cool-game-setup"]
    assert calls == ["setup_cool_game.exe"]
    assert game["exe"].endswith("Cool.exe") and game["managed_files"] and game["kind"] == "windows"
