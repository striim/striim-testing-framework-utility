package com.striim.testing.inttest;

import java.io.File;
import java.lang.management.ManagementFactory;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.Enumeration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.function.Supplier;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.management.MBeanAttributeInfo;
import javax.management.MBeanServer;
import javax.management.ObjectName;

/**
 * Builds a driven op's MBean, registers it under the name production would give it, and reads
 * back every attribute — the Java half of {@code assert.jmx:}.
 *
 * <p><b>The runner builds the bean itself.</b> The harness never constructs the op's {@code App}
 * shell, so the production hook ({@code AbstractConvertingOpenProcessorApp.getJmxMBean()}) never
 * runs. What this does instead:</p>
 * <ol>
 * <li>Find the bean class: the case's {@code bean:} if named, otherwise the single public, concrete,
 * top-level class in the core's own package that implements an {@code *MXBean}/{@code *MBean}
 * interface and has a public constructor whose first parameter accepts the core and whose other
 * parameters are all {@link Supplier}s. None, or more than one, is an error naming the
 * candidates.</li>
 * <li>Construct it with the core and {@code () -> null} for every {@code Supplier}. Such a
 * supplier stands for App-owned state (an OP's gate), which does not exist here.</li>
 * <li>Register it through the op jar's own (shaded) {@code OpJmxRegistry} when the jar carries
 * one, so the name and the compliance check are production's; otherwise directly, in the same
 * {@code com.example:type=<Type>,name="<namespace.sourceName>"} format.</li>
 * <li>Read every attribute through the {@link MBeanServer}, then unregister.</li>
 * </ol>
 *
 * <p><b>Never throws and never returns an empty success.</b> Any failure is returned as
 * {@code {"error": ...}}, which the Python side fails on, so a bean that could not be built can
 * never let an assertion pass vacuously.</p>
 */
final class JmxSnapshot {

    static final String DOMAIN = "com.example";

    /**
     * Mirrors {@code OpJmxRegistry.VERSIONED}, both schemes: {@code FooOpV8_7} and the older
     * {@code FooOpV8F} give {@code FooOp}.
     */
    private static final Pattern VERSIONED = Pattern.compile("^(.+?)V\\d+(?:_\\d+|[A-Z]*)$");

    private static final String REGISTRY_SIMPLE_NAME = ".OpJmxRegistry";

    private JmxSnapshot() {
    }

    static Map<String, Object> capture(File opJar, Object core, String explicitBean, String namespace,
            String sourceName) {
        try {
            return captureOrThrow(opJar, core, explicitBean, namespace, sourceName);
        } catch (Throwable t) {
            return error("JMX snapshot failed: " + t);
        }
    }

    private static Map<String, Object> captureOrThrow(File opJar, Object core, String explicitBean,
            String namespace, String sourceName) throws Exception {
        Class<?> coreClass = core.getClass();
        ClassLoader loader = coreClass.getClassLoader();
        String corePackage = coreClass.getPackageName();
        List<String> classNames = classNamesIn(opJar);

        Class<?> beanClass;
        if (explicitBean != null && !explicitBean.isBlank()) {
            String fqcn = explicitBean.contains(".") ? explicitBean : corePackage + "." + explicitBean;
            Class<?> named;
            try {
                named = Class.forName(fqcn, false, loader);
            } catch (ClassNotFoundException e) {
                return error("assert.jmx.bean " + explicitBean + ": class " + fqcn + " is not in " + opJar);
            }
            String why = ineligibility(named, coreClass);
            if (why != null) {
                return error("assert.jmx.bean " + fqcn + " cannot be built by the harness: " + why);
            }
            beanClass = named;
        } else {
            List<Class<?>> candidates = new ArrayList<>();
            for (String name : classNames) {
                if (!name.startsWith(corePackage + ".") || name.indexOf('.', corePackage.length() + 1) >= 0
                        || name.indexOf('$') >= 0) {
                    continue;
                }
                Class<?> c;
                try {
                    c = Class.forName(name, false, loader);
                } catch (Throwable unloadable) {
                    continue;
                }
                if (ineligibility(c, coreClass) == null) {
                    candidates.add(c);
                }
            }
            if (candidates.isEmpty()) {
                return error("no MBean class found in " + corePackage + ": expected a public class"
                        + " implementing an *MXBean or *MBean interface with a public constructor taking "
                        + coreClass.getSimpleName() + " (plus optional Supplier parameters). Name one"
                        + " with assert.jmx.bean if it lives elsewhere.");
            }
            if (candidates.size() > 1) {
                List<String> names = new ArrayList<>();
                candidates.forEach(c -> names.add(c.getName()));
                return error("more than one MBean class in " + corePackage + " " + names
                        + "; name the one to assert with assert.jmx.bean");
            }
            beanClass = candidates.get(0);
        }

        Object bean = construct(beanClass, coreClass, core);
        String component = (namespace != null ? namespace : "inttest") + "."
                + (sourceName != null ? sourceName : "source");

        Class<?> registry = registryClass(classNames, loader);
        ObjectName name;
        String registeredVia;
        if (registry != null) {
            String type = (String) registry.getMethod("typeFor", Class.class).invoke(null, coreClass);
            Method register = methodNamed(registry, "register", 4);
            name = (ObjectName) register.invoke(null, bean, type, component, null);
            registeredVia = registry.getName();
            if (name == null) {
                return error(registry.getName() + ".register refused " + beanClass.getName()
                        + " (it logs why only through a Logger, which the harness does not pass)");
            }
        } else {
            name = new ObjectName(DOMAIN + ":type=" + typeFor(coreClass) + ",name=" + ObjectName.quote(component));
            MBeanServer server = ManagementFactory.getPlatformMBeanServer();
            if (server.isRegistered(name)) {
                server.unregisterMBean(name);
            }
            server.registerMBean(bean, name);
            registeredVia = "MBeanServer";
        }

        try {
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("objectName", name.toString());
            result.put("beanClass", beanClass.getName());
            result.put("registeredVia", registeredVia);
            readAttributes(name, result);
            return result;
        } finally {
            if (registry != null) {
                methodNamed(registry, "unregister", 3).invoke(null, name, bean, null);
            } else {
                ManagementFactory.getPlatformMBeanServer().unregisterMBean(name);
            }
        }
    }

