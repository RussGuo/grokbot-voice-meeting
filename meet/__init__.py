"""One-command voice meetings."""

__all__ = ["main"]


def main(argv=None) -> int:
    from meet.cli import main as cli_main
    return cli_main(argv)
