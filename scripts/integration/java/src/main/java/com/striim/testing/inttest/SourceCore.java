package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;

/**
 * Drives a READER core — one with no input event at all.
 *
 * <p><b>Why the existing driver cannot.</b> {@link OperatorCore} resolves
 * {@code processEvent(WAEvent)}, the contract every in-stream op satisfies. A reader has nothing
 * to feed: it PULLS from its source and PUSHES into a channel, so its core method is
 * {@code tick(<channel>)}. That single difference is why all five readers sit at
 * {@code integration: null} while every in-stream op has a tier — not because nobody wrote the
 * cases.</p>
 *
 * <p><b>The channel is proxied by name, and it has to be.</b> The op jar is loaded in a child
 * classloader, and each OP also SHADES its shared classes into a module-unique
 * package. So the harness cannot hold a compile-time reference to the op's channel interface —
 * there is no single type to import. The interface is taken from the child-loaded core's own
 * {@code tick} signature and implemented with a {@link Proxy}, exactly as
 * {@code IntegrationProcessor.newTypeResolverProxy} already does for {@code TypeResolver}.</p>
 *
 * <p><b>Termination is a tick COUNT, not a clock.</b> D-D′ worried that a reader tier would need
 * "a predicate over time" inside a tier whose determinism story is "N events in, M events out",
 * and named that the flake risk that could sink the whole idea. It is designed out rather than
 * tolerated: one {@link #processEvent} call is one {@code tick()}, so the harness's existing
 * drive loop supplies exactly as many ticks as the case declares, and a scripted source emits a
 * fixed script. Nothing waits on wall-clock quiescence, so nothing can flake on a slow machine.
 * The event handed to {@code processEvent} is ignored — it is a tick trigger, not input.</p>
 */
final class SourceCore implements EventDriver {

    private final Object instance;
    private final Method tick;
    private final Method close;
    private final Object channel;
    private final List<WAEvent> emitted;

    private SourceCore(Object instance, Method tick, Method close, Object channel,
            List<WAEvent> emitted) {
        this.instance = instance;
        this.tick = tick;
        this.close = close;
        this.channel = channel;
        this.emitted = emitted;
    }

    /**
     * Builds a reader core from an already-loaded class.
     *
     * @param coreClass    the op's {@code Processor}, loaded from the op jar's child loader
     * @param ctorArgs     constructor arguments, already resolved by type
     * @param channelIface the op's own channel interface, from the SAME loader as {@code coreClass}
     */
    static SourceCore build(Class<?> coreClass, Object[] ctorArgs, Class<?> channelIface)
            throws Exception {
        Constructor<?> ctor = IntegrationProcessor.solelyConstructorOf(coreClass);
        // solelyConstructorOf returns getDeclaredConstructors()[0], which is routinely
        // package-private -- OperatorCore does the same before its newInstance.
        ctor.setAccessible(true);
        Object instance;
        try {
            instance = ctor.newInstance(ctorArgs);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException("Constructing " + coreClass.getName() + " threw",
                    e.getCause() != null ? e.getCause() : e);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to construct " + coreClass.getName()
                    + " via its sole constructor", e);
        }

