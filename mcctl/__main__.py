import sys

from mcctl import python_problem

if problem := python_problem():
    sys.exit(f"mcctl: {problem}")

from mcctl.cli import main

sys.exit(main())
