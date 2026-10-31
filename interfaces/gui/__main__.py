"""Command-line entry point for the GUI server."""

from interfaces import application_host


def main(argv: list[str] | None = None) -> None:
    application_host.main(argv, prog="nosis-gui")


if __name__ == "__main__":
    main()
