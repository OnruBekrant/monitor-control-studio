"""Uygulamayı sahte ddcutil + sahte sysfs ile başlatır (D-Bus dinamik testleri için).
Kullanım: run_app.py <drm_root> [app argümanları...]"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from monitor_ddc import ddc  # noqa: E402

ddc.DDCUTIL = str(ROOT / "tests" / "fakes" / "ddcutil")
ddc.DRM_ROOT = sys.argv[1]
from monitor_ddc import app  # noqa: E402

sys.exit(app.main(sys.argv[2:]))
