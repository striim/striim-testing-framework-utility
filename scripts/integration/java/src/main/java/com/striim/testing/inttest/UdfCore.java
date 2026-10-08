package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.Array;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;
import com.webaction.runtime.BuiltInFunc;

/**
 * The bare-UDF driver (the `udf:` schema -- docs/INTEGRATION-TESTS.md): loads a UDF jar (a plain class of
 * {@code public static} methods -- no {@code Striim-Service-Implementation} manifest
 * entry, no {@code Processor}, never instantiated) into a child classloader exactly as
 * {@link OperatorCore} does, and reflectively resolves + calls each {@code udf.pipeline}
 * step's static method in order, threading one value through a {@code $} register.
 *
 * <p>
 * <b>Register kinds (docs/INTEGRATION-TESTS.md's {@code udf.kind}).</b> {@code "waevent"} (default): the
 * register starts as the input {@link WAEvent} and the pipeline's final value IS the
 * emitted event (a {@code null} result means no emission, mirroring {@link
 * OperatorCore#processEvent}'s "no emission" case). {@code "jsonnode"}: the register
 * starts as the parsed {@code com.fasterxml.jackson.databind.JsonNode} read from one
 * envelope slot of the input event (default {@code data[0]}; the WAEvent-JSON fixture
 * format is unchanged in either kind -- a {@code jsonnode} case's {@code data[0]} is
 * simply a JSON-document STRING, exactly the shape a production CQ hands {@code
 * JSONPipeline} via {@code JSONParse(TO_STRING(data[0]))}), and the pipeline's final
 * value is serialized back into a (possibly different) slot of the SAME input event,
 * which is then emitted -- one input event still yields exactly one output event.
 *
 * <p>
 * <b>{@code $} vs. {@code as:}.</b> A step without {@code as:} updates the register; a
 * step WITH {@code as:} binds its result to that name (retrievable by a later step's
 * {@code {"ref": name}} arg) and leaves the register untouched -- this is what lets a
 * side-effecting toggle like {@code WASetLogging(false)} (which returns a {@code
 * boolean}, not the event) sit inside a pipeline without breaking the chain.
 *
 * <p>
 * <b>Method resolution</b> ({@link #resolveMethod}) mirrors the two-phase (fixed-arity,
 * then varargs) overload search javac performs: an exact-arity non-varargs overload is
 * preferred over a varargs one, which is exactly why UDFs ship concrete
 * non-varargs overloads for their common call shapes (a CQ's generated code cannot
 * itself synthesize an empty varargs array). Each step's resolution is cached against the
 * decoded arguments' runtime types, so a `run_size: 100k` perf replay resolves reflection
 * once and re-invokes a cached {@link Method} thereafter.
 */
final class UdfCore implements EventDriver {

    private static final ObjectMapper MAPPER = new ObjectMapper();
    // Digits bounded to 9 (max 999,999,999) so a match can never overflow int and
    // Integer.parseInt below never throws; \z (not $) so a trailing newline -- which
    // $ would otherwise tolerate in default (non-MULTILINE) mode -- does not slip
    // through. Kept byte-equivalent with manifest.py's own _SLOT_RE (same digit
    // bound, \A...\z via re.ASCII + explicit \Z, ASCII-only \S).
    private static final Pattern SLOT_RE = Pattern.compile("^(?:(?:data|before)\\[(\\d{1,9})\\]|userdata\\.(\\S+))\\z");

    private final String className;
    private final Class<?> udfClass;
    private final String kind;   // "waevent" | "jsonnode"
    private final String source; // jsonnode only
    private final String target; // jsonnode only
    private final List<UdfSpec.Step> steps;

    // Per-step resolved-method cache, invalidated on a decoded-argument-type change.
    private final ResolvedCall[] cache;
    private final Class<?>[][] cacheSignature;

