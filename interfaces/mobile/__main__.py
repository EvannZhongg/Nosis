"""Launch the application host for its configured messaging frontends."""

from interfaces import application_host


def main(argv: list[str] | None = None) -> None:
    application_host.main(argv, prog="nosis-mobile")


if __name__ == "__main__":
    main()
