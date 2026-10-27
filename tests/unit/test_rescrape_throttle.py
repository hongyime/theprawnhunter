"""Rescrape backs off as the delivery backlog grows."""

from __future__ import annotations

import pytest

from app.workers.tasks.flows.scrape import _rescale_for_backlog


def test_normal_backlog_keeps_batch(monkeypatch):
    monkeypatch.delenv("RESCRAPE_BACKLOG_HIGH", raising=False)
    monkeypatch.delenv("RESCRAPE_BACKLOG_PAUSE", raising=False)
    assert _rescale_for_backlog(1_000, 50, 300) == (50, 300, False)


def test_high_water_shrinks_batch_and_doubles_spread(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_HIGH", "25000")
    monkeypatch.setenv("RESCRAPE_BACKLOG_PAUSE", "40000")
    assert _rescale_for_backlog(30_000, 50, 300) == (10, 600, False)


def test_pause_threshold_skips_cycle(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_PAUSE", "40000")
    assert _rescale_for_backlog(41_000, 50, 300) == (0, 300, True)


def test_floor_of_five_credentials(monkeypatch):
    monkeypatch.setenv("RESCRAPE_BACKLOG_HIGH", "1")
    monkeypatch.setenv("RESCRAPE_BACKLOG_PAUSE", "100000")
    batch, _, skip = _rescale_for_backlog(5_000, 10, 300)
    assert batch == 5 and skip is False
