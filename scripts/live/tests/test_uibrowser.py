"""uibrowser helpers against a fake Playwright page (no browser)."""
import pytest

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


NAV_EXPANDED = "[data-testid=striim-navbar-expanded]:visible"
NAV_TOGGLE = "[data-testid=toggle-navbar]:visible"
NAV_ROOT = "#striim-navbar:visible"
LEFT_PANEL = "[data-test-id=left-panel]:visible"
LEFT_PANEL_CLOSE = "[data-test-id=left-panel] [data-testid=close-subcategory]:visible"
SUBMIT = "[data-test-id=submit-btn]"
RIGHT_SIDEBAR = ".right-sidebar:visible"
SHOW_ADVANCED = "text=/show advanced settings/i >> visible=true"
FORM_INPUTS = "[data-test-id^=form-control-] input:not([type=checkbox]):not([type=password]):visible"
VAL_ERROR = "[data-test-id=validation-error]:visible"
REQ_ERROR = "text=/^\\s*Value for .+ is required\\s*$/ >> visible=true"


class FakeField:
    def __init__(self, page, name, value):
        self.page, self._name, self._value = page, name, value

    def input_value(self):
        return self._value

    def get_attribute(self, attr):
        return self._name if attr == "name" else None

    def focus(self):
        self.page.calls.append(("focus", self._name))

    def press(self, key):
        self.page.calls.append(("field_press", key))
        if key == "Backspace" and self._value:
            self._value = self._value[:-1]

    def type(self, text):
        self.page.calls.append(("type", text))
        self._value += text


class CanvasLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def _node_name(self):
        prefix = "[data-test-id=node_container_"
        if self.selector.startswith(prefix) and self.selector.endswith("]"):
            return self.selector[len(prefix):-1]
        return None

    def count(self):
        p = self.page
        if self.selector == CLOSE:
            return p.card
        if self.selector == TIPS:
            return p.tooltips
        if self.selector == NAV_EXPANDED:
            return 1 if p.navbar_expanded else 0
        if self.selector == NAV_TOGGLE:
            return 1 if p.has_navbar else 0
        if self.selector == NAV_ROOT:
            return 1 if p.has_navbar else 0
        if self.selector == LEFT_PANEL:
            return 1 if p.has_left_panel else 0
        if self.selector == LEFT_PANEL_CLOSE:
            return 1 if (p.has_left_panel and p.left_panel_open and p.left_panel_closable) else 0
        if self.selector == RIGHT_SIDEBAR:
            return 1 if p.editor_open else 0
        if self.selector in (SHOW_ADVANCED, VAL_ERROR, REQ_ERROR):
            return 0
        if self.selector.startswith("button:has-text("):
            return 1 if p.editor_open else 0
        name = self._node_name()
        if name is not None:
            return 1 if name in p.nodes else 0
        return 0

    @property
    def first(self):
        return self

    def all(self):
        if self.selector == FORM_INPUTS and self.page.editor_open:
            return [self.page.field]
        return []

    def bounding_box(self, timeout=None):
        p = self.page
        if self.selector == NAV_ROOT and p.has_navbar:
            w = 240.0 if p.navbar_expanded else 64.0
            return {"x": 0.0, "y": 0.0, "width": w, "height": float(p.viewport_size["height"])}
        if self.selector == LEFT_PANEL and p.has_left_panel:
            nav_w = (240.0 if p.navbar_expanded else 64.0) if p.has_navbar else 0.0
            panel_w = 280.0 if p.left_panel_open else 72.0
            return {"x": nav_w, "y": 48.0, "width": panel_w, "height": float(p.viewport_size["height"]) - 48.0}
        name = self._node_name()
        if name is not None and name in p.nodes:
            n = p.nodes[name]
            return {
                "x": n["x"] + p.pan_x,
                "y": n["y"] + p.pan_y,
                "width": n["width"],
                "height": n["height"],
            }
        return None

    def scroll_into_view_if_needed(self, timeout=None):
        self.page.calls.append(("scroll_into_view", self.selector))
        name = self._node_name()
        if name and self.page.scrollable_container and name in self.page.nodes:
            n = self.page.nodes[name]
            self.page.pan_x = 400.0 - n["x"]
            self.page.pan_y = 300.0 - n["y"]

    def click(self, timeout=None):
        self.page.click(self.selector, timeout=timeout)