        // No jar, no properties: this overload is the unit-test entry point, so there is no
        // IntegrationSeams to delegate to. An unknown channel method still throws, as before.
        return of(instance, coreClass, channelIface, null);
    }

    /**
     * Builds a reader core from an op JAR, the entry point a {@code source:} case drives
     * Shares {@link LoadedOp} with {@link OperatorCore}, so a reader is constructed,
     * seam-injected and {@code start()}ed exactly as an in-stream op is; only what happens
     * afterward differs.
     */
    static SourceCore build(File opJar, Map<String, Object> properties, Map<String, Object> types,
            List<String> passwordProperties, String namespace, String sourceName) throws Exception {
        // No typeStampingInput: a reader has no input events, so the UUID-stamping half of the
        // `types:` linkage is inert here. The block still matters -- a reader resolves its target
        // types BY NAME through the TypeResolver seam, which `types:` is what populates.
        LoadedOp loaded = LoadedOp.load(opJar, properties, types, List.of(), passwordProperties,
                namespace, sourceName);
        Class<?> channelIface = discoverChannel(loaded.coreClass, loaded.coreClassName);
        return of(loaded.instance, loaded.coreClass, channelIface,
                channelSeamsFor(loaded.coreClassName, channelIface, properties,
                        loaded.coreClass.getClassLoader()));
    }

    /**
     * Delegation to the op's {@code IntegrationSeams} for any channel method beyond
     * {@code emit}/{@code checkpoint}. {@code null} when there is no op jar to ask.
     */
    private static ChannelSeams channelSeamsFor(String coreClassName, Class<?> channelIface,
            Map<String, Object> properties, ClassLoader child) {
        int lastDot = coreClassName.lastIndexOf('.');
        String corePackage = (lastDot < 0) ? "" : coreClassName.substring(0, lastDot);
        return (methodName, returnType) -> IntegrationSeamsLookup.channelValueFor(
                corePackage, channelIface.getName(), methodName, returnType, properties, child);
    }

    /** What answers a channel method the harness itself does not implement. */
    interface ChannelSeams {
        Object valueFor(String methodName, Class<?> returnType);
    }

    /**
     * Package-private rather than private so a test can drive the channel proxy directly.
     *
     * <p>A review severed the proxy's delegation branch (`return null` instead of asking the op)
     * and every harness test stayed green: {@code ChannelSeamDelegationTest} was covering the
     * LOOKUP thoroughly and the thing that CALLS it not at all. Widening one seam is the price of
     * testing the wire rather than its ends.</p>
     */
    static SourceCore of(Object instance, Class<?> coreClass, Class<?> channelIface,
            ChannelSeams channelSeams) {
        List<WAEvent> emitted = new ArrayList<WAEvent>();
        Object channel = newChannelProxy(channelIface, emitted, channelSeams);

        Method tick = findTick(coreClass, channelIface);
        Method close = optionalNoArg(coreClass, "close");
        return new SourceCore(instance, tick, close, channel, emitted);
    }

    /**
     * Finds the core's channel type by finding its {@code tick}.
     *
     * <p>Bridge and synthetic methods are skipped — see the loop.</p>
     *
     * <p><b>Why this is discovered rather than named in the YAML.</b> Each OP
     * shades its shared classes into a module-unique package, and every version bump MOVES that
     * package — so a channel FQCN written into a {@code test.yaml} would be invalidated by the
     * bump that G1 requires each module to take last. The tick method carries the same
     * information and cannot drift from the code.</p>
     */
    private static Class<?> discoverChannel(Class<?> coreClass, String coreClassName) {
        Class<?> found = null;
        int candidates = 0;
        StringBuilder seen = new StringBuilder();
        for (Method m : coreClass.getMethods()) {
            if (!"tick".equals(m.getName())) {
                continue;
            }
            // A core inheriting tick(C) from a generic supertype -- the shape the shared reader
            // contracts push toward -- carries a compiler-generated bridge tick(<erasure of C>)
            // alongside the real one, and BOTH take an interface. Counting the bridge rejects an
            // ordinary reader as ambiguous; invoking it would work but is not the declared method.
            if (m.isBridge() || m.isSynthetic()) {
                continue;
            }
            candidates++;
            seen.append(seen.length() == 0 ? "" : ", ").append(m);
            if (m.getParameterCount() == 1 && m.getParameterTypes()[0].isInterface()) {
                if (found != null) {
                    throw new IllegalStateException(coreClassName + " declares more than one"
                            + " tick(<interface>) method, so this harness cannot tell which one"
                            + " drives the reader: " + seen);
                }
                found = m.getParameterTypes()[0];
            }
        }
        if (found == null) {
            throw new IllegalStateException(coreClassName + " has no tick(<channel interface>)"
                    + " method — a reader driven by a `source:` case must expose one, which is the"
                    + " reader counterpart of processEvent(WAEvent). The channel must be an"
                    + " INTERFACE: the harness supplies it as a Proxy so it can record what the"
                    + " core emits."
                    + (candidates == 0 ? "" : " Methods named tick that do not fit: " + seen));
        }
        return found;
    }

    /**
     * One tick. The argument is ignored: a reader consumes nothing, and the harness's loop calls
     * this once per declared tick.
     */
    @Override
    public List<WAEvent> processEvent(Event ignoredTickTrigger) throws Exception {
        emitted.clear();
        try {
            tick.invoke(instance, channel);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw new IllegalStateException("tick() threw", cause);
        }
        return new ArrayList<WAEvent>(emitted);
    }

    @Override
    public void close() throws Exception {
        if (close == null) {
            return;
        }
        try {
            close.invoke(instance);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() != null ? e.getCause() : e;
            if (cause instanceof Exception) {
                throw (Exception) cause;
            }
            throw new IllegalStateException("close() threw", cause);
        }
    }

    /**
     * Records what the core emits and accepts its checkpoints.
     *
     * <p>{@code checkpoint} is deliberately a no-op that RETURNS rather than throwing: a reader
     * checkpoints on its own schedule, and a harness that rejected the call would change the
     * behaviour it is meant to observe. What the position contained is not asserted here — that
     * is recovery's business, and recovery is not expressible in a tier with no restart.</p>
     */
    private static Object newChannelProxy(Class<?> channelIface, List<WAEvent> emitted,
            ChannelSeams channelSeams) {
        InvocationHandler handler = (proxy, method, args) -> {
            switch (method.getName()) {
                case "emitAt":
                    // Positioned emission (recovery design r2): collect args[0] exactly like
                    // emit; the coordinate in args[1] is deliberately discarded — this tier has
                    // no restart, so position persistence is not observable here.
                case "emit": {
                    // emit(WAEvent) and emit(WAEvent, int channel) both land here; a trailing
                    // argument that is not the element is ignored on purpose. No module declares
                    // the two-arg form today -- MetricsReader did before moving its channel
                    // onto its App -- and the tolerance stays because it belongs to this proxy
                    // rather than to any one consumer. SourceCoreTest pins it.
                    Object element = args == null || args.length == 0 ? null : args[0];
                    if (element instanceof WAEvent) {
                        emitted.add((WAEvent) element);
                        return null;
                    }
                    // Dropping a non-WAEvent silently would let a case assert "0 events" and pass
                    // for the wrong reason -- and this is reachable, not theoretical: the event-emission
                    // contracts alongside this admit Emitter<Event> (an exception-store reader's shape)
                    // and name JsonNodeEvent as a future element type.
                    throw new IllegalStateException("this harness collects WAEvent only, but "
                            + channelIface.getName() + ".emit received "
                            + (element == null ? "null" : element.getClass().getName())
                            + "; teach SourceCore that element type rather than losing the event");
                }
                case "checkpoint":
                    return null;
                case "toString":
                    return "SourceCore.channel(" + channelIface.getName() + ")";
                case "hashCode":
                    return System.identityHashCode(proxy);
                case "equals":
                    return proxy == (args == null ? null : args[0]);
                default:
                    // ⚠ This branch used to throw, asserting that "a reader core should only emit
                    // and checkpoint through its channel". That was false for four of the five
                    // readers and blocked a change-data reader's tier entirely: its tick() resolves a
                    // checkpoint file from checkpointDir()/appQualifiedName()/componentQualifiedName()
                    // on its FIRST statement. The first reader with a tier hid the gap by reaching the same
                    // platform state through a CONSTRUCTOR seam, which IntegrationSeams already
                    // covered -- so the pathfinder was not representative.
                    //
                    // The op answers, not the harness: a fabricated default here (an empty app
                    // name, a temp dir) is exactly how a reader's checkpoint identity silently
                    // degrades to a node-wide shared file.
                    if (channelSeams == null) {
                        throw new UnsupportedOperationException(
                                channelIface.getName() + "." + method.getName()
                                        + " needs an answer, and this SourceCore was built without"
                                        + " an op jar to ask (the unit-test entry point). Use the"
                                        + " jar-loading build() so the op's IntegrationSeams can"
                                        + " answer it.");
                    }
                    return channelSeams.valueFor(method.getName(), method.getReturnType());
            }
        };
        return Proxy.newProxyInstance(channelIface.getClassLoader(),
                new Class<?>[] { channelIface }, handler);
    }

    /** The single {@code tick}-shaped method taking the channel. */
    private static Method findTick(Class<?> coreClass, Class<?> channelIface) {
        try {
            Method m = coreClass.getMethod("tick", channelIface);
            m.setAccessible(true);
            return m;
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(coreClass.getName() + " has no tick("
                    + channelIface.getSimpleName() + ") — a reader core driven by this harness must"
                    + " expose one, which is the reader counterpart of processEvent(WAEvent)", e);
        }
    }

    private static Method optionalNoArg(Class<?> coreClass, String name) {
        try {
            Method m = coreClass.getMethod(name);
            m.setAccessible(true);
            return m;
        } catch (NoSuchMethodException e) {
            return null;
        }
    }

    /** What the last tick emitted, for a caller driving this directly rather than through a loop. */
    List<WAEvent> lastEmitted() {
        return Collections.unmodifiableList(emitted);
    }
}