    private UdfCore(String className, Class<?> udfClass, String kind, String source, String target,
            List<UdfSpec.Step> steps) {
        this.className = className;
        this.udfClass = udfClass;
        this.kind = kind;
        this.source = source;
        this.target = target;
        this.steps = steps;
        this.cache = new ResolvedCall[steps.size()];
        this.cacheSignature = new Class<?>[steps.size()][];
    }

    /**
     * Loads {@code udfJar} into a child classloader and validates {@code spec} (defence
     * in depth -- the Python loader already validated an authored {@code test.yaml}, but
     * a direct-Java caller, e.g. a JUnit test, has not). Seeds the mock {@link
     * BuiltInFunc}'s column-schema registry from {@code types} -- a REPLACE, not a merge,
     * so a second {@code build()} in the same JVM cannot leak a previous test's schemas;
     * {@link OperatorCore} never touches this registry.
     */
    static UdfCore build(File udfJar, UdfSpec spec, Map<String, Object> types) throws Exception {
        if (spec.className == null || spec.className.isBlank()) {
            throw new IllegalStateException("udf.class is required");
        }
        String kind = spec.kind == null ? "waevent" : spec.kind;
        if (!kind.equals("waevent") && !kind.equals("jsonnode")) {
            throw new IllegalStateException("udf.kind must be 'waevent' or 'jsonnode', got " + kind);
        }
        String source = spec.source;
        String target = spec.target;
        if (kind.equals("waevent")) {
            if (source != null || target != null) {
                throw new IllegalStateException("udf.source/udf.target only apply when udf.kind is 'jsonnode'");
            }
        } else {
            source = source == null ? "data[0]" : source;
            target = target == null ? source : target;
            requireValidSlot(source, "udf.source");
            requireValidSlot(target, "udf.target");
        }
        if (spec.pipeline == null || spec.pipeline.isEmpty()) {
            throw new IllegalStateException("udf.pipeline is required and must be non-empty");
        }
        for (UdfSpec.Step step : spec.pipeline) {
            if (step.function == null || step.function.isBlank()) {
                throw new IllegalStateException("every udf.pipeline step needs a non-blank 'function'");
            }
        }

        BuiltInFunc.setColumnSchemas(IntegrationProcessor.parseTypeSchemas(types));

        URL udfJarUrl = udfJar.toURI().toURL();
        URLClassLoader child = new URLClassLoader(new URL[] { udfJarUrl }, IntegrationProcessor.class.getClassLoader());

        Class<?> udfClass;
        try {
            udfClass = Class.forName(spec.className, true, child);
        } catch (ClassNotFoundException e) {
            throw new IllegalStateException("UDF class not found: " + spec.className + " in " + udfJar, e);
        }

        return new UdfCore(spec.className, udfClass, kind, source, target, spec.pipeline);
    }

    private static void requireValidSlot(String slot, String fieldName) {
        if (slot == null || !SLOT_RE.matcher(slot).matches()) {
            throw new IllegalStateException(fieldName + " must match data[N]/before[N]/userdata.KEY, got " + slot);
        }
    }

    @Override
    public List<WAEvent> processEvent(Event inputEvent) throws Exception {
        WAEvent event = EventDriver.requireWAEvent(inputEvent, "a udf: pipeline");
        Object register;
        if (kind.equals("waevent")) {
            register = event;
        } else {
            Object raw = readSlot(event, source);
            if (raw == null) {
                register = null;
            } else if (raw instanceof String text) {
                register = MAPPER.readTree(text);
            } else {
                throw new IllegalStateException(
                        "udf.source " + source + " must hold a JSON-document string, got a " + raw.getClass().getName());
            }
        }

        Map<String, Object> bindings = new LinkedHashMap<>();
        for (int i = 0; i < steps.size(); i++) {
            UdfSpec.Step step = steps.get(i);
            Object[] args = materialize(step, register, bindings);
            Object result = invoke(i, step, args);
            if (step.as != null) {
                bindings.put(step.as, result);
            } else {
                register = result;
            }
        }

        if (kind.equals("waevent")) {
            if (register == null) {
                return List.of();
            }
            if (!(register instanceof WAEvent)) {
                throw new IllegalStateException(
                        className + " pipeline ended with a " + register.getClass().getName() + ", not a WAEvent (kind: waevent)");
            }
            return List.of((WAEvent) register);
        }

        if (register != null && !(register instanceof JsonNode)) {
            throw new IllegalStateException(
                    className + " pipeline ended with a " + register.getClass().getName() + ", not a JsonNode (kind: jsonnode)");
        }
        writeSlot(event, target, register == null ? null : MAPPER.writeValueAsString(register));
        return List.of(event);
    }

