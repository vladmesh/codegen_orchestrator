"""Scaffolder service entry point.

Consumes from scaffold:queue and prepares project repositories
by running copier + make setup + git push.
"""

from src.consumer import main

if __name__ == "__main__":
    main()
