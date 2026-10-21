"""The plugin is local_mcpconnector; the previous plugin name must be gone."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_NAME = "mcp" + "bridge"  # split so this file does not match itself


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def test_no_old_plugin_name_remains():
    offenders = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if OLD_NAME in line.lower():
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "old plugin name found:\n" + "\n".join(offenders)


def test_plugin_calls_use_new_prefix():
    from moodle_mcp_connector import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert '"local_mcpconnector_create_page"' in source
    assert server.PLUGIN_PREFIX == "local_mcpconnector_"
