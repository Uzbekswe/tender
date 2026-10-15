"""rank_bidders: the preliminary ranking of docs/customer-side-decisions.md §4.3. Pure, no database.

Every rule has its own test: verdict groups, the points step (and its skip without a scoring table),
the compliance step, ties (competition ranks 1, 1, 3), the order-not-certain flag (overlap on the
deciding step only), subject-to-rejection bidders last without a rank, and the degenerate cases.
"""

from __future__ import annotations

import pytest

from engine.scoring import REJECTION_RISK, NEEDS_REVISION, READY, BidderScore, rank_bidders


def bidder(key, verdict, cmin, cmax=None, pmin=None, pmax=None):
    return BidderScore(
        key=key,
        verdict=verdict,
        compliance_min=cmin,
        compliance_max=cmin if cmax is None else cmax,
        points_min=pmin,
        points_max=(pmin if pmax is None else pmax) if pmin is not None else None,
    )


def by_key(ranks):
    return {r.key: r for r in ranks}


def order(ranks):
    return [r.key for r in ranks]


# ---------- empty and single ----------

def test_no_bidders_gives_no_ranks():
    assert rank_bidders([]) == []


def test_one_bidder_is_rank_one_and_not_flagged():
    (only,) = rank_bidders([bidder(1, NEEDS_REVISION, 60.0, 70.0, 2, 4)])
    assert (only.key, only.rank, only.order_not_certain) == (1, 1, False)


def test_one_ready_bidder_is_rank_one():
    (only,) = rank_bidders([bidder(7, READY, 100.0)])
    assert only.rank == 1 and only.verdict == READY


# ---------- step 1: verdict groups ----------

def test_meets_all_mandatory_ranks_above_open_points_whatever_the_percentage():
    ranks = by_key(rank_bidders([
        bidder(1, NEEDS_REVISION, 95.0, 99.0, 12, 12),
        bidder(2, READY, 50.0, 55.0, 4, 4),
    ]))
    assert ranks[2].rank == 1
    assert ranks[1].rank == 2


def test_verdict_group_is_not_flagged_even_when_ranges_overlap():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 60.0, 90.0, 8, 12),
        bidder(2, NEEDS_REVISION, 61.0, 80.0, 8, 12),
    ]))
    assert ranks[1].order_not_certain is False
    assert ranks[2].order_not_certain is False


def test_subject_to_rejection_is_last_with_no_rank_and_never_flagged():
    ranks = rank_bidders([
        bidder(1, REJECTION_RISK, 99.0, 99.0, 14, 14),
        bidder(2, NEEDS_REVISION, 40.0, 50.0, 2, 4),
        bidder(3, READY, 30.0, 35.0, 1, 2),
    ])
    assert order(ranks) == [3, 2, 1]
    last = ranks[-1]
    assert last.key == 1 and last.rank is None and last.order_not_certain is False


def test_all_subject_to_rejection_means_no_rank_for_anyone():
    ranks = rank_bidders([bidder(5, REJECTION_RISK, 10.0), bidder(6, REJECTION_RISK, 20.0)])
    assert [r.rank for r in ranks] == [None, None]
    assert order(ranks) == [5, 6]  # input order is kept among the rejected


# ---------- step 2: tender points lower bound ----------

def test_higher_points_lower_bound_ranks_first_even_with_lower_compliance():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 90.0, 90.0, 4, 4),
        bidder(2, READY, 70.0, 70.0, 6, 8),
    ]))
    assert ranks[2].rank == 1
    assert ranks[1].rank == 2


def test_points_lower_bound_not_upper_bound_decides():
    # Bidder 1 could reach 10 points, bidder 2 is sure of 6. The lower bound decides: 2 first.
    ranks = by_key(rank_bidders([
        bidder(1, READY, 80.0, 80.0, 4, 10),
        bidder(2, READY, 80.0, 80.0, 6, 6),
    ]))
    assert ranks[2].rank == 1 and ranks[1].rank == 2


# ---------- step 3: compliance lower bound ----------

def test_compliance_lower_bound_decides_when_points_tie():
    ranks = by_key(rank_bidders([
        bidder(1, NEEDS_REVISION, 60.0, 95.0, 8, 8),
        bidder(2, NEEDS_REVISION, 70.0, 72.0, 8, 8),
    ]))
    assert ranks[2].rank == 1 and ranks[1].rank == 2


def test_compliance_upper_bound_does_not_decide():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 80.0, 99.0),
        bidder(2, READY, 70.0, 100.0),
    ]))
    assert ranks[1].rank == 1 and ranks[2].rank == 2


# ---------- no scoring table ----------

