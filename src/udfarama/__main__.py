import argparse

from udfarama import __version__
from udfarama.logging_setup import configure_logging


def main(argv=None):
    configure_logging()
    parser = argparse.ArgumentParser(description="UDFarama masaüstü arayüzü")
    parser.add_argument("--version", action="version", version=f"UDFarama {__version__}")
    parser.add_argument(
        "--background",
        action="store_true",
        help="Pencereyi açmadan sistem tepsisinde başlat",
    )
    args = parser.parse_args(argv)
    from udfarama.app import run_app
    return run_app(background=args.background)


if __name__ == "__main__":
    raise SystemExit(main())
