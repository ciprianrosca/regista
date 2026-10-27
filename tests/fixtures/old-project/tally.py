"""Tally: add up the numbers given on the command line."""
import sys


def total(numbers):
    """The sum of the numbers; 0 for none."""
    return sum(numbers)


if __name__ == "__main__":
    print(total(int(n) for n in sys.argv[1:]))