def test_without_points_the_points_step_is_skipped():
    # Compliance lower bounds decide alone.
    ranks = by_key(rank_bidders([
        bidder(1, READY, 85.0, 90.0),
        bidder(2, READY, 90.0, 92.0),
    ]))
    assert ranks[2].rank == 1 and ranks[1].rank == 2


def test_partial_points_skip_the_points_step_for_everyone():
    # One bidder without points means the points step is not used for anyone.
    ranks = by_key(rank_bidders([
        bidder(1, READY, 90.0, 90.0, 12, 12),
        bidder(2, READY, 80.0, 80.0),  # no points
    ]))
    assert ranks[1].rank == 1 and ranks[2].rank == 2


# ---------- step 4: ties ----------

def test_equal_on_both_steps_gives_the_same_rank_competition_style():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 80.0, 80.0, 6, 6),
        bidder(2, READY, 80.0, 80.0, 6, 6),
        bidder(3, READY, 70.0, 70.0, 6, 6),
    ]))
    assert ranks[1].rank == 1 and ranks[2].rank == 1
    assert ranks[3].rank == 3


def test_ties_are_not_flagged_even_when_the_ranges_overlap():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 80.0, 90.0, 6, 9),
        bidder(2, READY, 80.0, 95.0, 6, 9),
    ]))
    assert ranks[1].rank == 1 and ranks[2].rank == 1
    assert ranks[1].order_not_certain is False and ranks[2].order_not_certain is False


def test_ties_rank_across_groups_continue_the_count():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 90.0, 90.0, 12, 12),
        bidder(2, NEEDS_REVISION, 70.0, 70.0, 6, 6),
        bidder(3, NEEDS_REVISION, 70.0, 70.0, 6, 6),
        bidder(4, REJECTION_RISK, 99.0, 99.0, 12, 12),
    ]))
    assert ranks[1].rank == 1
    assert ranks[2].rank == 2 and ranks[3].rank == 2
    assert ranks[4].rank is None


# ---------- order not certain ----------

def test_points_overlap_on_the_deciding_step_flags_both():
    # Bidder 2 is ahead on points (6 vs 4), but bidder 1 could reach 6: the ranges touch, so it is flagged.
    ranks = by_key(rank_bidders([
        bidder(2, NEEDS_REVISION, 60.0, 60.0, 6, 6),
        bidder(1, NEEDS_REVISION, 60.0, 60.0, 4, 6),
    ]))
    assert ranks[2].order_not_certain is True
    assert ranks[1].order_not_certain is True


def test_points_no_overlap_is_not_flagged():
    ranks = by_key(rank_bidders([
        bidder(2, NEEDS_REVISION, 60.0, 60.0, 8, 8),
        bidder(1, NEEDS_REVISION, 60.0, 60.0, 4, 6),
    ]))
    assert ranks[2].order_not_certain is False
    assert ranks[1].order_not_certain is False


def test_compliance_overlap_flags_when_points_tie():
    ranks = by_key(rank_bidders([
        bidder(1, NEEDS_REVISION, 70.0, 80.0, 8, 8),
        bidder(2, NEEDS_REVISION, 60.0, 72.0, 8, 8),
    ]))
    assert ranks[1].rank == 1 and ranks[2].rank == 2
    assert ranks[1].order_not_certain is True and ranks[2].order_not_certain is True


def test_compliance_no_overlap_is_not_flagged_when_points_tie():
    ranks = by_key(rank_bidders([
        bidder(1, NEEDS_REVISION, 70.0, 80.0, 8, 8),
        bidder(2, NEEDS_REVISION, 60.0, 69.9, 8, 8),
    ]))
    assert ranks[1].order_not_certain is False and ranks[2].order_not_certain is False


def test_without_points_the_compliance_overlap_is_the_deciding_check():
    ranks = by_key(rank_bidders([
        bidder(1, READY, 80.0, 90.0),
        bidder(2, READY, 75.0, 85.0),
    ]))
    assert ranks[1].order_not_certain is True and ranks[2].order_not_certain is True


def test_overlap_is_checked_with_the_next_bidder_only():
    # Bidder 3 reaches 9 points, which overlaps bidder 1's 9, but 2 sits between them. Only the
    # neighbour pairs count: 1 and 2 do not overlap, 2 and 3 do (3 reaches 2's lower bound of 6).
    ranks = by_key(rank_bidders([
        bidder(1, NEEDS_REVISION, 90.0, 90.0, 9, 9),
        bidder(2, NEEDS_REVISION, 80.0, 80.0, 6, 6),
        bidder(3, NEEDS_REVISION, 70.0, 70.0, 5, 9),
    ]))
    assert ranks[1].order_not_certain is False
    assert ranks[2].order_not_certain is True
    assert ranks[3].order_not_certain is True


def test_bad_verdict_is_refused():
    with pytest.raises(ValueError):
        rank_bidders([bidder(1, "maybe", 50.0)])
