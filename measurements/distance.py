"""Small, dependency-free distance measures for rollout paths.

The functions in this module deliberately know nothing about Inspect logs or the
trajectory model.  A caller can compare organization names, ``(route, stage)``
tuples, or any other hashable path token.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence


def levenshtein_distance[T](left: Sequence[T], right: Sequence[T]) -> int:
    """Return the number of single-token edits between two sequences.

    Insertions, deletions, and substitutions each cost one.  The implementation
    retains only one dynamic-programming row, so memory use is proportional to
    the shorter input rather than to the product of both input lengths.
    """

    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for left_index, left_token in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_token in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_token != right_token),
                )
            )
        previous = current
    return previous[-1]


def normalized_edit_distance[T](left: Sequence[T], right: Sequence[T]) -> float:
    """Return Levenshtein distance divided by the longer path length.

    The result is in ``[0.0, 1.0]``.  Two empty paths are considered identical
    and therefore have distance zero.
    """

    denominator = max(len(left), len(right))
    if denominator == 0:
        return 0.0
    return levenshtein_distance(left, right) / denominator


def jaccard_distance[T](left: Collection[T], right: Collection[T]) -> float:
    """Return set-based Jaccard distance for two collections.

    Repeated contacts do not affect this measure.  Two empty collections have
    distance zero because neither contains an organization missing from the
    other.
    """

    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    if not union:
        return 0.0
    return 1.0 - (len(left_set & right_set) / len(union))


def shared_prefix_length[T](left: Sequence[T], right: Sequence[T]) -> int:
    """Return the number of equal tokens at the start of both paths."""

    for index, (left_token, right_token) in enumerate(zip(left, right)):
        if left_token != right_token:
            return index
    return min(len(left), len(right))


# A concise alias that reads naturally at call sites and preserves the more
# explicit public name used in the plan.
normalized_levenshtein = normalized_edit_distance
