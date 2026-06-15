"""Entry point so the workflow and worker can call ``python -m fleet``."""

from fleet.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
