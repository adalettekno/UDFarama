import argparse

from udfarama import __version__


def main():
    parser = argparse.ArgumentParser(description="UDFarama masaüstü arayüzü")
    parser.add_argument("--version", action="version", version=f"UDFarama {__version__}")
    parser.parse_args()
    from udfarama.app import run_app
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
