"""Rescrape slows down with the backlog, but never stops backfilling."""

from __future__ import annotations

from app.workers.tasks.flows.scrape import _rescale_for_backlog


def test_normal_backlog_keeps_batch(monkeypatch):
    monkeypatch.delenv("RESCRAPE_BACKLOG_HIGH", raising=False)
    monkeypatch.delenv("RESCRAPE_BACKLOG_SEVERE", raising=False)
    assert _rescale_for_backlog(1_000, 50, 300) == (50, 300)


def test_high_water_shrinks_batch_and_doubles_spread(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_HIGH", "25000")
    monkeypatch.setenv("RESCRAPE_BACKLOG_SEVERE", "40000")
    assert _rescale_for_backlog(30_000, 50, 300) == (10, 600)


def test_severe_backlog_crawls_but_never_skips(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_HIGH", "25000")
    monkeypatch.setenv("RESCRAPE_BACKLOG_SEVERE", "40000")
    batch, spread = _rescale_for_backlog(500_000, 50, 300)
    assert batch == 5
    assert spread == 1200


def test_floor_of_five_credentials(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_HIGH", "1")
    monkeypatch.setenv("RESCRAPE_BACKLOG_SEVERE", "100000")
    batch, _ = _rescale_for_backlog(5_000, 10, 300)
    assert batch == 5
