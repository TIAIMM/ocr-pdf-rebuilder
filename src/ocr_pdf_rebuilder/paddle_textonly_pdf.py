"""Stable CLI entry point for the PaddleOCR-VL-first production pipeline."""

from . import paddle_pipeline
from .signal_cleanup import termination_raises_keyboard_interrupt


def main() -> None:
    try:
        with termination_raises_keyboard_interrupt():
            paddle_pipeline.main()
    except KeyboardInterrupt:
        paddle_pipeline.log("PaddleOCR task interrupted; child process cleanup completed")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
