package com.example.common;

import com.webaction.runtime.BuiltInFunc;

import org.joda.time.DateTime;

/**
 * The platform's value-conversion family, behind one import.
 *
 * <p><b>Static conversion helper, independent of resolver implementations.</b>
 * {@link BuiltInFuncs} exists to be MOCKED: {@code getFieldsArray} and {@code getAliasFieldName}
 * reach the metadata repository, so a unit test must be able to substitute them, and the interface
 * is the shape that allows it. Nothing here needs substituting — these are total functions of a
 * value. A total function behind a mocked seam can fail unexpectedly: every {@code mock()} returns {@code null} from it, and the
 * integration harness's {@code Proxy} throws. {@link TypeNames} is the worked precedent.</p>
 *
 * <p>⚠ <b>The partition is "does a test need to substitute it", not "is it a total function".</b>
 * {@code BuiltInFuncs.IS_PRESENT} is itself total, and sits on the interface because it travels
 * with the two members that are not — an author placing a new method should ask which side its
 * CALLERS need, not which side its purity suggests.</p>
 *
 * <p><b>These delegate; they do not reimplement.</b> The point of the move is that a caller's
 * promise — "the result matches what a Striim CQ would have produced", say —
 * survives it. Re-deriving the coercions here would silently fork that promise the first time the
 * platform changed one, and nothing would fail.</p>
 *
 * <p><b>Eight conversion families — nine methods, since {@code TO_DATE} has two overloads — and
 * no others.</b> {@code BuiltInFunc} declares a much larger
 * {@code TO_*} family — among it {@code TO_SHORT}, {@code TO_CHAR}, {@code TO_ZONEDDATETIME},
 * {@code TO_HEX}, {@code TO_JSON_NODE}, {@code TO_OBJ}, {@code TO_JAVA_DATE},
 * {@code TO_DATE_JAVA_DATE}, {@code TO_DATEF}, the two-argument variants and the {@code TO_*_PV}
 * set. <b>None of those is here</b>, because none had a consumer when this was written, and
 * shipping surface with no caller is the {@code common.Emitter} mistake. Add one when a module
 * needs it — and do not read this class as "the conversions", which it is not.</p>
 *
 * <p>Field and alias introspection stays on {@link BuiltInFuncs}, and {@code getIndexOfColumn} is
 * introspection that belongs there rather than here — see that interface.</p>
 */
public final class ValueConversions {

    private ValueConversions() {
    }

    /** {@code BuiltInFunc.TO_INT} — null-tolerant, returns the boxed type the platform returns. */
    public static Integer toInt(Object value) {
        return BuiltInFunc.TO_INT(value);
    }

    /** {@code BuiltInFunc.TO_LONG}. */
    public static Long toLong(Object value) {
        return BuiltInFunc.TO_LONG(value);
    }

    /** {@code BuiltInFunc.TO_FLOAT}. */
    public static Float toFloat(Object value) {
        return BuiltInFunc.TO_FLOAT(value);
    }

    /** {@code BuiltInFunc.TO_DOUBLE}. */
    public static Double toDouble(Object value) {
        return BuiltInFunc.TO_DOUBLE(value);
    }

    /** {@code BuiltInFunc.TO_BOOLEAN}. */
    public static Boolean toBoolean(Object value) {
        return BuiltInFunc.TO_BOOLEAN(value);
    }

    /** {@code BuiltInFunc.TO_STRING}. */
    public static String toStringValue(Object value) {
        return BuiltInFunc.TO_STRING(value);
    }

    /** {@code BuiltInFunc.TO_BYTE_ARRAY}. */
    public static byte[] toByteArray(Object value) {
        return BuiltInFunc.TO_BYTE_ARRAY(value);
    }

    /**
     * Delegates to {@code BuiltInFunc.TO_DATE(Object)}.
     *
     * <p>🚨 <b>What that does to a given input is the platform's business, and this javadoc
     * deliberately does not say.</b> Four drafts tried; all four were false, each in a different
     * way, and each one's test passed because it sampled the value the claim happened to hold for.
     * The behaviour is irregular — a short digit string is read as a YEAR while a long one is read
     * as epoch millis — and any summary short enough for a comment has been wrong. <b>The measured
     * points live in {@code ValueConversionsTest}, asserted against {@code BuiltInFunc} itself, so
     * they track the platform rather than restate it. Add a case there and measure; do not add a
     * rule here.</b>
     *
     * <p>Two operational warnings, both of which are about CHOOSING A METHOD rather than about
     * what a conversion returns:
     * <ul>
     *   <li><b>Holding a number? Use {@link #toDateFromMillis(long)}.</b> Overload resolution sends
     *       a boxed value to this method, and boxed values throw
     *       {@code IllegalArgumentException} — the measured set is in the test. Stringifying the
     *       number instead can silently reinterpret it as a year.</li>
     *   <li><b>Migrating a call by hand? {@code BuiltInFunc.TO_DATE} took a primitive
     *       {@code int}/{@code short} by widening it to its {@code long} overload; splitting the
     *       overloads into two names here removes that path.</b> Rewriting
     *       {@code TO_DATE(anInt)} to {@code toDate(anInt)} boxes it and throws. It must become
     *       {@link #toDateFromMillis(long)}.</li>
     * </ul>
     */
    public static DateTime toDate(Object value) {
        return BuiltInFunc.TO_DATE(value);
    }

    /** {@code BuiltInFunc.TO_DATE(long)} — epoch millis. See {@link #toDate(Object)}. */
    public static DateTime toDateFromMillis(long epochMillis) {
        return BuiltInFunc.TO_DATE(epochMillis);
    }
}