    /** Every readable attribute, as JSON primitives; a getter that throws is reported by name. */
    private static void readAttributes(ObjectName name, Map<String, Object> result) throws Exception {
        MBeanServer server = ManagementFactory.getPlatformMBeanServer();
        Map<String, Object> attributes = new TreeMap<>();
        Map<String, Object> errors = new TreeMap<>();
        for (MBeanAttributeInfo info : server.getMBeanInfo(name).getAttributes()) {
            if (!info.isReadable()) {
                continue;
            }
            try {
                Object value = server.getAttribute(name, info.getName());
                attributes.put(info.getName(), value == null || value instanceof Number
                        || value instanceof Boolean || value instanceof String ? value : String.valueOf(value));
            } catch (Exception e) {
                Throwable cause = e.getCause() != null ? e.getCause() : e;
                errors.put(info.getName(), String.valueOf(cause));
            }
        }
        result.put("attributes", attributes);
        if (!errors.isEmpty()) {
            result.put("attributeErrors", errors);
        }
    }

    /** Null when {@code c} can be built by {@link #construct}; otherwise why not. */
    static String ineligibility(Class<?> c, Class<?> coreClass) {
        if (c.isInterface() || Modifier.isAbstract(c.getModifiers()) || !Modifier.isPublic(c.getModifiers())) {
            return "not a public concrete class";
        }
        if (!implementsMBeanInterface(c)) {
            return "implements no *MXBean or *MBean interface";
        }
        return beanConstructor(c, coreClass) == null
                ? "no public constructor taking " + coreClass.getName() + " followed only by Supplier parameters"
                : null;
    }

    private static boolean implementsMBeanInterface(Class<?> c) {
        Deque<Class<?>> todo = new ArrayDeque<>();
        for (Class<?> k = c; k != null; k = k.getSuperclass()) {
            todo.addAll(List.of(k.getInterfaces()));
        }
        while (!todo.isEmpty()) {
            Class<?> i = todo.pop();
            if (i.getSimpleName().endsWith("MXBean") || i.getSimpleName().endsWith("MBean")) {
                return true;
            }
            todo.addAll(List.of(i.getInterfaces()));
        }
        return false;
    }

    private static Constructor<?> beanConstructor(Class<?> c, Class<?> coreClass) {
        for (Constructor<?> ctor : c.getConstructors()) {
            Class<?>[] params = ctor.getParameterTypes();
            if (params.length == 0 || !params[0].isAssignableFrom(coreClass)) {
                continue;
            }
            boolean restAreSuppliers = true;
            for (int i = 1; i < params.length; i++) {
                restAreSuppliers &= params[i] == Supplier.class;
            }
            if (restAreSuppliers) {
                return ctor;
            }
        }
        return null;
    }

    private static Object construct(Class<?> beanClass, Class<?> coreClass, Object core) throws Exception {
        Constructor<?> ctor = beanConstructor(beanClass, coreClass);
        Object[] args = new Object[ctor.getParameterCount()];
        args[0] = core;
        for (int i = 1; i < args.length; i++) {
            args[i] = (Supplier<Object>) () -> null;
        }
        return ctor.newInstance(args);
    }

    /** The jar's OpJmxRegistry (normally relocated under com.example.shaded), or null. */
    private static Class<?> registryClass(List<String> classNames, ClassLoader loader) {
        for (String name : classNames) {
            if (name.endsWith(REGISTRY_SIMPLE_NAME) && name.startsWith("com.example.")) {
                try {
                    return Class.forName(name, true, loader);
                } catch (Throwable t) {
                    return null;
                }
            }
        }
        return null;
    }

    private static Method methodNamed(Class<?> c, String name, int arity) throws NoSuchMethodException {
        for (Method m : c.getMethods()) {
            if (m.getName().equals(name) && m.getParameterCount() == arity && Modifier.isStatic(m.getModifiers())) {
                return m;
            }
        }
        throw new NoSuchMethodException(c.getName() + "." + name + " with " + arity + " parameters");
    }

    static String typeFor(Class<?> coreClass) {
        String pkg = coreClass.getPackageName();
        String segment = pkg.substring(pkg.lastIndexOf('.') + 1);
        if (segment.isEmpty()) {
            return coreClass.getSimpleName();
        }
        return unversioned(segment);
    }

    /** {@code segment} without its trailing version; unchanged when it has none. */
    static String unversioned(String segment) {
        Matcher m = VERSIONED.matcher(segment);
        return m.matches() ? m.group(1) : segment;
    }

    private static List<String> classNamesIn(File opJar) throws Exception {
        List<String> names = new ArrayList<>();
        try (JarFile jar = new JarFile(opJar)) {
            Enumeration<JarEntry> entries = jar.entries();
            while (entries.hasMoreElements()) {
                String entry = entries.nextElement().getName();
                if (entry.endsWith(".class") && !entry.startsWith("META-INF/")) {
                    names.add(entry.substring(0, entry.length() - ".class".length()).replace('/', '.'));
                }
            }
        }
        return names;
    }

    private static Map<String, Object> error(String message) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("error", message);
        return result;
    }
}
