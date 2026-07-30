"""Tests for the shared data types.

The :class:`~milhouse.models.Graph` helpers are pure, so everything they decide
is settled here, against graphs written out by hand and without a subprocess.
"""

from __future__ import annotations

from milhouse.models import Graph, Issue, Iteration, MergeRecord


def graph(statuses: dict[str, str], *edges: tuple[str, str], epics: tuple[str, ...] = ()) -> Graph:
    """A graph from ``{id: status}`` and ``(blocker, blocked)`` pairs."""
    return Graph(
        nodes={
            issue_id: Issue(
                id=issue_id,
                title=f"Do {issue_id}",
                status=status,
                issue_type="epic" if issue_id in epics else "task",
            )
            for issue_id, status in statuses.items()
        },
        edges=list(edges),
    )


def open_graph(ids: str, *edges: tuple[str, str]) -> Graph:
    """The same, with every named issue open."""
    return graph(dict.fromkeys(ids.split(), "open"), *edges)


def test_issue_reports_closed_status() -> None:
    assert Issue(id="bd-1", title="t", status="closed").is_closed
    assert not Issue(id="bd-1", title="t", status="in_progress").is_closed


def test_iteration_detects_a_commit() -> None:
    committed = Iteration(number=1, issue_id="bd-1", outcome="partial", commits=["abc1234"])
    still = Iteration(number=1, issue_id="bd-1", outcome="stalled", head_before="a", head_after="a")

    assert committed.made_commit
    assert not still.made_commit


# -- what became of a worker lane ----------------------------------------------


def merge(**fields: object) -> MergeRecord:
    """A merge of one run's worker lane into its integration branch."""
    return MergeRecord(source="milhouse/bd-e--bd-e.1", target="milhouse/bd-e", **fields)  # ty: ignore[invalid-argument-type]


def test_only_a_real_merge_commit_joined_two_histories() -> None:
    """The signal ADR 0024 keeps by not forcing `--no-ff`, and what re-verifies."""
    assert merge(sha="c" * 40).joined
    assert not merge(sha="c" * 40, fast_forwarded=True).joined
    assert not merge().joined


def test_a_branch_that_was_already_contained_still_landed() -> None:
    """Nothing moved because there was nothing to move, which is not a failure."""
    assert merge().landed
    assert merge(sha="c" * 40).landed
    assert merge(sha="c" * 40, fast_forwarded=True).landed


def test_a_conflicted_or_refused_merge_did_not_land() -> None:
    """A closed issue, a live branch, and an integration branch without its work."""
    conflicted = merge(conflicts=["src/a.py"])
    refused = merge(error="could not merge: the index is locked")

    assert not conflicted.landed
    assert not refused.landed
    # Both branches are on the record, because the recovery is by hand.
    assert (conflicted.source, conflicted.target) == ("milhouse/bd-e--bd-e.1", "milhouse/bd-e")


def test_a_merge_nobody_attempted_did_not_land_either() -> None:
    """Same consequence as a conflict: a closed issue on a branch only a person can land."""
    skipped = merge(skipped="milhouse/bd-e--bd-e.1 did not land in milhouse/bd-e")

    assert not skipped.landed
    # And nothing was combined, so there is no integration branch to re-verify.
    assert not skipped.joined


# -- the dependency graph ------------------------------------------------------


def test_waves_level_a_diamond() -> None:
    """One issue, two independent issues behind it, and a join: three levels."""
    diamond = open_graph("a b c d", ("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"))

    assert diamond.waves() == [["a"], ["b", "c"], ["d"]]


def test_width_is_the_widest_wave() -> None:
    """What a `--count` above it would buy, which is nothing."""
    fan = open_graph("a b c d e", ("a", "b"), ("a", "c"), ("a", "d"), ("b", "e"))

    assert fan.waves() == [["a"], ["b", "c", "d"], ["e"]]
    assert fan.width == 3
    assert open_graph("a b c", ("a", "b"), ("b", "c")).width == 1
    assert Graph().width == 0


def test_a_cycle_terminates_instead_of_hanging() -> None:
    """`bd` should not permit one, and a walk that hangs is a poor way to learn it did."""
    ring = open_graph("a b c", ("a", "b"), ("b", "c"), ("c", "a"))

    assert ring.waves() == [["a", "b", "c"]]
    assert sorted(ring.blocked_behind("a")) == ["b", "c"]
    assert ring.frontier() == []


def test_a_cycle_costs_only_the_levels_it_is_part_of() -> None:
    """What can still be levelled is, and the tangle is one final wave."""
    ring = open_graph("a b c d", ("a", "b"), ("b", "a"), ("b", "c"))

    assert ring.waves() == [["d"], ["a", "b", "c"]]


def test_blocked_behind_is_transitive() -> None:
    chain = open_graph("a b c d", ("a", "b"), ("b", "c"), ("a", "d"))

    assert chain.blocked_behind("a") == ["b", "d", "c"]
    assert chain.blocked_behind("b") == ["c"]
    assert chain.blocked_behind("c") == []


def test_nothing_waits_on_a_closed_or_unknown_issue() -> None:
    done = graph({"a": "closed", "b": "open"}, ("a", "b"))

    assert done.blocked_behind("a") == []
    assert done.blocked_behind("bd-elsewhere") == []


def test_frontier_skips_an_issue_with_an_open_blocker() -> None:
    waiting = open_graph("a b", ("a", "b"))

    assert [issue.id for issue in waiting.frontier()] == ["a"]


def test_closing_the_blocker_puts_its_dependent_on_the_frontier() -> None:
    landed = graph({"a": "closed", "b": "open"}, ("a", "b"))

    assert [issue.id for issue in landed.frontier()] == ["b"]
    assert landed.waves() == [["b"]]


def test_the_frontier_is_only_what_could_be_claimed_now() -> None:
    """In progress and deferred are unfinished work, but not work to hand out."""
    started = graph({"a": "in_progress", "b": "deferred", "c": "open"})

    assert [issue.id for issue in started.frontier()] == ["c"]
    assert started.waves() == [["a", "b", "c"]]


def test_an_epic_is_never_in_a_wave() -> None:
    """An epic is a container for the work, not a unit of it."""
    under = graph({"e": "open", "a": "open"}, epics=("e",))

    assert [issue.id for issue in under.frontier()] == ["a"]
    assert under.waves() == [["a"]]
