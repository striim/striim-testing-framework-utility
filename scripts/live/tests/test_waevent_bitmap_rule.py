"""WAEvent presence-bitmap guard (livetest.waevent_guard) -- hermetic rule tests.

The rule: no Java main source may swap a WAEvent's data/before array without a presence-bitmap
rebuild nearby (a stale bitmap poisons DatabaseWriter's server-wide column-pattern cache ->
silently NULLed columns; fixed historically). The registry-wide
gate over java/{OpenProcessors,UserDefinedFunctions}/*/src/main stays in the test repo that
owns that corpus.

The fixtures below are synthetic. Each reproduces the shape of one bug class the rule exists
for (rebuild from the DATA()/BEFORE() maps, swap in a narrower array, swap in a wider array),
so the rule provably catches them without depending on git history at collection time.
"""

from __future__ import annotations

from pathlib import Path

from livetest.waevent_guard import RULE, check_waevent_bitmap_rebuild


# ---------------------------------------------------------------------------------------
# Bug class 1: rebuild data/before from the presence-gated DATA()/BEFORE() maps, which
# silently shrinks a partial CDC event.
# ---------------------------------------------------------------------------------------
_REBUILD_FROM_MAPS = """
public class MaskingOp extends StriimOpenProcessor {
    public void run() {
        final IBatch<WAEvent> batch = getAdded();
        for (WAEvent wrapper : batch) {
            send(mask((com.webaction.proc.events.WAEvent) wrapper.data));
        }
    }

    public com.webaction.proc.events.WAEvent mask(final com.webaction.proc.events.WAEvent event) {
        HashMap<String, Object> data = BuiltInFunc.DATA(event);
        HashMap<String, Object> before = BuiltInFunc.BEFORE(event);
        for (String key : data.keySet()) {
            data.put(key, maskValue(data.get(key)));
        }
        event.data = data.values().toArray();
        if (event.before != null) {
            for (String key : before.keySet()) {
                before.put(key, maskValue(before.get(key)));
            }
            event.before = before.values().toArray();
        }
        return event;
    }
}
"""


# ---------------------------------------------------------------------------------------
# Bug class 2: swap in narrower filtered arrays, no bitmap rebuild.
# ---------------------------------------------------------------------------------------
_NARROW_SWAP = """
public class ColumnFilter {
    public WAEvent keep(final WAEvent event, final int[] keptIndexes) {
        Object[] newData = new Object[keptIndexes.length];
        Object[] newBefore = event.before != null ? new Object[keptIndexes.length] : null;
        for (int i = 0; i < keptIndexes.length; i++) {
            newData[i] = event.data[keptIndexes[i]];
            if (newBefore != null) {
                newBefore[i] = event.before[keptIndexes[i]];
            }
        }
        event.data = newData;
        if (newBefore != null) {
            event.before = newBefore;
        }
        event.typeUUID = filteredType.getUuid();
        return event;
    }
}
"""


# ---------------------------------------------------------------------------------------
# Bug class 3: swap in widened arrays, no bitmap rebuild.
# ---------------------------------------------------------------------------------------
_WIDEN_SWAP = """
public class ColumnAppender {
    private void append(WAEvent event, List<Object> extra) {
        int n = event.data.length;
        Object[] newData = Arrays.copyOf(event.data, n + extra.size());
        for (int i = 0; i < extra.size(); i++) {
            newData[n + i] = extra.get(i);
        }
        Object[] newBefore = event.before != null ? Arrays.copyOf(event.before, n + extra.size()) : null;
        event.data = newData;
        if (newBefore != null) {
            event.before = newBefore;
        }
        event.typeUUID = widenedType.getUuid();
    }
}
"""


def _flagged_members(text: str) -> list[str]:
    vs = check_waevent_bitmap_rebuild(Path("Fixture.java"), text)
    assert all(v.rule == RULE for v in vs)
    return [v.message.split()[0] for v in vs]


def test_rebuild_from_maps_is_flagged():
    assert _flagged_members(_REBUILD_FROM_MAPS) == ["event.data", "event.before"]


def test_narrow_swap_is_flagged():
    assert _flagged_members(_NARROW_SWAP) == [
        "event.data",
        "event.before",
    ]


def test_widen_swap_is_flagged():
    assert _flagged_members(_WIDEN_SWAP) == [
        "event.data",
        "event.before",
    ]


# ---------------------------------------------------------------------------------------
# Sanctioned idioms must NOT be flagged.
# ---------------------------------------------------------------------------------------


def test_fresh_construction_with_setdata_is_clean():
    # the ubiquitous factory idiom
    assert _flagged_members("""
        class C {
            WAEvent make(String ddl) {
                WAEvent event = new WAEvent(1, null);
                event.data = new Object[1];
                event.before = new Object[1];
                event.setData(0, ddl);
                return event;
            }
        }
    """) == []


def test_explicit_bitmap_rebuild_after_swap_is_clean():
    # the sanctioned swap-then-rebuild form
    assert _flagged_members("""
        class C {
            void filter(WAEvent event, Object[] newData, int newColumnCount) {
                event.data = newData;
                event.dataPresenceBitMap = new byte[newColumnCount / 7 + 1];
                event.beforePresenceBitMap = new byte[newColumnCount / 7 + 1];
            }
        }
    """) == []


def test_setdata_repopulation_within_window_is_clean():
    assert _flagged_members("""
        class C {
            void widen(WAEvent event, Object[] newData) {
                event.data = newData;
                for (int i = 0; i < newData.length; i++) {
                    event.setData(i, newData[i]);
                }
            }
        }
    """) == []


def test_null_and_fresh_allocation_rhs_are_clean():
    assert _flagged_members("""
        class C {
            void reset(WAEvent event, int n) {
                event.data = null;
                event.before = new Object[n];
                event.data = new Object[]{ "marker" };
            }
        }
    """) == []


def test_non_waevent_receivers_are_ignored():
    # JsonNodeEvent/TableMeta/... carry .data too; locals named data; this.data
    assert _flagged_members("""
        class C {
            JsonNodeEvent convert(ObjectMapper mapper) {
                JsonNodeEvent newEvent = new JsonNodeEvent();
                newEvent.data = mapper.createArrayNode();
                TableMeta ss = src.get(t);
                ss.data = source.getData(ss, numberOfRows);
                this.data = somethingElse;
                undeclared.data = alsoIgnored;
                return newEvent;
            }
        }
    """) == []


def test_nearest_declaration_wins_over_earlier_same_name():
    # `event` is an IBatch in run() but a WAEvent parameter in process() -- only the
    # WAEvent-typed use may flag.
    assert _flagged_members("""
        class C {
            void run() {
                final IBatch<WAEvent> event = getAdded();
                event.data = notAWAEventSoIgnored();
            }
            void process(final com.webaction.proc.events.WAEvent event) {
                event.data = map.values().toArray();
            }
        }
    """) == ["event.data"]


def test_comments_strings_and_comparisons_are_ignored():
    assert _flagged_members("""
        class C {
            void inspect(WAEvent event) {
                // event.data = wouldBeABugIfLive();
                /* event.before = alsoCommentedOut(); */
                String s = "event.data = decoy();";
                if (event.data == null || event.before != null) {
                    log(event.data.length);
                }
            }
        }
    """) == []
