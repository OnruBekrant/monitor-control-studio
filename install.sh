#!/bin/sh
# Kullanıcı düzeyinde kurulum (root gerekmez). Kaldırmak için: ./install.sh --uninstall
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/monitor-ddc"
UNIT="$HOME/.config/systemd/user/monitor-ddc.service"
DESK="$HOME/.local/share/applications/io.github.MonitorDDC.desktop"

if [ "$1" = "--uninstall" ]; then
    systemctl --user disable --now monitor-ddc.service 2>/dev/null || true
    rm -f "$BIN" "$UNIT" "$DESK"
    systemctl --user daemon-reload
    echo "Kaldırıldı (profiller ~/.config/monitor-ddc içinde duruyor)."
    exit 0
fi

command -v ddcutil >/dev/null || { echo "ddcutil gerekli: sudo pacman -S ddcutil"; exit 1; }
/usr/bin/python3 -c "import PySide6.QtDBus" 2>/dev/null || {
    echo "PySide6 gerekli / PySide6 is required:"
    echo "  Arch/CachyOS: sudo pacman -S pyside6"
    echo "  Fedora:       sudo dnf install python3-pyside6"
    echo "  Debian/Ubuntu (24.10+): sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtdbus"
    exit 1
}

# Yol sed'e yerleştirilecek: '|', '&', '\' ve satır sonu içeren yollar komut enjeksiyonuna
# ya da bozuk unit dosyasına yol açabilir → reddet.
# shellcheck disable=SC1003  # tek tırnak içindeki ters bölü bilinçli: düz karakter olarak eşleşir
case "$HOME$HERE" in
    *'|'*|*'&'*|*'\'*|*'
'*) echo "Desteklenmeyen karakter içeren yol: $HOME / $HERE"; exit 1 ;;
esac
# Kodun bulunduğu dizin başkaları tarafından yazılabilirse servis onların kodunu çalıştırır
if [ -n "$(find "$HERE" -maxdepth 2 -perm -o+w ! -type l 2>/dev/null | head -1)" ]; then
    echo "UYARI: $HERE altında herkesin yazabildiği dosyalar var; düzeltin: chmod -R o-w \"$HERE\""
    exit 1
fi

umask 077
mkdir -p "$(dirname "$BIN")" "$(dirname "$UNIT")" "$(dirname "$DESK")" \
         "$HOME/.config/monitor-ddc" "$HOME/.cache/monitor-ddc"
ln -sfn "$HERE/bin/monitor-ddc" "$BIN"
sed "s|@BIN@|$BIN|" "$HERE/packaging/monitor-ddc.service" > "$UNIT"
sed "s|@BIN@|$BIN|" "$HERE/packaging/io.github.MonitorDDC.desktop" > "$DESK"
chmod 644 "$DESK"
systemd-analyze --user verify "$UNIT"
update-desktop-database "$(dirname "$DESK")" 2>/dev/null || true

# Elle başlatılmış bir örnek varsa kapat; servis devralsın
"$BIN" --quit 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now monitor-ddc.service
echo "Kuruldu. Servis durumu:"
systemctl --user --no-pager status monitor-ddc.service | head -5

# I2C erişim kontrolü
BUS=$(ddcutil detect --terse 2>/dev/null | sed -n 's|.*/dev/i2c-\([0-9]*\).*|\1|p' | head -1)
if [ -n "$BUS" ] && [ ! -w "/dev/i2c-$BUS" ]; then
    echo "UYARI: /dev/i2c-$BUS yazılabilir değil. Kalıcı çözüm: sudo usermod -aG i2c $USER (sonra oturumu yeniden aç)"
fi
