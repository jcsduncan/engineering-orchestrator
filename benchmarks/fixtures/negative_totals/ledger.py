"""Small fixture for a signed transaction total."""


def total(amounts: list[int]) -> int:
    return sum(max(0, amount) for amount in amounts)
