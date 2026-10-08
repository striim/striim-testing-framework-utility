package com.striim.testing.inttest;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * The {@code udf:} block of a {@code test.yaml}, as it arrives on the request wire
 * (docs/INTEGRATION-TESTS.md, PERF_SPEC.md §3): drives a bare UDF's {@code public static} functions
 * directly, with no {@code Processor}/constructor/manifest involved. Jackson-bean
 * shaped (public mutable fields, default no-arg constructor), matching {@link
 * IntegrationProcessor.Request}/{@link PerformanceProcessor.PerfRequest}'s own style.
 *
 * <p>
 * The YAML author writes {@code class:}; that becomes {@link #className} on the wire
 * because {@code class} cannot be a Java field name. Every other key is spelled
 * identically in YAML, on the wire, and here.
 */
public final class UdfSpec {

    /** Fully-qualified name of the UDF class inside the jar named by {@code opJar}. */
    public String className;

    /** {@code "waevent"} (default) or {@code "jsonnode"} -- the runtime type of the {@code $} register. */
    public String kind = "waevent";

    /** {@code kind: jsonnode} only: the envelope slot to read the JSON text from (e.g. {@code "data[0]"}). */
    public String source;

    /** {@code kind: jsonnode} only: the envelope slot to write the result back to. Defaults to {@link #source}. */
    public String target;

    /** Ordered pipeline steps, applied left to right. Required, non-empty. */
    public List<Step> pipeline = new ArrayList<>();

    /** One pipeline step: a static method call plus where to send its result. */
    public static final class Step {
        /** Simple (unqualified) static-method name on {@link UdfSpec#className}. */
        public String function;

        /** Encoded args; each entry is a single-key map -- see {@code UdfCore#decodeArg}. */
        public List<Map<String, Object>> args = new ArrayList<>();

        /**
         * When non-null, this step's result is bound to this name (retrievable by a
         * later step's {@code {"ref": name}} arg) INSTEAD of updating the {@code $}
         * register. When null, the result becomes the new {@code $}.
         */
        public String as;
    }
}
