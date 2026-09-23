"""WSL Paddle default with an explicitly enabled Windows fallback."""

from __future__ import annotations

import os


def main() -> None:
    if os.name == "nt":
        raise SystemExit(
            "Windows native inference is an opt-in fallback; "
            "use ocr-pdf-rebuilder-windows explicitly."
        )
    from .paddle_textonly_pdf import main as run
    run()


def windows_native_main() -> None:
    if os.name != "nt":
        raise SystemExit("The Windows native fallback must run on Windows.")
    from .paddle_textonly_pdf import main as run
    run()
