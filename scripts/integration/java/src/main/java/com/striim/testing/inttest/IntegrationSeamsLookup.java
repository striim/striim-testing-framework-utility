package com.striim.testing.inttest;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.URLClassLoader;
import java.util.Map;

/**
 * how the harness supplies a constructor parameter it has never heard of.
 *
 * <p><b>The problem this exists for.</b> {@code resolveConstructorArgument} knows four parameter
 * types — {@code Map}, {@code Logger}, {@code BuiltInFuncs}, {@code TypeResolver} — which covers
 * every in-stream op. The five readers ask for module-specific seams instead, and those seams have
 * <b>no overlap with each other</b>: 6 methods here, 4 there, 3 + 4 + 3 + 6 across the rest. No
 * shared {@code PlatformContext} can absorb that, and a harness that grew a case per module would
 * stop being op-agnostic — the property that lets one tier serve fourteen modules.</p>
 *
 * <p><b>The contract.</b> An op that needs seams the harness cannot invent ships a class named
 * {@code IntegrationSeams} in its core package, with one static method:</p>
 *
 * <pre>
 *   public static Object seamFor(String parameterTypeName, Map&lt;String, Object&gt; properties)
 * </pre>
 *
 * <p>The harness calls it for each parameter it cannot resolve itself, passing the parameter type
 * name and the case's <i>enriched</i> properties — the same view the core gets, reserved keys and
 * {@code Password} wrapping included.</p>
 *
 * <p><b>{@code null} means "pass null, deliberately", and nothing else.</b> A reader's
 * recovered-position parameter is null on a first run, so the harness cannot treat null as an
 * error. That makes the fall-through case dangerous: an op whose {@code if}-chain simply fails to
 * recognise a type would inject a silent null and the case would NPE later, or assert "0 events"
 * and pass for the wrong reason. <b>So the contract is: an op that does not recognise a type MUST
 * THROW.</b> Its own exception then names the mistake, at the point of the mistake.</p>
 *
 * <p><b>Match on {@code Class.getName()}, not a suffix.</b> The name passed here is
 * {@link Class#getName()}, so a nested seam type arrives as {@code com.foo.Processor$TableReader}.
 * A {@code endsWith(".TableReader")} test misses that and falls through — which is exactly the
 * silent-null trap above. Compare against {@code TableReader.class.getName()}.</p>
 *
 * <p><b>Why a class in the op rather than a block in the YAML.</b> A seam is code — a scripted DAO,
 * a fake type resolver — and a YAML block would have to name classes and constructor arguments,
 * which is a worse programming language than Java. Keeping it in the op also puts the knowledge
 * next to the thing it describes, so a constructor change and its seam move together.</p>
 *
 * <p><b>Why it may live in {@code src/main}.</b> The same reasoning as a reserved properties key
 * such as an op's reserved {@code RUNTIME_KEY}: it is
 * inert in production — nothing calls it unless a harness does — and the alternative is a
 * constructor shape that exists only to be testable, which is what Rule 5 forbids.</p>
 */
final class IntegrationSeamsLookup {

    static final String CLASS_SIMPLE_NAME = "IntegrationSeams";
    static final String METHOD_NAME = "seamFor";

    /**
     * The second convention method, added when a change-data reader could not build an integration
     * tier.
     *
     * <p><b>Why a separate method rather than reusing {@link #METHOD_NAME}.</b> {@code seamFor}
     * keys on a TYPE, which is exactly right for a constructor parameter and useless for a channel:
     * A change-data reader's channel declares {@code appQualifiedName()} and
     * {@code componentQualifiedName()}, both returning {@code String}, so a type key cannot tell
     * them apart. The METHOD NAME is the only thing that distinguishes them.</p>
     */
    static final String CHANNEL_METHOD_NAME = "channelValueFor";

    private IntegrationSeamsLookup() {
    }

