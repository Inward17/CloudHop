"""The files Cloudhop hands out (pipelines, run.sh, OpenTofu, setup files), read once from backend/templates."""
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / 'templates'
TEMPLATES = {path.name: path.read_text(encoding='utf-8').replace('\r\n', '\n') for path in sorted(TEMPLATE_DIR.iterdir()) if path.is_file()}
