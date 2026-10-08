package com.webaction.source.lib.prop;

/**
 * Mock of the platform's {@code com.webaction.source.lib.prop.RetryPolicy}.
 *
 * <p><b>The wait is MILLISECONDS, and that is the trap {@code AbstractWriterApp} documents.</b>
 * The constructor reads {@code (wait, count)}, and {@link Property#parseRetryPolicy} multiplies a
 * bare {@code retryInterval=30} by 1000 before it gets here — so the platform's own default is
 * 30_000, not 30. Passing 30 compiles, reads correctly, and retries a downed target a thousand
 * times faster than intended. The units are reproduced here so a tier case that pins the policy
 * pins the real number.</p>
 */
public class RetryPolicy {

    private int retryWait;
    private int maxRetries;
    private int retriesSoFar;
    private long lastRetryTime;

    /** A policy waiting {@code retryWait} MILLISECONDS, up to {@code maxRetries} times. */
    public RetryPolicy(int retryWait, int maxRetries) {
        this.retryWait = retryWait;
        this.maxRetries = maxRetries;
    }

    /** How many attempts are permitted. */
    public int getMaxRetries() {
        return maxRetries;
    }

    /** How long to wait between attempts, in milliseconds. */
    public int getRetryWait() {
        return retryWait;
    }

    /** Overrides the wait, in milliseconds. */
    public void setRetryWait(int retryWait) {
        this.retryWait = retryWait;
    }

    /** Overrides the attempt count. */
    public void setMaxRetries(int maxRetries) {
        this.maxRetries = maxRetries;
    }

    /** How many attempts have been made. */
    public int getRetriesSofar() {
        return retriesSoFar;
    }

    /** Sets how many attempts have been made. */
    public void setRetriesSofar(int retriesSoFar) {
        this.retriesSoFar = retriesSoFar;
    }

    /** Records one more attempt. */
    public void incRetriesSofar() {
        retriesSoFar++;
    }

    /** When the last attempt was made. */
    public long getLastRetryTime() {
        return lastRetryTime;
    }

    /** Records when the last attempt was made. */
    public void setLastRetryTime(long lastRetryTime) {
        this.lastRetryTime = lastRetryTime;
    }

    @Override
    public String toString() {
        return "RetryPolicy(waitMillis=" + retryWait + ", maxRetries=" + maxRetries + ")";
    }
}
