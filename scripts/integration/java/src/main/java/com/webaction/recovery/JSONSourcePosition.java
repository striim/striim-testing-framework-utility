package com.webaction.recovery;

/**
 * Mock of the platform's {@code com.webaction.recovery.JSONSourcePosition}.
 *
 * <p><b>Why it exists.</b> {@code common.MdrPositionCarrier} — the shared MDR state
 * restore/persist helper every recovery-shaped OP uses — contains {@code instanceof} checks and a
 * wrapping constructor against this class on the plain restore path. Loading the carrier in the
 * harness JVM resolves the reference and dies with {@code NoClassDefFoundError} unless the type
 * exists here, which would make MDR-seeded integration cases impossible. Same reason
 * {@link SourcePosition} and {@link ComponentCheckpoint} are mocked.</p>
 *
 * <p><b>Shape matches the platform class on 5.4</b>: public
 * {@code (String, long)} constructor, {@code getSourceName()} (the carrier reads its payload back
 * through that accessor), the setters, {@code getSequenceNo()}, and the
 * {@code compareTo} overrides. The harness never compares positions; the carrier only ever
 * constructs and calls {@code getSourceName()} here.</p>
 */
public class JSONSourcePosition extends SourcePosition {

    private static final long serialVersionUID = 1L;

    private String sourceName;
    private long sequenceNo;

    public JSONSourcePosition(String json, long sequenceNo) {
        this.sourceName = json;
        this.sequenceNo = sequenceNo;
    }

    public void setSourceName(String sourceName) {
        this.sourceName = sourceName;
    }

    public String getSourceName() {
        return sourceName;
    }

    public long getSequenceNo() {
        return sequenceNo;
    }

    public void setSequenceNo(long sequenceNo) {
        this.sequenceNo = sequenceNo;
    }

    @Override
    public int compareTo(SourcePosition other) {
        return Long.compare(sequenceNo,
                other instanceof JSONSourcePosition
                        ? ((JSONSourcePosition) other).sequenceNo : -1L);
    }

    @Override
    public String toString() {
        return "JSONSourcePosition(payload=" + sourceName + ", seq=" + sequenceNo + ")";
    }
}
