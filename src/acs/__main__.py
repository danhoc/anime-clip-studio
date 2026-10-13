import sys


def main() -> int:
    from acs.app import main as app_main
    return app_main()


if __name__ == "__main__":
    sys.exit(main())
