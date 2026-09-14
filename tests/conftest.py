"""No autouse authentication overrides.

Fixtures are explicit unittest setup/cleanup helpers in auth_test_support.py,
so pytest and `python -m unittest discover -s tests` exercise the same auth.
"""
