"""Minimal distutils.dir_util compatibility for queenbee_local on Python 3.13."""

from setuptools._distutils.dir_util import copy_tree  # pragma: no cover

__all__ = ['copy_tree']