    /** A bare UDF has no constructed instance/lifecycle to release. */
    @Override
    public void close() {
    }

    // ------------------------------------------------------------------
    // Argument materialization / decoding (`udf:` arg wire form -- docs/INTEGRATION-TESTS.md)
    // ------------------------------------------------------------------

    private Object[] materialize(UdfSpec.Step step, Object register, Map<String, Object> bindings) {
        List<Map<String, Object>> argSpecs = step.args == null ? List.of() : step.args;
        Object[] args = new Object[argSpecs.size()];
        for (int i = 0; i < argSpecs.size(); i++) {
            args[i] = decodeArg(step, i, argSpecs.get(i), register, bindings);
        }
        return args;
    }

    private Object decodeArg(UdfSpec.Step step, int argIndex, Map<String, Object> raw, Object register,
            Map<String, Object> bindings) {
        if (raw == null || raw.size() != 1) {
            throw new IllegalStateException(
                    "step '" + step.function + "' arg[" + argIndex + "] must be a single-key map, got " + raw);
        }
        Map.Entry<String, Object> entry = raw.entrySet().iterator().next();
        String key = entry.getKey();
        Object value = entry.getValue();
        switch (key) {
            case "reg":
                return register;
            case "ref": {
                String name = String.valueOf(value);
                if (!bindings.containsKey(name)) {
                    throw new IllegalStateException(
                            "step '" + step.function + "' arg[" + argIndex + "] refers to unbound name '" + name + "'");
                }
                return bindings.get(name);
            }
            case "json": {
                try {
                    if (value instanceof String s) {
                        return MAPPER.readTree(s);
                    }
                    return MAPPER.valueToTree(value);
                } catch (Exception e) {
                    throw new IllegalStateException(
                            "step '" + step.function + "' arg[" + argIndex + "] is not valid JSON: " + value, e);
                }
            }
            case "val":
                return decodeNode(step, argIndex, value, register, bindings);
            default:
                throw new IllegalStateException(
                        "step '" + step.function + "' arg[" + argIndex + "] has unknown key '" + key
                                + "' (expected one of reg, ref, json, val)");
        }
    }

    /**
     * Decodes one node inside a {@code "val"} wire value: a nested single-key tag map
     * (e.g. a {@code {"ref": name}} inside a list -- docs/INTEGRATION-TESTS.md's
     * {@code JSONBuildArrayFromList} worked example) dispatches through the SAME
     * {@link #decodeArg} switch a top-level arg would; a list decodes recursively
     * element-wise into an {@code ArrayList} (feeds e.g. {@code
     * JSONBuildArrayFromList(List)}); any other value passes through unchanged.
     */
    @SuppressWarnings("unchecked")
    private Object decodeNode(UdfSpec.Step step, int argIndex, Object node, Object register, Map<String, Object> bindings) {
        if (node instanceof Map<?, ?> tag) {
            return decodeArg(step, argIndex, (Map<String, Object>) tag, register, bindings);
        }
        if (node instanceof List<?> list) {
            List<Object> out = new ArrayList<>(list.size());
            for (Object element : list) {
                out.add(decodeNode(step, argIndex, element, register, bindings));
            }
            return out;
        }
        return node;
    }

