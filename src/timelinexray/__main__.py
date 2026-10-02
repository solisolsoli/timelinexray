"""Allow ``python -m timelinexray`` as an alias of the ``txray`` console script."""

from .cli import main

raise SystemExit(main())
