"""Pytest configuration for the efibootdude project.

Adds the repository root to ``sys.path`` so that ``import efibootdude`` works
regardless of how pytest was invoked, without needing to install the project
first (e.g. ``pytest`` straight from a source checkout).
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
