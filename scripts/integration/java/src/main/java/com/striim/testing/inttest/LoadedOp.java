package com.striim.testing.inttest;

import java.io.File;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.URL;
import java.net.URLClassLoader;
import java.util.List;
import java.util.Map;

import com.webaction.event.Event;
import com.webaction.proc.events.WAEvent;
import com.webaction.uuid.UUID;

/**
 * "Load the jar, build the source-schema linkage, construct the core, {@code start()} it" —
 * everything an {@link EventDriver} needs before it has a core to drive, and nothing about how
 * the core is then driven.
 *
 * <p>Factored out when the reader driver arrived: {@link OperatorCore} resolves
 * {@code processEvent(WAEvent)} afterward and {@link SourceCore} resolves {@code tick(<channel>)},
 * but the sequence up to that point is identical — and a second hand-written copy of it is how the
 * two drivers would quietly diverge on classloading or on the {@code types:} linkage.</p>
 */
final class LoadedOp {

    /**
     * Optional harness entry an op may ship next to its core: {@code <corePackage>.IntegrationHarness}
     * with a {@code public static} {@value #HARNESS_ENTRY_METHOD} taking exactly the core
     * constructor's parameter types. When present the harness builds the core through it instead of
     * the constructor, so an op can enable its scalar test fixtures ONLY on this explicit call path.
     * A Striim server never calls it, and no configuration selects it.
     * Ops without one are constructed exactly as before.
     */
    static final String HARNESS_ENTRY_CLASS = "IntegrationHarness";
    static final String HARNESS_ENTRY_METHOD = "newProcessor";

    final String coreClassName;
    final Class<?> coreClass;

    /** The constructed, already-{@code start()}ed core instance. */
    final Object instance;

    private LoadedOp(String coreClassName, Class<?> coreClass, Object instance) {
        this.coreClassName = coreClassName;
        this.coreClass = coreClass;
        this.instance = instance;
    }