class CanvasPage:
    """Fake Playwright Page modelling #striim-navbar and a ReactFlow-style panned canvas."""

    def __init__(
        self,
        nodes,
        frames,
        navbar_expanded=False,
        has_navbar=True,
        has_left_panel=False,
        left_panel_open=False,
        left_panel_closable=True,
        viewport=(1600, 1000),
        pan_on_wheel=True,
        scrollable_container=False,
        tooltip_on_pan=False,
    ):
        self.nodes = {k: dict(v) for k, v in nodes.items()}
        self.frames = frames
        self.navbar_expanded = navbar_expanded
        self.has_navbar = has_navbar
        self.has_left_panel = has_left_panel
        self.left_panel_open = left_panel_open
        self.left_panel_closable = left_panel_closable
        self.viewport_size = {"width": viewport[0], "height": viewport[1]}
        self.pan_on_wheel = pan_on_wheel
        self.scrollable_container = scrollable_container
        self.tooltip_on_pan = tooltip_on_pan
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.card = 0
        self.tooltips = 0
        self.editor_open = False
        self.mouse_pos = (0.0, 0.0)
        self.calls = []
        self.field = FakeField(self, "ConnectionUrl", "jdbc:oracle:thin:@h:1521/db")
        page = self

        class Mouse:
            def move(self, x, y):
                page.mouse_pos = (x, y)
                page.calls.append(("move", x, y))

            def wheel(self, dx, dy):
                page.calls.append(("wheel", dx, dy))
                nav_w = (240.0 if page.navbar_expanded else 64.0) if page.has_navbar else 0.0
                left_w = nav_w + ((280.0 if page.left_panel_open else 72.0) if page.has_left_panel else 0.0)
                # Only wheel events over the open canvas (not over the navbar or left panel) pan ReactFlow.
                if page.pan_on_wheel and page.mouse_pos[0] >= left_w:
                    page.pan_x -= dx * 0.5
                    page.pan_y -= dy * 0.5
                    if page.tooltip_on_pan:
                        page.tooltips = 1

        class Keyboard:
            def press(self, key):
                page.calls.append(("press", key))
                if key == "Escape":
                    page.tooltips = 0

        self.mouse, self.keyboard = Mouse(), Keyboard()

    def locator(self, selector):
        return CanvasLocator(self, selector)

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait_for_selector", selector))

    def wait_for_timeout(self, ms):
        self.calls.append(("wait", ms))

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        if selector == NAV_TOGGLE:
            self.navbar_expanded = not self.navbar_expanded
            return
        if selector == LEFT_PANEL_CLOSE:
            self.left_panel_open = False
            return
        if selector == CLOSE:
            self.card = 0
            return
        if selector in ("button:has-text('Cancel'):visible", "button:has-text('close'):visible"):
            self.editor_open = False
            return
        if selector == SUBMIT:
            self.frames.append(("S", {
                "method": "CRUDHandler",
                "params": ["tok", "app", "UPDATE", {"adapter": {"properties": {"ConnectionUrl": self.field._value}}}],
                "callbackIndex": 10,
            }))
            self.frames.append(("R", {"callbackIndex": 10, "success": True}))
            self.frames.append(("S", {
                "method": "CreateAlterRecompileStmt",
                "params": ["tok", "APPLICATION", "ns.app"],
                "callbackIndex": 11,
            }))
            self.frames.append(("R", {"callbackIndex": 11, "success": True}))
            self.editor_open = False
            return
        prefix = "[data-test-id=node_container_"
        if selector.startswith(prefix) and selector.endswith("]"):
            name = selector[len(prefix):-1]
            n = self.nodes[name]
            x = n["x"] + self.pan_x
            y = n["y"] + self.pan_y
            cx = x + n["width"] / 2
            cy = y + n["height"] / 2
            vw, vh = self.viewport_size["width"], self.viewport_size["height"]
            if x < 0 or x + n["width"] > vw or y < 0 or y + n["height"] > vh:
                raise TimeoutError(f"Page.click: element is outside of the viewport ({x}, {y})")
            nav_w = (240.0 if self.navbar_expanded else 64.0) if self.has_navbar else 0.0
            left_w = nav_w + ((280.0 if self.left_panel_open else 72.0) if self.has_left_panel else 0.0)
            if cx < nav_w:
                raise TimeoutError(f"Page.click: <div id='striim-navbar'> subtree intercepts pointer events at x={cx}")
            if cx < left_w:
                raise TimeoutError(f"Page.click: <div data-test-id='left-panel'> subtree intercepts pointer events at x={cx}")
            if self.tooltips:
                raise TimeoutError("Page.click: tooltip intercepts pointer events")
            self.editor_open = True
            return
        raise AssertionError(f"unexpected click selector: {selector}")


