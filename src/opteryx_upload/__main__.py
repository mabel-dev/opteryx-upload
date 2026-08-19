"""`python -m opteryx_upload` - the same entry point as the `opteryx-upload` script."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
