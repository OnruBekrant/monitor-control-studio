#!/bin/sh
# Tüm kalite ve güvenlik kontrolleri. Yerelde ve CI'da aynı betik çalışır.
#   scripts/check.sh            → statik + güvenlik + birim/modül/dinamik testler
#   scripts/check.sh --live     → + gerçek monitörde entegrasyon testleri (servis durdurulur)
set -eu
cd "$(dirname "$0")/.."
V=.venv/bin
S=.venv-semgrep/bin
export QT_QPA_PLATFORM=offscreen
fail=0
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
run() { if "$@"; then echo "   ✓ geçti"; else echo "   ✗ BAŞARISIZ"; fail=1; fi; }

step "Lint + güvenlik kuralları (ruff: pyflakes, bugbear, bandit-S, pylint)"
run "$V/ruff" check monitor_ddc tests
step "Tip denetimi (mypy)"
run "$V/mypy" monitor_ddc
step "SAST: bandit"
run "$V/bandit" -q -r monitor_ddc -c pyproject.toml
if [ -x "$S/semgrep" ]; then
    step "SAST: semgrep (p/python, p/bandit, p/secrets)"
    run "$S/semgrep" scan --metrics=off --quiet --error --config p/python --config p/bandit --config p/secrets \
        --exclude .venv --exclude .venv-semgrep --exclude tests .
fi
step "Ölü kod (vulture)"
run "$V/vulture"
step "Gizli anahtar taraması (detect-secrets)"
run sh -c "$V/detect-secrets scan --exclude-files '(^|/)\.venv' . 2>/dev/null | python3 -c 'import json,sys; r=json.load(sys.stdin)[\"results\"]; print(r or \"   gizli anahtar yok\"); sys.exit(1 if r else 0)'"
step "Kabuk betikleri (shellcheck)"
run "$V/shellcheck" -S style install.sh bin/monitor-ddc scripts/check.sh
step "Bağımlılık açıkları (pip-audit: uygulama + geliştirme ortamı)"
run sh -c "printf 'PySide6==%s\n' \"\$($V/python -c 'import PySide6; print(PySide6.__version__)')\" | $V/pip-audit -r /dev/stdin --progress-spinner off"
run "$V/pip-audit" --local --progress-spinner off
step "Paketleme: desktop dosyası + systemd unit + sandbox puanı"
run desktop-file-validate packaging/io.github.MonitorDDC.desktop
tmpu=$(mktemp -d)
sed "s|@BIN@|/usr/bin/true|" packaging/monitor-ddc.service > "$tmpu/monitor-ddc.service"
run systemd-analyze --user verify "$tmpu/monitor-ddc.service"
run sh -c "systemd-analyze --user security --offline=yes --threshold=25 '$tmpu/monitor-ddc.service' --no-pager | tail -1"
rm -rf "$tmpu"
step "Birim + modül + fuzz + dinamik D-Bus testleri (pytest, kapsam)"
run "$V/pytest" tests/unit -q -p no:cacheprovider --cov --cov-report=term-missing:skip-covered --cov-fail-under=80

if [ "${1:-}" = "--live" ]; then
    step "Gerçek monitör entegrasyon testleri"
    systemctl --user stop monitor-ddc 2>/dev/null || true
    run python3 tests/integration/live_profile_test.py
    run python3 tests/integration/live_lock_test.py
    systemctl --user start monitor-ddc 2>/dev/null || true
fi

echo
[ $fail -eq 0 ] && echo "TÜM KONTROLLER GEÇTİ" || echo "BAZI KONTROLLER BAŞARISIZ"
exit $fail
