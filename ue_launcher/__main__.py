"""Entry point: python -m ue_launcher"""

from .ui.app import run


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
