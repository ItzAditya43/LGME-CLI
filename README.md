# LGME-CLI — Linux Gaming Made Easy

🌐 **Website & guide:** https://itzaditya43.github.io/LGME-CLI/

**Play your games on Linux without the headache.** LGME (`lgme`) is a small command-line helper for people new to Linux gaming. It sets up what your computer needs, adds your games, and starts them. When something breaks, it explains the problem in plain language.

It works with:

- **Steam games.** LGME asks the Steam client to install and launch them.
- **Game archives you already have:** ZIP, 7z, RAR, TAR (including split `.part1.rar` / `.001` archives).
- **Direct download links** from sources you trust, pointing to one of those archives.
- **Game folders** that are already unpacked, including a Wine/Proton prefix that holds your saves.
- **Windows installers** (`setup.exe`). LGME runs the installer, then finds the installed game.
- **Linux-native games** (`.x86_64`, `.sh`, `.AppImage`).

Windows games run with **Proton** (GE-Proton preferred) or **Wine**. LGME doesn't include game stores, catalogs or downloads from anywhere except Steam and links you provide.

---

## 1. Install (one line)

Open a terminal and paste:

```sh
curl -fsSL https://github.com/ItzAditya43/LGME-CLI/releases/latest/download/install.sh | sh
```

This puts the `lgme` command in `~/.local/bin`. If that folder isn't in your PATH yet, the installer tells you the one command to fix it. LGME needs Python 3.9+, which almost every Linux distro already has.

<details>
<summary>Other ways to install</summary>

