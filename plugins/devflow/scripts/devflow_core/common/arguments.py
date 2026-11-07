"""Argument errors are values reported by the runner, never argparse exits."""
import argparse

from .errors import UsageError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f"{self.prog}: {message}")
