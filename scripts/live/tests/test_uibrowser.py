"""uibrowser helpers against a fake Playwright page (no browser)."""
from livetest import uibrowser

CLOSE = "[data-test-id=insights-close-btn]:visible"
TIPS = "[role=tooltip]:visible"


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def count(self):
        return self.page.card if self.selector == CLOSE else self.page.tooltips

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self.page.calls.append(("close", timeout))
        self.page.card, self.page.tooltips = 0, 0


class FakePage:
    def __init__(self, tooltips=0, card=0, closes_on=None, card_after=None):
        self.tooltips, self.card, self.closes_on, self.calls = tooltips, card, closes_on, []
        self.card_after = card_after   # the card renders after this many waits
        page = self

        class Mouse:
            def move(self, x, y):
                page.calls.append(("move", x, y))
                if page.closes_on == "move":
                    page.tooltips = 0

        class Keyboard:
            def press(self, key):
                page.calls.append(("press", key))
                if page.closes_on == "escape":
                    page.tooltips = 0

        self.mouse, self.keyboard = Mouse(), Keyboard()

    def locator(self, selector):
        assert selector in (CLOSE, TIPS)
        return FakeLocator(self, selector)

    def wait_for_timeout(self, ms):
        self.calls.append(("wait", ms))
        if self.card_after is not None:
            self.card_after -= 1
            if self.card_after == 0:
                self.card = 1


def test_nothing_open_only_moves_the_pointer():
    page = FakePage()
    uibrowser._clear_popups(page)
    assert page.calls == [("move", 0, 0)]


def test_the_sherlock_card_is_closed_with_its_close_button():
    page = FakePage(card=1, tooltips=1)
    uibrowser._clear_popups(page)
    assert page.calls == [("move", 0, 0), ("close", 5000)]


def test_a_hover_tooltip_closes_when_the_pointer_leaves():
    page = FakePage(tooltips=1, closes_on="move")
    uibrowser._clear_popups(page)
    assert ("press", "Escape") not in page.calls


def test_a_tooltip_that_stays_is_closed_with_escape():
    page = FakePage(tooltips=1, closes_on="escape")
    uibrowser._clear_popups(page)
    assert page.calls[:2] == [("move", 0, 0), ("press", "Escape")]


def test_a_tooltip_that_never_closes_is_left_to_the_click_to_report():
    page = FakePage(tooltips=1)
    uibrowser._clear_popups(page)
    assert page.calls.count(("press", "Escape")) == 1
    assert page.calls.count(("wait", 250)) == 20


def test_the_card_alone_is_closed():
    page = FakePage(card=1)
    uibrowser._clear_popups(page)
    assert page.calls == [("move", 0, 0), ("close", 5000)]


def test_a_card_that_renders_late_is_waited_for_once_per_page():
    page = FakePage(card_after=3)
    uibrowser._close_card(page, wait_ms=3000)
    assert page.calls == [("wait", 250)] * 3 + [("close", 5000)]


def test_no_card_costs_at_most_the_wait():
    page = FakePage()
    uibrowser._close_card(page, wait_ms=3000)
    assert page.calls == [("wait", 250)] * 12