def test_save_one_collapses_expanded_navbar_before_clicking_left_node():
    # TrailSource in a 10-node app sits under the 240px expanded navbar, clear once collapsed to 64px.
    frames = []
    page = CanvasPage(
        nodes={"TrailSource": {"x": 110.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=True,
    )
    got, touched = uibrowser._save_one(page, frames, "TrailSource")
    assert not page.navbar_expanded
    assert ("click", NAV_TOGGLE) in page.calls
    assert ("click", "[data-test-id=node_container_TrailSource]") in page.calls
    assert touched == "ConnectionUrl"
    assert got["adapter"]["properties"]["ConnectionUrl"] == "jdbc:oracle:thin:@h:1521/db"


def test_save_one_pans_offscreen_node_into_viewport_before_clicking():
    # AccountSource in a 20-node app sits far outside the 1600x1000 viewport.
    frames = []
    page = CanvasPage(
        nodes={"AccountSource": {"x": 3200.0, "y": 1400.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=True,
    )
    got, touched = uibrowser._save_one(page, frames, "AccountSource")
    assert not page.navbar_expanded
    assert any(c[0] == "wheel" for c in page.calls)
    assert ("click", "[data-test-id=node_container_AccountSource]") in page.calls
    assert touched == "ConnectionUrl"
    assert got["adapter"]["properties"]["ConnectionUrl"] == "jdbc:oracle:thin:@h:1521/db"


def test_save_one_pans_node_clear_of_collapsed_navbar_and_bottom_controls():
    frames = []
    page = CanvasPage(
        nodes={"EdgeNode": {"x": 10.0, "y": 950.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=False,
    )
    uibrowser._save_one(page, frames, "EdgeNode")
    assert any(c[0] == "wheel" for c in page.calls)
    assert ("click", "[data-test-id=node_container_EdgeNode]") in page.calls


def test_save_one_does_not_pan_when_node_is_already_in_bounds():
    # Small 3-6 node apps already fit in the unobstructed viewport: zero wheel pans.
    frames = []
    page = CanvasPage(
        nodes={"SmallSource": {"x": 400.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=False,
    )
    uibrowser._save_one(page, frames, "SmallSource")
    assert not any(c[0] == "wheel" for c in page.calls)
    assert ("click", "[data-test-id=node_container_SmallSource]") in page.calls


def test_save_one_clears_tooltip_opened_during_panning():
    frames = []
    page = CanvasPage(
        nodes={"FarNode": {"x": 2400.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=False,
        tooltip_on_pan=True,
    )
    uibrowser._save_one(page, frames, "FarNode")
    assert ("press", "Escape") in page.calls
    assert ("click", "[data-test-id=node_container_FarNode]") in page.calls


def test_focus_node_stops_when_wheel_cannot_move_and_falls_back_to_scroll():
    frames = []
    page = CanvasPage(
        nodes={"ScrollNode": {"x": 2400.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        pan_on_wheel=False,
        scrollable_container=True,
    )
    uibrowser._save_one(page, frames, "ScrollNode")
    wheel_calls = [c for c in page.calls if c[0] == "wheel"]
    assert len(wheel_calls) == 1
    assert ("scroll_into_view", "[data-test-id=node_container_ScrollNode]") in page.calls
    assert ("click", "[data-test-id=node_container_ScrollNode]") in page.calls


def test_save_one_closes_left_panel_and_pans_clear_of_it():
    # TrailSource in a 10-node app sits at x=190 (clear of collapsed 64px navbar, but inside
    # the 280px open left-panel drawer). Closing the drawer leaves only the 72px category strip
    # (right edge 136px), and panning clears even a node overlapping that strip.
    frames = []
    page = CanvasPage(
        nodes={"TrailSource": {"x": 190.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=True,
        has_left_panel=True,
        left_panel_open=True,
    )
    got, touched = uibrowser._save_one(page, frames, "TrailSource")
    assert not page.navbar_expanded
    assert not page.left_panel_open
    assert ("click", LEFT_PANEL_CLOSE) in page.calls
    assert ("click", "[data-test-id=node_container_TrailSource]") in page.calls
    assert touched == "ConnectionUrl"
    assert got["adapter"]["properties"]["ConnectionUrl"] == "jdbc:oracle:thin:@h:1521/db"


def test_save_one_pans_clear_of_unclosable_left_panel():
    # Even if left-panel has no Close button, _canvas_bounds accounts for its width and pans clear.
    frames = []
    page = CanvasPage(
        nodes={"LeftNode": {"x": 110.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        navbar_expanded=False,
        has_left_panel=True,
        left_panel_open=True,
        left_panel_closable=False,
    )
    uibrowser._save_one(page, frames, "LeftNode")
    assert any(c[0] == "wheel" for c in page.calls)
    assert ("click", "[data-test-id=node_container_LeftNode]") in page.calls


def test_focus_node_names_a_node_it_cannot_bring_into_view():
    # Twelve wheel steps cannot reach a node 50,000 px away: fail naming it, not a click timeout.
    frames = []
    page = CanvasPage(
        nodes={"FarAway": {"x": 50000.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
    )
    with pytest.raises(uibrowser.UiFormError, match="FarAway: could not be brought into view"):
        uibrowser._save_one(page, frames, "FarAway")
    assert not any(c == ("click", "[data-test-id=node_container_FarAway]") for c in page.calls)


def test_focus_node_names_a_node_neither_wheel_nor_scroll_can_move():
    frames = []
    page = CanvasPage(
        nodes={"Stuck": {"x": 2400.0, "y": 300.0, "width": 120.0, "height": 48.0}},
        frames=frames,
        pan_on_wheel=False,
        scrollable_container=False,
    )
    with pytest.raises(uibrowser.UiFormError, match="Stuck: could not be brought into view"):
        uibrowser._save_one(page, frames, "Stuck")