    /**
     * Asks the op for a value of {@code paramType}.
     *
     * @return the op's answer, which may legitimately be {@code null}
     * @throws IllegalStateException if the op ships no {@code IntegrationSeams}, if its method has
     *     the wrong shape, or if it returns something that is not of the requested type — each
     *     with a message naming the convention, because the alternative is a reflection error a
     *     reader has to decode
     */
    static Object seamFor(String corePackage, Class<?> paramType, Map<String, Object> properties,
            URLClassLoader child, String coreClassName) {
        String seamsClassName = corePackage + "." + CLASS_SIMPLE_NAME;
        Class<?> seamsClass;
        try {
            // initialize = false: an op whose IntegrationSeams has a throwing static initializer
            // would otherwise raise ExceptionInInitializerError -- an Error, so the catch below
            // misses it and the convention-naming message is lost. Initialization still happens
            // on first use, inside the invoke() below, whose handler wraps Throwable properly.
            seamsClass = Class.forName(seamsClassName, false, child);
        } catch (ClassNotFoundException e) {
            throw new IllegalStateException(
                    "Don't know how to supply a constructor parameter of type "
                            + paramType.getName() + " for " + coreClassName
                            + ". The harness resolves java.util.Map, Logger, BuiltInFuncs and"
                            + " TypeResolver itself; anything else is an op-specific seam, so this"
                            + " op must ship " + seamsClassName + " with a static "
                            + METHOD_NAME + "(String parameterTypeName, Map<String, Object>"
                            + " properties) — see IntegrationSeamsLookup", e);
        }

        Method seamFor;
        try {
            seamFor = seamsClass.getMethod(METHOD_NAME, String.class, Map.class);
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(seamsClassName + " exists but has no static "
                    + METHOD_NAME + "(String, Map) — the harness calls exactly that signature", e);
        }

        Object seam;
        try {
            seamFor.setAccessible(true);
            seam = seamFor.invoke(null, paramType.getName(), properties);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            throw new IllegalStateException(seamsClassName + "." + METHOD_NAME + " threw while"
                    + " building a " + paramType.getName(), cause);
        } catch (Exception e) {
            throw new IllegalStateException("Could not call " + seamsClassName + "." + METHOD_NAME
                    + "; it must be public and static", e);
        }

        // null is a legitimate answer for a REFERENCE parameter -- a reader's recovered position
        // is null on a first run -- but never for a primitive, where newInstance would fail with
        // the opaque "argument type mismatch" this class exists to replace.
        if (seam == null) {
            if (paramType.isPrimitive()) {
                throw new IllegalStateException(seamsClassName + "." + METHOD_NAME
                        + " returned null for primitive parameter type " + paramType.getName()
                        + ", which cannot be null");
            }
            return null;
        }

        // isInstance() is ALWAYS false for a primitive Class -- int.class.isInstance(
        // Integer.valueOf(5)) is false -- so a primitive parameter would be rejected here even
        // though newInstance unboxes it happily. Compare against the wrapper instead.
        Class<?> accepted = paramType.isPrimitive() ? wrapperFor(paramType) : paramType;
        if (!accepted.isInstance(seam) && !widensTo(paramType, seam)) {
            throw new IllegalStateException(seamsClassName + "." + METHOD_NAME + " returned a "
                    + seam.getClass().getName() + " for parameter type " + paramType.getName()
                    + ", which the constructor cannot accept (expected "
                    + accepted.getSimpleName() + ")");
        }
        return seam;
    }

    /**
     * {@code Constructor.newInstance} performs method-invocation conversion: unboxing AND
     * widening. So an {@code Integer} really is acceptable for a {@code long} parameter — a
     * likely shape for a position or offset seam — and rejecting it here would be stricter than
     * the reflection call this check exists to protect.
     */
    private static boolean widensTo(Class<?> paramType, Object seam) {
        if (!paramType.isPrimitive() || !(seam instanceof Number || seam instanceof Character)) {
            return false;
        }
        String target = paramType.getName();
        if (seam instanceof Byte) {
            return "short".equals(target) || "int".equals(target) || "long".equals(target)
                    || "float".equals(target) || "double".equals(target);
        } else if (seam instanceof Short || seam instanceof Character) {
            return "int".equals(target) || "long".equals(target) || "float".equals(target)
                    || "double".equals(target);
        } else if (seam instanceof Integer) {
            return "long".equals(target) || "float".equals(target) || "double".equals(target);
        } else if (seam instanceof Long) {
            return "float".equals(target) || "double".equals(target);
        } else if (seam instanceof Float) {
            return "double".equals(target);
        }
        return false;
    }

    private static Class<?> wrapperFor(Class<?> primitive) {
        if (primitive == int.class) {
            return Integer.class;
        } else if (primitive == long.class) {
            return Long.class;
        } else if (primitive == boolean.class) {
            return Boolean.class;
        } else if (primitive == double.class) {
            return Double.class;
        } else if (primitive == float.class) {
            return Float.class;
        } else if (primitive == short.class) {
            return Short.class;
        } else if (primitive == byte.class) {
            return Byte.class;
        } else if (primitive == char.class) {
            return Character.class;
        }
        throw new IllegalStateException("no wrapper for primitive " + primitive.getName());
    }

