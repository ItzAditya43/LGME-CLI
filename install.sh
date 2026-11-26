#!/bin/sh
# LGME installer - puts the `lgme` command in ~/.local/bin.
#   curl -fsSL https://github.com/ItzAditya43/LGME-CLI/releases/latest/download/install.sh | sh
# Uninstall: rm ~/.local/bin/lgme
set -eu

REPO="ItzAditya43/LGME-CLI"
URL="https://github.com/$REPO/releases/latest/download/lgme"
BIN="${LGME_BIN_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
fail() { printf 'Problem: %s\n' "$1" >&2; [ $# -gt 1 ] && printf '→ %s\n' "$2" >&2; exit 1; }

say "Installing LGME (Linux Gaming Made Easy)…"

command -v python3 >/dev/null 2>&1 || fail "LGME needs Python 3, which isn't installed." \
  "Install it, then run this again. Arch: sudo pacman -S python · Ubuntu/Debian: sudo apt install python3 · Fedora: sudo dnf install python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "LGME needs Python 3.9 or newer." \
  "Update Python with your package manager."

mkdir -p "$BIN"
tmp=$(mktemp "$BIN/.lgme-install.XXXXXX")
trap 'rm -f "$tmp"' EXIT INT TERM

if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$tmp" || fail "Download failed." "Check your internet connection and try again."
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$tmp" "$URL" || fail "Download failed." "Check your internet connection and try again."
else
  fail "Neither curl nor wget is installed." "Install curl with your package manager, then run this again."
fi

head -n 1 "$tmp" | grep -q python3 || fail "The download doesn't look like LGME." "Try again in a minute."
python3 -c 'import sys; compile(open(sys.argv[1]).read(), "lgme", "exec")' "$tmp" \
  || fail "The download is damaged." "Try again in a minute."

if [ -e "$BIN/lgme" ] && ! grep -q "LGME" "$BIN/lgme" 2>/dev/null; then
  fail "$BIN/lgme already exists and isn't LGME, so it was left alone." \
    "Move it away, or install elsewhere: LGME_BIN_DIR=/some/dir sh install.sh"
fi

chmod 755 "$tmp"
mv -f "$tmp" "$BIN/lgme"
trap - EXIT INT TERM

say "✓ Installed $("$BIN/lgme" --version) to $BIN/lgme"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    say ""
    say "! $BIN isn't in your PATH yet. Do this once, then open a new terminal:"
    case "$(basename "${SHELL:-sh}")" in
      fish) say "    fish_add_path $BIN" ;;
      zsh) say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc" ;;
      *) say "    echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;;
    esac
    ;;
esac

say ""
say "Next step, to get your computer ready for games:"
say "    lgme setup"
say "Then just type  lgme  for the menu."
