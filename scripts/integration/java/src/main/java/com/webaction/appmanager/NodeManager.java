package com.webaction.appmanager;

/**
 * Mock of the platform's {@code com.webaction.appmanager.NodeManager}: only {@code epochNumber}, the
 * flow's start count the real class sets at each start (5.4.0.6C). Field's AbstractWriterApp reads it
 * reflectively (OpenProcessorCommon R17) to tell a platform restart from the run it started in.
 */
public class NodeManager {

    /** The flow's start count; null until the flow starts. */
    public Long epochNumber;

    /** Harness-only; the real constructor takes the flow and its server. */
    public NodeManager(Long epochNumber) {
        this.epochNumber = epochNumber;
    }
}
