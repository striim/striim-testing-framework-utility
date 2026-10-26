package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class FailureChainTest {

    @Test
    void namesTheFailureEachCauseAndEachSuppressedOnce() {
        IllegalStateException root = new IllegalStateException("value does not fit scale 2");
        RuntimeException wrapper = new RuntimeException("drive failed", root);
        wrapper.addSuppressed(new IllegalArgumentException("close failed"));
        assertEquals("java.lang.RuntimeException: drive failed\n"
                + "java.lang.IllegalStateException: value does not fit scale 2\n"
                + "java.lang.IllegalArgumentException: close failed\n",
                IntegrationProcessor.failureChain(wrapper));
    }

    @Test
    void followsTheNextExceptionOfABatchFailure() {
        java.sql.BatchUpdateException batch = new java.sql.BatchUpdateException("batch failed", new int[0]);
        batch.setNextException(new java.sql.SQLException("value too long for column NAME"));
        String chain = IntegrationProcessor.failureChain(new RuntimeException("flush failed", batch));
        org.junit.jupiter.api.Assertions.assertTrue(chain.contains("value too long for column NAME"), chain);
    }

    @Test
    void aLongBatchChainIsCappedAndKeepsItsFirstEntries() {
        java.sql.BatchUpdateException batch = new java.sql.BatchUpdateException("batch failed", new int[0]);
        java.sql.SQLException last = batch;
        for (int row = 0; row < 120; row++) {
            java.sql.SQLException next = new java.sql.SQLException("row " + row + " refused");
            last.setNextException(next);
            last = next;
        }
        String chain = IntegrationProcessor.failureChain(batch);
        String[] lines = chain.split("\n");
        assertEquals(IntegrationProcessor.FAILURE_CHAIN_LIMIT + 1, lines.length, chain);
        org.junit.jupiter.api.Assertions.assertTrue(chain.contains("batch failed") && chain.contains("row 0 refused"), chain);
        assertEquals("... 71 more", lines[lines.length - 1]);
    }
}
