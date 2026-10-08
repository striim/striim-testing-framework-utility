package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

/** The bean type drops the module version in both numbering schemes, as OpJmxRegistry does. */
class JmxSnapshotTypeTest {

    @Test
    void stripsMajorMinorVersions() {
        assertEquals("ExampleCacheOp", JmxSnapshot.unversioned("ExampleCacheOpV8_7"));
        assertEquals("ExampleReader", JmxSnapshot.unversioned("ExampleReaderV2_5"));
    }

    @Test
    void stripsLetterVersions() {
        assertEquals("LookupOp", JmxSnapshot.unversioned("LookupOpV8F"));
        assertEquals("LookupOp", JmxSnapshot.unversioned("LookupOpV1"));
    }

    @Test
    void leavesAnUnversionedSegmentAlone() {
        assertEquals("LookupOp", JmxSnapshot.unversioned("LookupOp"));
    }
}
