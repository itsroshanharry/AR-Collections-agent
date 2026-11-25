"""Significance testing for A/B results, checked against textbook values."""

from __future__ import annotations

import pytest

from ab_dashboard.db import ExperimentRow, ExperimentStatus, PromptVersionRow
from shared.eval.stats import compare, paired_bootstrap_diff, sign_test_p, wilson_interval


class TestWilson:
    def test_known_value(self) -> None:
        lo, hi = wilson_interval(5, 10)
        assert lo == pytest.approx(0.2366, abs=1e-3)
        assert hi == pytest.approx(0.7634, abs=1e-3)

    def test_stays_inside_unit_interval_at_the_extremes(self) -> None:
        # Where the normal approximation would give [1, 1] and claim certainty.
        lo, hi = wilson_interval(10, 10)
        assert 0.0 < lo < 1.0 and hi == 1.0
        assert wilson_interval(0, 10)[0] == 0.0

    def test_no_data(self) -> None:
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_rejects_impossible_counts(self) -> None:
        with pytest.raises(ValueError):
            wilson_interval(11, 10)


class TestSignTest:
    def test_close_split_is_not_significant(self) -> None:
        assert sign_test_p(10, 8) == pytest.approx(0.8145, abs=1e-3)

    def test_lopsided_split_is(self) -> None:
        # P(X <= 1 | n=10, p=0.5) = 11/1024, doubled.
        assert sign_test_p(9, 1) == pytest.approx(22 / 1024)

    def test_symmetric(self) -> None:
        assert sign_test_p(2, 7) == sign_test_p(7, 2)

    def test_no_decisive_comparisons(self) -> None:
        assert sign_test_p(0, 0) == 1.0

    def test_even_split_is_capped_at_one(self) -> None:
        assert sign_test_p(5, 5) == 1.0


class TestPairedBootstrap:
    def test_constant_shift_has_a_tight_interval(self) -> None:
        a = [4.0, 3.0, 5.0, 4.5]
        diff, (lo, hi) = paired_bootstrap_diff(a, [x - 0.5 for x in a])
        assert diff == pytest.approx(0.5)
        assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)

    def test_is_reproducible(self) -> None:
        a, b = [4.0, 2.0, 5.0, 3.0, 4.0], [3.0, 3.0, 4.0, 4.0, 2.0]
        assert paired_bootstrap_diff(a, b, seed=7) == paired_bootstrap_diff(a, b, seed=7)

    def test_noisy_difference_interval_spans_zero(self) -> None:
        a, b = [5.0, 1.0, 5.0, 1.0], [1.0, 5.0, 1.0, 5.0]
        _, (lo, hi) = paired_bootstrap_diff(a, b)
        assert lo < 0 < hi

    def test_rejects_unpaired_samples(self) -> None:
        with pytest.raises(ValueError):
            paired_bootstrap_diff([1.0], [1.0, 2.0])


class TestCompare:
    def test_ten_eight_is_called_noise(self) -> None:
        sig = compare(10, 8, 0, [4.0] * 18, [4.0] * 18)
        assert not sig.significant
        assert "Not significant" in sig.interpretation

    def test_clear_win_is_called_significant(self) -> None:
        sig = compare(15, 1, 2, [4.5] * 18, [3.0] * 18)
        assert sig.significant
        assert sig.composite_diff == pytest.approx(1.5)
        assert "excludes zero" in sig.interpretation

    def test_all_ties(self) -> None:
        sig = compare(0, 0, 5, [4.0] * 5, [4.0] * 5)
        assert (sig.decisive, sig.p_value, sig.significant) == (0, 1.0, False)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'stats.db'}")
    import ab_dashboard.db as db_module
    from shared.config import get_settings

    get_settings.cache_clear()
    db_module._engine = None
    db_module._sessionmaker = None
    await db_module.init_db()
    yield db_module
    await db_module.dispose_engine()
    get_settings.cache_clear()


async def test_results_report_significance(db) -> None:
    from ab_dashboard import jobs
    from ab_dashboard.routers.experiments import experiment_results
    from shared.eval.eval_set import load_eval_set
    from tests.test_migrations_and_jobs import _comparison

    async with db.get_sessionmaker()() as session:
        for pid in ("p_a", "p_b"):
            session.add(
                PromptVersionRow(id=pid, name=pid, version=1, tone="firm", template="x")
            )
        exp = ExperimentRow(
            variant_a_id="p_a", variant_b_id="p_b", status=ExperimentStatus.COMPLETED
        )
        session.add(exp)
        await session.commit()
        eid = exp.id

    # A wins 4 of 6: a lead on paper, nowhere near significant.
    for i, scenario in enumerate(load_eval_set()[:6]):
        comparison = _comparison(scenario)
        comparison.pairwise_winner = "A" if i < 4 else "B"
        await jobs._persist_comparison(eid, comparison)

    async with db.get_sessionmaker()() as session:
        results = await experiment_results(eid, session)

    assert (results.variant_a.wins, results.variant_b.wins) == (4, 2)
    assert not results.significance.significant
    assert results.significance.p_value == pytest.approx(0.6875)
    assert "not significant" in results.verdict
    # Every scenario's tags are carried through to a per-capability breakdown.
    first_tag = load_eval_set()[0].tags[0]
    assert results.by_tag[first_tag].scenarios >= 1
