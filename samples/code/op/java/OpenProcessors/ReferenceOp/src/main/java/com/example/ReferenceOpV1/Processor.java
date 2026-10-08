package com.example.ReferenceOpV1;

import java.util.List;
import java.util.Map;
import java.util.Objects;

import com.example.common.BuiltInFuncs;
import com.example.common.Logger;
import com.example.common.WAEvents;
import com.webaction.proc.events.WAEvent;

/**
 * Copies every inbound event through unchanged and stamps {@code userdata.processed=true} on the
 * copy.
 *
 * <p>The contract is that the copy is indistinguishable from the source except for that one
 * userdata key: {@code data}, {@code before}, their presence bitmaps, {@code metadata} and the
 * type identity all travel, and the SOURCE event is never mutated.
 *
 * <p>{@code EnableInspection} additionally records, per source column, whether that column was
 * present in the source image — written to the copy's userdata as {@code column N}, one per column. It is
 * off by default because it writes one userdata entry per column per event.
 *
 * <p><b>The {@link BuiltInFuncs} seam is not required for a transform like this one, and is taken
 * here on purpose.</b> A pure copy needs no platform seam at all and could use the two-argument
 * constructor {@code Processor(props, logger)}. This class takes the three-argument form so the
 * fleet has one worked example of it; with {@code EnableInspection} off — the default — the seam is
 * never called. Do not read its presence here as a requirement when copying this module as a
 * template.
 */
public class Processor implements com.example.common.EventProcessor<WAEvent> {

    private final BuiltInFuncs funcs;
    private final Logger logger;
    private final boolean enableInspection;

    public Processor(Map<String, Object> props, BuiltInFuncs funcs, Logger logger) {
        this.funcs = funcs;
        this.logger = logger;
        this.enableInspection = Boolean.parseBoolean(
                Objects.toString(props == null ? null : props.get("EnableInspection"), "false"));
    }

    @Override
    public List<WAEvent> processEvent(WAEvent e) {
        if (logger != null) {
            logger.log(() -> "processEvent: " + (e == null ? "null event" : "copying event"));
        }
        if (e == null) {
            return List.of();
        }

        // WAEvents.copyEvent, not the platform's e.makeCopy(): makeCopy carries data/before and
        // their bitmaps, metadata/userdata/aiData, timestamp and the type UUIDs, but silently
        // drops every other SimpleEvent field. The costly one is `key` -- SimpleEvent implements
        // Partitionable, so a raw makeCopy loses the partition assignment with no error and no
        // log line. WAEvents re-stamps those fields reflectively.
        WAEvent copy = WAEvents.copyEvent(e);
        copy.putUserdata("processed", "true");

        if (enableInspection) {
            inspect(e, copy);
        }

        return List.of(copy);
    }

    private void inspect(WAEvent e, WAEvent copy) {
        if (logger != null) {
            logger.log(() -> "inspect: recording presence for "
                    + (e.data == null ? 0 : e.data.length) + " column(s)");
        }
        if (e.data == null) {
            return;
        }
        // e.data is passed BY IDENTITY, not as a convenience. The platform's IS_PRESENT picks
        // which bitmap to read by comparing the image argument against event.data and
        // event.before with ==; an array that is neither -- a copy, or the copy's data -- matches
        // nothing and every column reports absent, with no error. So this must ask about the
        // SOURCE event and the SOURCE array, and write the answer onto the copy.
        //
        // Bits are packed SEVEN per byte, not eight: the platform indexes
        // bitmap[index / 7] and tests 1 << (index % 7).
        //
        // Because the answer comes from the bitmap and never from the value, this is a PRESENCE
        // check and not a null check: a column explicitly set to NULL is still present. "Not
        // present" means the source supplied no value for that column at all, which is the
        // ordinary shape of a CDC UPDATE image carrying only changed columns.
        for (int i = 0; i < e.data.length; i++) {
            copy.putUserdata("column " + i, funcs.IS_PRESENT(e, e.data, i));
        }
    }

    @Override
    public void close() {

    }
}