    // ------------------------------------------------------------------
    // Invocation + method resolution (with per-step caching)
    // ------------------------------------------------------------------

    private Object invoke(int stepIndex, UdfSpec.Step step, Object[] args) throws Exception {
        Class<?>[] signature = runtimeSignature(args);
        ResolvedCall call = cache[stepIndex];
        if (call == null || !Arrays.equals(cacheSignature[stepIndex], signature)) {
            call = resolveMethod(udfClass, step.function, args);
            cache[stepIndex] = call;
            cacheSignature[stepIndex] = signature;
        }
        Object[] invokeArgs = call.varargs ? pack(call, args) : args;
        try {
            return call.method.invoke(null, invokeArgs);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            throw new IllegalStateException(className + "." + step.function + " threw for step " + stepIndex, cause);
        }
    }

    private static Class<?>[] runtimeSignature(Object[] args) {
        Class<?>[] sig = new Class<?>[args.length];
        for (int i = 0; i < args.length; i++) {
            sig[i] = args[i] == null ? Void.class : args[i].getClass();
        }
        return sig;
    }

    private static Object[] pack(ResolvedCall call, Object[] args) {
        int fixed = call.fixedCount;
        Object packed = Array.newInstance(call.varargComponent, args.length - fixed);
        for (int i = fixed; i < args.length; i++) {
            Array.set(packed, i - fixed, args[i]);
        }
        Object[] invokeArgs = new Object[fixed + 1];
        System.arraycopy(args, 0, invokeArgs, 0, fixed);
        invokeArgs[fixed] = packed;
        return invokeArgs;
    }

    /** A resolved static-method call: the {@link Method} plus how (if at all) to pack a trailing varargs array. */
    static final class ResolvedCall {
        final Method method;
        final boolean varargs;
        final Class<?> varargComponent;
        final int fixedCount;

        ResolvedCall(Method method, boolean varargs, Class<?> varargComponent, int fixedCount) {
            this.method = method;
            this.varargs = varargs;
            this.varargComponent = varargComponent;
            this.fixedCount = fixedCount;
        }
    }