    /**
     * Loads {@code opJar} into a child classloader, constructs its {@code Processor}
     * (injecting {@code properties}/{@code Logger}/{@code BuiltInFuncs}/{@code TypeResolver} by
     * constructor parameter type), and invokes its optional {@code start()} lifecycle hook.
     * {@code typeStampingInput} is stamped with the {@code types:}-derived {@code typeUUID} in
     * place (docs/INTEGRATION-TESTS.md) before construction — for {@link IntegrationProcessor#drive} this is
     * the same list about to be looped over; for {@link PerformanceProcessor} it is the pre-parsed
     * template list, so every per-replay {@code WAEvent.makeCopy} inherits the stamp for free; for
     * a reader ({@link SourceCore}) it is empty, since a reader has no input to stamp.
     */
    static LoadedOp load(File opJar, Map<String, Object> properties, Map<String, Object> types,
            List<? extends Event> typeStampingInput, List<String> passwordProperties, String namespace,
            String sourceName) throws Exception {
        // Build the source-schema linkage from `types:` (docs/INTEGRATION-TESTS.md) BEFORE
        // constructing the core: mint one UUID per distinct source table, then
        // re-stamp every input event whose metadata.TableName names a declared
        // table so the mock BuiltInFuncs proxy (below) can resolve its columns by
        // that UUID exactly as the real BuiltInFunc/MDR path would by the source
        // event's real typeUUID. Lives in SourceTypes, shared with TargetCore, so the
        // two drivers cannot diverge on what a case's types: block means.
        SourceTypes sourceTypes = SourceTypes.from(types);
        sourceTypes.stamp(typeStampingInput);
        Map<UUID, List<String>> columnsByUuid = sourceTypes.columnsByUuid;
        Map<com.webaction.uuid.UUID, com.webaction.runtime.meta.MetaInfo.Type> declaredByUuid =
                sourceTypes.declaredByUuid;
        Map<String, com.webaction.runtime.meta.MetaInfo.Type> declaredByName =
                sourceTypes.declaredByName;

        URL opJarUrl = opJar.toURI().toURL();
        URLClassLoader child = new URLClassLoader(new URL[] { opJarUrl }, IntegrationProcessor.class.getClassLoader());

        String appClassName = IntegrationProcessor.readServiceImplementation(opJar);
        int lastDot = appClassName.lastIndexOf('.');
        if (lastDot < 0) {
            throw new IllegalStateException("Striim-Service-Implementation is not a qualified class name: " + appClassName);
        }
        String corePackage = appClassName.substring(0, lastDot);
        String coreClassName = corePackage + ".Processor";

        Class<?> coreClass;
        try {
            coreClass = Class.forName(coreClassName, true, child);
        } catch (ClassNotFoundException e) {
            throw new IllegalStateException(
                    "Core class not found: " + coreClassName + " (derived from manifest Striim-Service-Implementation=" + appClassName + " in " + opJar + ")",
                    e);
        }

        Constructor<?> ctor = IntegrationProcessor.solelyConstructorOf(coreClass);
        Class<?>[] paramTypes = ctor.getParameterTypes();
        Object[] args = new Object[paramTypes.length];
        for (int i = 0; i < paramTypes.length; i++) {
            args[i] = IntegrationProcessor.resolveConstructorArgument(coreClassName, paramTypes[i], properties, columnsByUuid, declaredByUuid, declaredByName, child, passwordProperties, namespace, sourceName);
        }

        Method harnessEntry = harnessEntry(corePackage, coreClass, paramTypes, child);
        ctor.setAccessible(true);
        Object instance;
        try {
            instance = harnessEntry != null ? harnessEntry.invoke(null, args) : ctor.newInstance(args);
        } catch (InvocationTargetException e) {
            throw new IllegalStateException("Constructing " + coreClassName + " threw", e.getCause() != null ? e.getCause() : e);
        } catch (Exception e) {
            throw new IllegalStateException("Failed to construct " + coreClassName + " via "
                    + (harnessEntry != null ? harnessEntry : ctor), e);
        }

        try {
            Method start = coreClass.getMethod("start");
            start.invoke(instance);
        } catch (NoSuchMethodException ignored) {
            // No start() lifecycle declared (or not overridden from the EventProcessor
            // default no-op); nothing to run before the event loop.
        } catch (InvocationTargetException e) {
            throw new IllegalStateException(coreClassName + ".start() threw", e.getCause() != null ? e.getCause() : e);
        }

        return new LoadedOp(coreClassName, coreClass, instance);
    }

    /**
     * The op's {@link #HARNESS_ENTRY_CLASS}.{@link #HARNESS_ENTRY_METHOD}, or {@code null} when the
     * jar ships no such class. A class that exists without a usable method is an authoring error
     * and is reported, rather than silently falling back to the constructor (which would make the
     * op refuse its fixtures with a less direct message).
     */
    static Method harnessEntry(String corePackage, Class<?> coreClass, Class<?>[] paramTypes,
            ClassLoader child) {
        String entryClassName = corePackage + "." + HARNESS_ENTRY_CLASS;
        Class<?> entryClass;
        try {
            entryClass = Class.forName(entryClassName, false, child);
        } catch (ClassNotFoundException absent) {
            return null;
        }
        Method method;
        try {
            method = entryClass.getMethod(HARNESS_ENTRY_METHOD, paramTypes);
        } catch (NoSuchMethodException e) {
            throw new IllegalStateException(entryClassName + " exists but has no public "
                    + HARNESS_ENTRY_METHOD + " with the core constructor's parameter types "
                    + java.util.Arrays.toString(paramTypes), e);
        }
        if (!java.lang.reflect.Modifier.isStatic(method.getModifiers())
                || !coreClass.isAssignableFrom(method.getReturnType())) {
            throw new IllegalStateException(entryClassName + "." + HARNESS_ENTRY_METHOD
                    + " must be static and return " + coreClass.getName());
        }
        return method;
    }

    /** The core's {@code close()} handle, or {@code null} when it declares no close lifecycle. */
    Method optionalClose() {
        try {
            return coreClass.getMethod("close");
        } catch (NoSuchMethodException e) {
            return null; // No close() lifecycle declared; nothing to release.
        }
    }
}
