#!/usr/bin/env python3
"""Resolve shared CI test worker limits and clamp explicit pytest xdist counts."""

import argparse
import shlex
import sys


def effective_workers(cap: str, requested: str) -> int:
    maximum = int(cap)
    if maximum < 1:
        raise ValueError("worker cap must be at least 1")
    try:
        value = int(requested.strip())
    except (AttributeError, ValueError):
        return maximum
    if value < 1:
        return maximum
    return min(maximum, value)


def clamp_pytest_args(args: str, workers: int) -> list[str]:
    tokens = shlex.split(args)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        number = None
        value_index = None
        if token in ("-n", "--numprocesses") and index + 1 < len(tokens):
            value_index = index + 1
            number = tokens[value_index]
        elif token.startswith("--numprocesses="):
            value_index = index
            number = token.partition("=")[2]
        elif token.startswith("-n") and token != "-n":
            value_index = index
            number = token[2:]

        if value_index is not None:
            try:
                requested = int(number)
            except (TypeError, ValueError):
                index += 1
                continue
            if requested > workers:
                if tokens[value_index].startswith("--numprocesses=") or tokens[value_index].startswith("-n") and value_index == index:
                    tokens[value_index] = f"{tokens[value_index].split('=', 1)[0]}={workers}" if tokens[value_index].startswith("--") else f"-n{workers}"
                else:
                    tokens[value_index] = str(workers)
        index += 1
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("resolve", "pytest-args"))
    parser.add_argument("cap")
    parser.add_argument("requested")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    values = parser.parse_args()
    workers = effective_workers(values.cap, values.requested)
    if values.mode == "resolve":
        print(workers)
    else:
        args = " ".join(values.pytest_args)
        sys.stdout.buffer.write("\0".join(clamp_pytest_args(args, workers)).encode() + b"\0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