    /**
     * Resolves {@code name} against {@code udfClass}'s public static methods for the
     * given (already-decoded) {@code args}: Phase A tries every exact-arity non-varargs
     * overload; only if none match does Phase B try varargs overloads (packing the tail
     * into a fresh array of the declared component type). Ties within a phase are broken
     * by {@link #pickMostSpecific}; an unresolved tie or zero matches in both phases is an
     * {@link IllegalStateException} naming the class, method, decoded argument runtime
     * types, and every same-named candidate signature. Package-private + static so it is
     * unit-testable without a jar or a classloader.
     *
     * <p>
     * <b>Known divergences from javac's real overload resolution (JLS §15.12.2),
     * neither reachable by the overload sets of the UDFs this harness was checked against,
     * both verified by hand-tracing every real overload in those classes before this note
     * was written):</b>
     * <ul>
     * <li>Phase A merges javac's phase 1 (strict, no boxing) and phase 2 (with boxing,
     * no varargs) into one pass. A class with e.g. both {@code f(long)} and
     * {@code f(Integer)} called with an {@code Integer} arg would resolve unambiguously
     * to {@code f(Integer)} under real overload resolution; here both are Phase A
     * candidates, {@link #noWorseThan} finds neither more specific (their param types
     * are unrelated), and {@link #pickMostSpecific} throws an ambiguity error a real
     * caller would not hit.</li>
     * <li>{@link #noWorseThan} returns {@code false} outright for two varargs
     * candidates of different arity (line ~{@code aTypes.length != bTypes.length}), so
     * two varargs overloads of the same name with different fixed-argument counts
     * always tie in Phase B rather than the more specific one winning.</li>
     * </ul>
     * Widening either gap is a real (if currently unneeded) improvement for a future
     * UDF with a richer overload set -- flagged here rather than fixed speculatively.
     */
    static ResolvedCall resolveMethod(Class<?> udfClass, String name, Object[] args) {
        List<Method> candidates = new ArrayList<>();
        for (Method m : udfClass.getMethods()) {
            if (Modifier.isStatic(m.getModifiers()) && m.getName().equals(name)) {
                candidates.add(m);
            }
        }
        if (candidates.isEmpty()) {
            List<String> declared = new ArrayList<>();
            for (Method m : udfClass.getMethods()) {
                if (Modifier.isStatic(m.getModifiers())) {
                    declared.add(m.getName());
                }
            }
            throw new IllegalStateException(
                    "no static method named '" + name + "' on " + udfClass.getName() + " (declares: " + declared + ")");
        }

        int n = args.length;

        List<Method> exact = new ArrayList<>();
        for (Method m : candidates) {
            if (!m.isVarArgs() && m.getParameterCount() == n && acceptsAll(m.getParameterTypes(), args, n)) {
                exact.add(m);
            }
        }
        Method chosen = pickMostSpecific(exact, name, args, udfClass);
        if (chosen != null) {
            return new ResolvedCall(chosen, false, null, n);
        }

        List<Method> varargCandidates = new ArrayList<>();
        for (Method m : candidates) {
            if (!m.isVarArgs()) {
                continue;
            }
            Class<?>[] paramTypes = m.getParameterTypes();
            int fixed = paramTypes.length - 1;
            if (n < fixed || !acceptsAll(paramTypes, args, fixed)) {
                continue;
            }
            Class<?> componentType = paramTypes[fixed].getComponentType();
            boolean ok = true;
            for (int i = fixed; i < n; i++) {
                if (!accepts(componentType, args[i])) {
                    ok = false;
                    break;
                }
            }
            if (ok) {
                varargCandidates.add(m);
            }
        }
        chosen = pickMostSpecific(varargCandidates, name, args, udfClass);
        if (chosen != null) {
            Class<?> componentType = chosen.getParameterTypes()[chosen.getParameterCount() - 1].getComponentType();
            return new ResolvedCall(chosen, true, componentType, chosen.getParameterCount() - 1);
        }

        List<String> signatures = new ArrayList<>();
        for (Method m : candidates) {
            signatures.add(m.toString());
        }
        List<String> argTypes = new ArrayList<>();
        for (Object a : args) {
            argTypes.add(a == null ? "null" : a.getClass().getName());
        }
        throw new IllegalStateException(
                "no overload of '" + name + "' on " + udfClass.getName() + " accepts args " + argTypes
                        + " (candidates: " + signatures + ")");
    }

    private static boolean acceptsAll(Class<?>[] paramTypes, Object[] args, int count) {
        for (int i = 0; i < count; i++) {
            if (!accepts(paramTypes[i], args[i])) {
                return false;
            }
        }
        return true;
    }

    /** Exactly one candidate no-worse-than every other wins; more than one is an ambiguity error; zero candidates returns {@code null}. */
    private static Method pickMostSpecific(List<Method> candidates, String name, Object[] args, Class<?> udfClass) {
        if (candidates.isEmpty()) {
            return null;
        }
        if (candidates.size() == 1) {
            return candidates.get(0);
        }
        Method best = null;
        for (Method candidate : candidates) {
            boolean noWorseThanAll = true;
            for (Method other : candidates) {
                if (other != candidate && !noWorseThan(candidate, other)) {
                    noWorseThanAll = false;
                    break;
                }
            }
            if (noWorseThanAll) {
                if (best != null) {
                    throw ambiguity(name, args, udfClass, candidates);
                }
                best = candidate;
            }
        }
        if (best == null) {
            throw ambiguity(name, args, udfClass, candidates);
        }
        return best;
    }