    /**
     * Asks the op to answer one channel method the harness cannot answer itself.
     *
     * <p><b>Why this exists.</b> {@link SourceCore}'s channel proxy answers {@code emit} and
     * {@code checkpoint} — the two calls the reader contract is actually about — and used to throw
     * on everything else, with a message asserting that "a reader core should only emit and
     * checkpoint through its channel". <b>That assertion was false for four of the five readers.</b>
     * The first reader with a tier reached its platform state through a CONSTRUCTOR seam, which
     * {@link #seamFor} already covered, so the gap stayed invisible while it was the only reader
     * with a tier. A change-data reader reaches it through the channel — {@code tick}'s first
     * statement resolves a checkpoint file from {@code checkpointDir()},
     * {@code appQualifiedName()} and {@code componentQualifiedName()} — and an API-polling reader has the
     * same shape. So the harness had to grow the capability rather than the ops shedding it.</p>
     *
     * <p><b>The op stays in charge of the answer.</b> The harness deliberately does not invent
     * defaults for unknown channel methods: a fabricated {@code null} app name is precisely how a
     * reader's checkpoint identity degrades to a node-wide shared filename, which is the failure
     * this fleet's naming code exists to prevent. An op that has not declared an answer gets an
     * error naming the method, not a plausible-looking value.</p>
     *
     * @return the op's answer, which may legitimately be {@code null}
     * @throws IllegalStateException if the op ships no {@code IntegrationSeams}, has no
     *     {@link #CHANNEL_METHOD_NAME}, or answers with something the method cannot return
     */
    static Object channelValueFor(String corePackage, String channelIfaceName, String methodName,
            Class<?> returnType, Map<String, Object> properties, ClassLoader child) {
        String seamsClassName = corePackage + "." + CLASS_SIMPLE_NAME;
        Class<?> seamsClass;
        try {
            seamsClass = Class.forName(seamsClassName, false, child);
        } catch (ClassNotFoundException e) {
            throw new IllegalStateException(channelIfaceName + "." + methodName
                    + " is not something this harness answers itself (it answers emit and"
                    + " checkpoint), so this op must ship " + seamsClassName + " with a static "
                    + CHANNEL_METHOD_NAME + "(String methodName, Map<String, Object> properties)"
                    + " — see IntegrationSeamsLookup", e);
        }

        Method channelValueFor;
        try {
            channelValueFor = seamsClass.getMethod(CHANNEL_METHOD_NAME, String.class, Map.class);
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(seamsClassName + " exists but has no static "
                    + CHANNEL_METHOD_NAME + "(String, Map), which is what answers "
                    + channelIfaceName + "." + methodName + ". A reader whose channel declares more"
                    + " than emit/checkpoint needs one; " + METHOD_NAME + " cannot serve here"
                    + " because it keys on a TYPE and two channel methods may share a return type",
                    e);
        }

        Object value;
        try {
            channelValueFor.setAccessible(true);
            value = channelValueFor.invoke(null, methodName, properties);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            throw new IllegalStateException(seamsClassName + "." + CHANNEL_METHOD_NAME
                    + " threw while answering " + channelIfaceName + "." + methodName, cause);
        } catch (Exception e) {
            throw new IllegalStateException("Could not call " + seamsClassName + "."
                    + CHANNEL_METHOD_NAME + "; it must be public and static", e);
        }

        if (value == null) {
            if (returnType.isPrimitive()) {
                throw new IllegalStateException(seamsClassName + "." + CHANNEL_METHOD_NAME
                        + " answered null for " + channelIfaceName + "." + methodName
                        + ", whose return type " + returnType.getName() + " is primitive and cannot"
                        + " hold it");
            }
            // null is legitimate for a reference return -- "the platform cannot answer yet" is a
            // real state these accessors model, and a case may want to exercise it.
            return null;
        }
        if (!box(returnType).isInstance(value)) {
            throw new IllegalStateException(seamsClassName + "." + CHANNEL_METHOD_NAME
                    + " answered a " + value.getClass().getName() + " for " + channelIfaceName
                    + "." + methodName + ", which returns " + returnType.getName());
        }
        return value;
    }

    /** Primitive return types arrive boxed from {@code invoke}, so compare against the box. */
    private static Class<?> box(Class<?> type) {
        if (!type.isPrimitive()) {
            return type;
        }
        if (type == boolean.class) return Boolean.class;
        if (type == int.class) return Integer.class;
        if (type == long.class) return Long.class;
        if (type == double.class) return Double.class;
        if (type == float.class) return Float.class;
        if (type == short.class) return Short.class;
        if (type == byte.class) return Byte.class;
        if (type == char.class) return Character.class;
        return type;
    }
}