- **Download by hand:** get `lgme` from the [latest release](https://github.com/ItzAditya43/LGME-CLI/releases/latest), then:
  `chmod +x lgme && mkdir -p ~/.local/bin && mv lgme ~/.local/bin/`
- **From source:** `git clone https://github.com/ItzAditya43/LGME-CLI && cd LGME-CLI && ./lgme setup`
- **With pipx:** `pipx install git+https://github.com/ItzAditya43/LGME-CLI`

</details>

**Update:** `lgme update` · **Uninstall:** `rm ~/.local/bin/lgme && rm -r ~/.local/share/lgme/app` (your games, saves and settings stay where they are)

**Light on your PC:** LGME is one small Python file using only the standard library. Starting a game adds about 0.05 s. While you play, LGME just sleeps and uses no CPU. It writes the game's messages straight to a log that is capped at a few MB.

## 2. Get your computer ready (one time)

```sh
lgme setup
```

`setup` checks your computer and offers to fix anything missing:

| It checks | Why | How it fixes it |
|---|---|---|
| Steam | For Steam games | Installs it with your package manager (pacman, apt, dnf, zypper) or Flatpak |
| GE-Proton | Best way to run Windows games | Downloads the latest release from GitHub and verifies its checksum |
| Wine | Backup when Proton isn't available | Package manager |
| winetricks | Installs missing Windows parts (`lgme fix`) | Package manager |
| 7-Zip | Opens `.7z` and `.rar` archives | Package manager |
| Vulkan graphics | Modern games need it | Tells you which driver to install |
| Free space | Games are big | Tells you where to free space |
| `lgme` command | So you can type `lgme` from any folder | Links it into `~/.local/bin` |

LGME always asks before installing anything, and package installs ask for your password through `sudo`. To only check without changing anything, run `lgme doctor`.

## 3. The easy way: the menu

Just type:

```sh
lgme
```

A numbered menu lets you play, add, fix or remove games. To add a game, **drag the file or folder into the terminal window** and press Enter.

## 4. Adding games

One command handles everything: `lgme add <file, folder, installer or link>`.

```sh
lgme add ~/Downloads/MyGame.zip         # archive: unpacked into ~/Games/LGME/<name>
lgme add ~/Downloads/MyGame.part1.rar   # split archive: keep all parts in the same folder
lgme add ~/Games/MyGame                 # folder that's already unpacked: stays where it is
lgme add ~/Downloads/setup_mygame.exe   # Windows installer: runs it, then adds the game
lgme add "https://example.com/mygame.zip"   # direct link to an archive you trust
```

What happens:

1. LGME suggests a short name like `my-game`. You'll type it to play.
2. It looks for the program that starts the game. If there are several candidates, it lists them and recommends one. It skips things like `UnityCrashHandler`, `vcredist` and `_CommonRedist`.
3. If the folder already has a **prefix** (for example `prefix/pfx`, from an earlier Proton setup), LGME offers to reuse it, so you keep your saves and settings.
4. It offers to add the game to your **app menu** and to start it right away.

Handy options: `--name`, `--exe "path/inside/folder/Game.exe"`, `--prefix /path/to/prefix`, `--runner proton|wine`, and `-y` (accept the safe default answer to every question).

**Steam games:**

```sh
lgme steam add 413150                                          # App ID…
lgme steam add https://store.steampowered.com/app/413150/Stardew_Valley/   # …or the store link
lgme steam list                                                # what's already installed in Steam
```

Steam opens its install window, and you need to own the game on your account. For a Windows-only game, go to Steam → right-click the game → Properties → Compatibility and turn on a Proton version.

## 5. Playing

```sh
lgme list                       # your games and whether they're ready
lgme my-game                    # play! (same as: lgme run my-game)
lgme run my-game -- -windowed   # anything after -- goes to the game
lgme run my-game --verbose      # show the game's messages in the terminal
```

**Even shorter:** when you add a game, LGME offers to create a command with the game's name, so typing just `my-game` in a terminal starts it (or run `lgme command my-game`). It never replaces a command that already exists on your system.

On first launch, LGME creates a private Windows environment (a **prefix**) for the game. Each game gets its own prefix, so games can't break each other. Game messages are saved to a log file.

## 6. When a game won't start

LGME reads the log after a crash and tells you what it found. For example:

```
! The game stopped after 3 seconds (exit code 1).
→ The game is missing Windows files: msvcp140.dll. Fix: lgme fix my-game vcrun2022
```

Other tools:

```sh
lgme logs my-game                  # last messages, with tips
lgme fix my-game vcrun2022 d3dx9   # install Windows components (winetricks); no names → pick from a list
lgme set my-game --pick-exe        # LGME picked the wrong program
lgme set my-game --runner wine     # try Wine instead of Proton (or: proton, auto)
lgme set my-game --proton GE-Proton11-3   # use a specific Proton version (or: auto)
lgme set my-game --new-prefix      # fresh Windows environment; the old one is KEPT
lgme set my-game --env DXVK_HUD=fps --args "-windowed"
lgme info my-game                  # where everything is
lgme doctor                        # check your computer again
```

Also check the game on [protondb.com](https://www.protondb.com). Other players post the settings that worked for them.

**Common situations**

- *"That link opens a web page, not a file."* Many file hosts need a click in the browser. Download the file there, then run `lgme add ~/Downloads/<file>`.
- *Archive with a password.* LGME switches to 7-Zip and asks for the password.
- *Download interrupted.* Run the same command again and it continues where it stopped.
- *"made with Wine, so Proton can't use it"* (or the other way round). A prefix belongs to one tool. Use the suggested `lgme set` command.
- *Anti-cheat games* (EasyAntiCheat, BattlEye) often refuse to run on Linux unless the developer enables it.

## 7. Tidying up

```sh
lgme shortcut my-game [--remove]   # app-menu entry
lgme command my-game [--remove]    # type just "my-game" in a terminal to play
lgme rename my-game new-name
lgme remove my-game                # asks about each step
lgme settings --games-dir /mnt/big-drive/Games   # where new games get unpacked
```

**LGME never overwrites or deletes your stuff without asking:**

- Adding a game never replaces an existing game, folder or prefix. You're asked to pick another name.
- `remove` takes the game off the list. It deletes game files only if LGME unpacked them **and** you say yes (or pass `--delete-files`). It deletes the prefix, where your **saves** live, only if LGME created it **and** you say yes (or pass `--delete-saves`). `-y` alone never deletes files.
- Folders you added with `lgme add <folder>`, and prefixes you pointed it at, are never deleted.
- Your own archive files are never deleted. Archives LGME downloaded itself are removed after a successful unpack.
- Failed or cancelled unpacks are cleaned up. Archives that try to write outside the game folder are refused.

## Where things are stored

| What | Where |
|---|---|
| Unpacked games | `~/Games/LGME/` (change with `lgme settings --games-dir`, or `LGME_GAMES_DIR`) |
| Prefixes (Windows environments, saves) | `~/.local/share/lgme/prefixes/` |
| Game logs | `~/.local/share/lgme/logs/` |
| Unfinished downloads | `~/.local/share/lgme/downloads/` |
| Game list | `~/.config/lgme/games.json` (a backup is kept as `games.json.bak`) |
| App-menu shortcuts | `~/.local/share/applications/lgme-*.desktop` |
| Game-name commands | `~/.local/bin/<game>` (only ones LGME created) |

## For developers

- Single file: `lgme.py`, Python 3.9+, standard library only. Optional tools: `7z`/`bsdtar`, `winetricks`, `vulkaninfo`, `notify-send`.
- Install as a package instead: `pipx install .`
- Tests (they don't touch Steam, Wine, the network or your real games): `python3 -m pytest`
- `--debug` shows the full technical error.