    private static IllegalStateException ambiguity(String name, Object[] args, Class<?> udfClass, List<Method> candidates) {
        return new IllegalStateException(
                "ambiguous overload of '" + name + "' on " + udfClass.getName() + " for args " + Arrays.toString(args)
                        + " (candidates: " + candidates + ")");
    }

    /** {@code a} is no worse than {@code b} when every one of {@code a}'s param types is assignable to {@code b}'s (JLS §15.12.2.5, in miniature). */
    private static boolean noWorseThan(Method a, Method b) {
        Class<?>[] aTypes = a.getParameterTypes();
        Class<?>[] bTypes = b.getParameterTypes();
        if (aTypes.length != bTypes.length) {
            return false;
        }
        for (int i = 0; i < aTypes.length; i++) {
            if (!box(bTypes[i]).isAssignableFrom(box(aTypes[i]))) {
                return false;
            }
        }
        return true;
    }

    private static Class<?> box(Class<?> c) {
        if (!c.isPrimitive()) return c;
        if (c == boolean.class) return Boolean.class;
        if (c == char.class) return Character.class;
        if (c == byte.class) return Byte.class;
        if (c == short.class) return Short.class;
        if (c == int.class) return Integer.class;
        if (c == long.class) return Long.class;
        if (c == float.class) return Float.class;
        return Double.class;
    }

    /** Whether {@code arg} is a legal argument for a parameter of type {@code param} (JLS-style widening for primitives; {@code isInstance} otherwise). */
    static boolean accepts(Class<?> param, Object arg) {
        if (arg == null) {
            return !param.isPrimitive();
        }
        if (!param.isPrimitive()) {
            return param.isInstance(arg);
        }
        Class<?> argClass = arg.getClass();
        if (param == boolean.class) return argClass == Boolean.class;
        if (param == char.class) return argClass == Character.class;
        if (param == byte.class) return argClass == Byte.class;
        if (param == short.class) return argClass == Byte.class || argClass == Short.class;
        if (param == int.class) return argClass == Byte.class || argClass == Short.class || argClass == Character.class || argClass == Integer.class;
        if (param == long.class) return argClass == Byte.class || argClass == Short.class || argClass == Character.class || argClass == Integer.class || argClass == Long.class;
        if (param == float.class) return argClass == Byte.class || argClass == Short.class || argClass == Character.class || argClass == Integer.class || argClass == Long.class || argClass == Float.class;
        if (param == double.class) return argClass == Byte.class || argClass == Short.class || argClass == Character.class || argClass == Integer.class || argClass == Long.class || argClass == Float.class || argClass == Double.class;
        return false;
    }

    // ------------------------------------------------------------------
    // jsonnode envelope slot grammar: data[N] / before[N] / userdata.KEY
    // ------------------------------------------------------------------

    private static Object readSlot(WAEvent event, String slot) {
        Matcher m = SLOT_RE.matcher(slot);
        if (!m.matches()) {
            throw new IllegalStateException("invalid slot: " + slot);
        }
        if (slot.startsWith("data[")) {
            int idx = Integer.parseInt(m.group(1));
            return (event.data != null && idx < event.data.length) ? event.data[idx] : null;
        }
        if (slot.startsWith("before[")) {
            int idx = Integer.parseInt(m.group(1));
            return (event.before != null && idx < event.before.length) ? event.before[idx] : null;
        }
        return event.userdata == null ? null : event.userdata.get(m.group(2));
    }

    private static void writeSlot(WAEvent event, String slot, Object value) {
        Matcher m = SLOT_RE.matcher(slot);
        if (!m.matches()) {
            throw new IllegalStateException("invalid slot: " + slot);
        }
        if (slot.startsWith("data[")) {
            event.setData(Integer.parseInt(m.group(1)), value);
        } else if (slot.startsWith("before[")) {
            event.setBefore(Integer.parseInt(m.group(1)), value);
        } else {
            event.putUserdata(m.group(2), value);
        }
    }
}
