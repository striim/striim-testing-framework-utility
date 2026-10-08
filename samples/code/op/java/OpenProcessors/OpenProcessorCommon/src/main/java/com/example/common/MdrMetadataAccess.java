package com.example.common;

import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

import com.webaction.metaRepository.MetadataRepository;
import com.webaction.metaRepository.MetadataRepositoryUtils;
import com.webaction.metaRepository.SecurityInfo;
import com.webaction.runtime.Context;
import com.webaction.runtime.components.EntityType;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.security.Password;
import com.webaction.utility.MonitorReportUtility;
import com.webaction.uuid.AuthToken;
import com.webaction.uuid.UUID;

/**
 * Production {@link MetadataAccess}: the real Metadata Repository, behind the contract.
 *
 * <p><b>Every platform type stops here.</b> {@code MDCache}, {@code MetadataRepository},
 * {@code MetaInfo}, {@code EntityType}, {@code SecurityInfo}, {@code Password}, {@code AuthToken}
 * and {@code Context} appear in this file and nowhere a module can see. That containment is the
 * seam's purpose: a caller depends on JDK types only, so a fake needs no {@code STRIIM_HOME} on the
 * test classpath.</p>
 *
 * <p>Lazy and cached — nothing touches {@code MDCache} until the first call, because the platform's
 * {@code MDCache} is not guaranteed initialized when a module is constructed. Every path catches
 * {@code Exception | LinkageError} and degrades: the MDR classes are {@code system}-scoped, so
 * outside a real Striim server they are absent and a missing class raises
 * {@code NoClassDefFoundError}, an {@code Error} that {@code catch (Exception)} does NOT catch. A
 * genuinely fatal {@code Error} — OOM, stack overflow — still propagates.</p>
 *
 * <p>Failures are logged ONCE per kind: an unreachable MDR is usually unreachable for the life of
 * the process, and a module consulting it per event would otherwise flood the log.</p>
 *
 * <p><b>Not optional.</b> An OP jar that references Striim's security manager is REFUSED at
 * {@code LOAD OPEN PROCESSOR} — "contains dependency on Striim's Security class". Reaching the
 * token through {@code MDCache} (see {@link SecurityTokenProvider}) is what makes MDR access legal
 * from a module at all.</p>
 */
public class MdrMetadataAccess implements MetadataAccess {

    private final Logger logger;
    private final SecurityTokenProvider tokens = new SecurityTokenProvider();

    private volatile Context context;
    private boolean authWarned;
    private volatile boolean contextWarned;
    private volatile boolean lookupWarned;

    public MdrMetadataAccess(Logger logger) {
        this.logger = logger;
    }

    // ------------------------------------------------------------------
    // Credentials — private on purpose. Nothing outside this class needs an
    // AuthToken or a Context; exposing them was what leaked the platform back
    // out to every adopter.
    // ------------------------------------------------------------------

    /**
     * The one place {@code MDCache} is reached, via the module's single token implementation.
     *
     * <p>{@link SecurityTokenProvider#token()} PROPAGATES on failure; this class catches. That is
     * the division of labour between them: one acquires, the other decides that failure is
     * survivable. Package-private and overridable ONLY so the hermetic suite can drive the failure
     * path — this module compiles against the real platform jars, so {@code MDCache} is present
     * here and returns a null token without throwing, meaning the {@code Exception | LinkageError}
     * catches would otherwise have no test anywhere.</p>
     */
    AuthToken fetchToken() {
        return tokens.token();
    }

    private AuthToken token() {
        try {
            return fetchToken();
        } catch (Exception | LinkageError e) {
            warnAuth(e);
            return null;
        }
    }

    private Context context() {
        Context c = context;
        if (c != null) {
            return c;
        }
        AuthToken t = token();
        if (t == null) {
            return null;
        }
        synchronized (this) {
            if (context == null) {
                try {
                    context = Context.createContext(t);
                } catch (Exception | LinkageError e) {
                    warnContext(e);
                }
            }
            return context;
        }
    }

    @Override
    public boolean available() {
        // Both halves, matching what callers previously had to assemble themselves: a token to
        // authorise the lookup, and a Context proving auth is actually usable.
        return token() != null && context() != null;
    }

    // ------------------------------------------------------------------
    // Lookups
    // ------------------------------------------------------------------

    @Override
    public Component byName(Kind kind, String name, String fallbackNamespace, Integer version) {
        if (kind == null || name == null || name.trim().isEmpty()) {
            return null;
        }
        AuthToken t = token();
        if (t == null) {
            return null;
        }
        final String requested = name.trim();
        final String namespace = namespaceOf(requested, fallbackNamespace);
        final String simple = simpleNameOf(requested);
        try {
            MetaInfo.MetaObject obj = MetadataRepository.getINSTANCE()
                    .getMetaObjectByName(entityTypeFor(kind), namespace, simple, version, t);
            return toComponent(obj, requested, kind);
        } catch (Exception | LinkageError e) {
            warnLookup(requested, e);
            return null;
        }
    }

    @Override
    public Optional<Map<String, Object>> streamPropertySet(
            String namespace, String streamName) {
        if (streamName == null || streamName.trim().isEmpty()) {
            return Optional.empty();
        }
        AuthToken t = token();
        if (t == null) {
            return Optional.empty();
        }
        final String requested = streamName.trim();
        final String streamNamespace = namespaceOf(requested, trimToNull(namespace));
        final String simpleStreamName = simpleNameOf(requested);
        try {
            MetaInfo.MetaObject streamObject = MetadataRepository.getINSTANCE()
                    .getMetaObjectByName(EntityType.STREAM, streamNamespace,
                            simpleStreamName, null, t);
            if (!(streamObject instanceof MetaInfo.Stream)) {
                return Optional.empty();
            }

            String propertySetName = ((MetaInfo.Stream) streamObject).pset;
            if (propertySetName == null || propertySetName.trim().isEmpty()) {
                return Optional.empty();
            }
            propertySetName = propertySetName.trim();
            String propertySetNamespace = namespaceOf(propertySetName, streamNamespace);
            String simplePropertySetName = simpleNameOf(propertySetName);

            MetaInfo.MetaObject propertySetObject = MetadataRepository.getINSTANCE()
                    .getMetaObjectByName(EntityType.PROPERTYSET, propertySetNamespace,
                            simplePropertySetName, null, t);
            if (!(propertySetObject instanceof MetaInfo.PropertySet)) {
                return Optional.empty();
            }

            Map<String, Object> properties =
                    ((MetaInfo.PropertySet) propertySetObject).getProperties();
            if (properties == null || properties.isEmpty()) {
                return Optional.of(Collections.<String, Object>emptyMap());
            }
            return Optional.of(Collections.unmodifiableMap(
                    new LinkedHashMap<String, Object>(properties)));
        } catch (Exception | LinkageError e) {
            warnLookup("stream property set " + requested, e);
            return Optional.empty();
        }
    }

    @Override
    public Optional<Boolean> streamEncrypted(String namespace, String streamName) {
        if (streamName == null || streamName.trim().isEmpty()) {
            return Optional.empty();
        }
        AuthToken t = token();
        if (t == null) {
            return Optional.empty();
        }
        final String requested = streamName.trim();
        final String streamNamespace = namespaceOf(requested, trimToNull(namespace));
        final String simpleStreamName = simpleNameOf(requested);
        try {
            MetaInfo.MetaObject streamObject = MetadataRepository.getINSTANCE()
                    .getMetaObjectByName(EntityType.STREAM, streamNamespace,
                            simpleStreamName, null, t);
            if (!(streamObject instanceof MetaInfo.Stream)) {
                return Optional.empty();
            }
            MetaInfo.Flow owningFlow = MetadataRepositoryUtils
                    .getAppMetaObjectBelongsTo((MetaInfo.Stream) streamObject);
            return encryptionState(owningFlow);
        } catch (Exception | LinkageError e) {
            warnLookup("stream encryption " + requested, e);
            return Optional.empty();
        }
    }

    @Override
    public Component byUUID(String uuid) {
        if (uuid == null || uuid.trim().isEmpty()) {
            return null;
        }
        AuthToken t = token();
        if (t == null) {
            return null;
        }
        try {
            MetaInfo.MetaObject obj =
                    MetadataRepository.getINSTANCE().getMetaObjectByUUID(new UUID(uuid), t);
            return toComponent(obj, qualifiedNameOf(obj, uuid), null);
        } catch (Exception | LinkageError e) {
            warnLookup(uuid, e);
            return null;
        }
    }

    @Override
    public List<Component> list(Kind kind) {
        if (kind == null) {
            return Collections.emptyList();
        }
        AuthToken t = token();
        if (t == null) {
            return Collections.emptyList();
        }
        try {
            EntityType type = entityTypeFor(kind);
            if (type == null) {
                return Collections.emptyList();
            }
            Collection<?> objs = MetadataRepository.getINSTANCE().getByEntityType(type, t);
            if (objs == null || objs.isEmpty()) {
                return Collections.emptyList();
            }
            List<Component> out = new ArrayList<Component>(objs.size());
            for (Object o : objs) {
                // instanceof rather than a cast on the wildcard the platform returns: one
                // unexpected element must not lose the whole inventory.
                if (o instanceof MetaInfo.MetaObject) {
                    if (kind == Kind.EXCEPTIONSTORE && !isExceptionStore((MetaInfo.MetaObject) o)) {
                        continue;   // the flagged subset of the WACTIONSTORE inventory
                    }
                    out.add(inventoryComponent((MetaInfo.MetaObject) o, kind));
                }
            }
            return Collections.unmodifiableList(out);
        } catch (Exception | LinkageError e) {
            warnLookup("list " + kind, e);
            return Collections.emptyList();
        }
    }

    @Override
    public Optional<String> applicationStatus(String qualifiedName) {
        if (qualifiedName == null || qualifiedName.trim().isEmpty()) {
            return Optional.empty();
        }
        AuthToken t = token();
        if (t == null) {
            return Optional.empty();
        }
        final String requested = qualifiedName.trim();
        try {
            MetaInfo.MetaObject obj = MetadataRepository.getINSTANCE()
                    .getMetaObjectByName(EntityType.APPLICATION, namespaceOf(requested, null),
                            simpleNameOf(requested), null, t);
            if (!(obj instanceof MetaInfo.Flow)) {
                return Optional.empty();
            }
            return liveStatusName((MetaInfo.Flow) obj, t);
        } catch (Exception | LinkageError e) {
            warnLookup("application status " + requested, e);
            return Optional.empty();
        }
    }

    @Override
    public Optional<Map<String, Object>> monitorSummary(String uuid) {
        if (uuid == null || uuid.trim().isEmpty()) {
            return Optional.empty();
        }
        final String requested = uuid.trim();
        try {
            Map<String, Object> raw = MonitorReportUtility.getEntitySummary(
                    new UUID(requested), new String[0], new HashMap<String, Object>());
            if (raw == null || raw.isEmpty()) {
                return Optional.empty();
            }
            return Optional.of(jdkOnlyMap(raw));
        } catch (Exception | LinkageError e) {
            warnLookup("monitor summary " + requested, e);
            return Optional.empty();
        }
    }

    /**
     * The LIVE status, with the desired-state field as the fallback.
     *
     * <p>Order is load-bearing: {@code flowStatus} is the
     * DESIRED state and is only rewritten when the running action's declared desired state matches,
     * so a soft-resume recovery (which declares none) leaves it reading {@code CRASH} indefinitely
     * on an application that is running. The actual-status map is updated on every real transition.
     * Falling back rather than failing keeps a transient repository error from blanking the answer.
     */
    private Optional<String> liveStatusName(MetaInfo.Flow app, AuthToken t) {
        try {
            MetaInfo.StatusInfo statusInfo =
                    MetadataRepository.getINSTANCE().getStatusInfo(app.getUuid(), t);
            if (statusInfo != null && statusInfo.getStatus() != null) {
                return Optional.of(statusInfo.getStatus().name());
            }
        } catch (Exception | LinkageError e) {
            warnLookup("live status " + app.getFullName(), e);
        }
        return desiredStatusName(app);
    }

    /** The desired-state field, or empty. Package-private so the split can be tested. */
    static Optional<String> desiredStatusName(MetaInfo.Flow app) {
        return (app == null || app.flowStatus == null)
                ? Optional.<String>empty()
                : Optional.of(app.flowStatus.name());
    }

    /**
     * A listed object's JDK-only view.
     *
     * <p>Namespace and simple name are read OFF THE OBJECT rather than re-split from a string:
     * an inventory already knows them exactly, and re-deriving them would reintroduce the very
     * ambiguity {@link #isQualified} exists to bound.
     */
    private static Component inventoryComponent(MetaInfo.MetaObject obj, Kind kind) {
        String uuid = obj.uuid == null ? null : obj.uuid.toString();
        return new Component(qualifiedNameOf(obj, obj.getName()), trimToNull(obj.getNsName()),
                obj.getName(), uuid, kind, propertiesOf(obj), adapterClassNameOf(obj));
    }

    /** The platform's flag, read off the object; false for anything that is not a WActionStore. */
    static boolean isExceptionStore(MetaInfo.MetaObject obj) {
        return obj instanceof MetaInfo.WActionStore && ((MetaInfo.WActionStore) obj).isExceptionstore;
    }

    /**
     * The adapter class a SOURCE or TARGET was created with, as the platform stores it; null for
     * any other kind. Package-private so a test can pin it with a bare {@code MetaInfo} object.
     */
    static String adapterClassNameOf(MetaInfo.MetaObject obj) {
        if (obj instanceof MetaInfo.Source) {
            return trimToNull(((MetaInfo.Source) obj).adapterClassName);
        }
        if (obj instanceof MetaInfo.Target) {
            return trimToNull(((MetaInfo.Target) obj).adapterClassName);
        }
        return null;
    }

    /**
     * {@inheritDoc}
     *
     * <p>Resolved by UUID where the caller has one and by qualified name otherwise, because the
     * two consumers arrive with different halves: an app reading its OWN settings knows its
     * qualified name and may not have a UUID yet during early init.</p>
     */
    @Override
    public Optional<MetadataAccess.Recovery> recoveryOf(Component application) {
        if (application == null) {
            return Optional.empty();
        }
        AuthToken t = token();
        if (t == null) {
            return Optional.empty();
        }
        try {
            MetaInfo.MetaObject obj = resolveApplication(application, t);
            if (!(obj instanceof MetaInfo.Flow)) {
                return Optional.empty();
            }
            MetaInfo.Flow flow = (MetaInfo.Flow) obj;
            return Optional.of(new MetadataAccess.Recovery(flow.getRecoveryType(),
                                                           flow.getRecoveryPeriod()));
        } catch (Exception | LinkageError e) {
            // LinkageError alongside Exception: these classes are system-scoped and simply absent
            // outside a real Striim server, so a unit test would otherwise see an Error rather
            // than the degraded read every other member of this class returns.
            warnLookup("recovery settings " + application.qualifiedName(), e);
            return Optional.empty();
        }
    }

    private MetaInfo.MetaObject resolveApplication(Component application, AuthToken t)
            throws com.webaction.metaRepository.MetaDataRepositoryException {
        String uuid = application.uuid();
        if (uuid != null && !uuid.trim().isEmpty()) {
            return MetadataRepository.getINSTANCE().getMetaObjectByUUID(new UUID(uuid.trim()), t);
        }
        String qualified = application.qualifiedName();
        if (qualified == null || qualified.trim().isEmpty()) {
            return null;
        }
        String ns = namespaceOf(qualified, application.namespace());
        // The COMPONENT's own kind, not a hard-coded APPLICATION. Hard-coding it meant a
        // Kind.FLOW component with no UUID was looked up as an application -- resolving to an
        // unrelated one or to nothing, silently. A channel-aware caller passes its monitored
        // components through here, so this is pinned before it has a second caller.
        // entityTypeFor is a switch and NPEs on null, so the null check must come first -- an
        // `if (et == null)` fallback after the call is unreachable, which is what an earlier draft
        // wrote. A Component's kind is nullable: the constructor accepts it.
        EntityType et = application.kind() == null
                ? EntityType.APPLICATION
                : entityTypeFor(application.kind());
        return MetadataRepository.getINSTANCE()
                .getMetaObjectByName(et, ns, simpleNameOf(qualified), null, t);
    }

    @Override
    public Optional<Map<String, String>> typeFields(String typeUuid) {
        if (typeUuid == null || typeUuid.trim().isEmpty()) {
            return Optional.empty();
        }
        AuthToken t = token();
        if (t == null) {
            return Optional.empty();
        }
        final String requested = typeUuid.trim();
        try {
            MetaInfo.MetaObject obj = MetadataRepository.getINSTANCE()
                    .getMetaObjectByUUID(new UUID(requested), t);
            if (!(obj instanceof MetaInfo.Type)) {
                return Optional.empty();
            }
            return Optional.of(immutableOrderedFields(((MetaInfo.Type) obj).fields));
        } catch (Exception | LinkageError e) {
            warnLookup("type fields " + requested, e);
            return Optional.empty();
        }
    }

    /**
     * Kind to the platform's {@code EntityType}, resolved INSIDE a caller's try block.
     *
     * <p>Deliberately not a {@code static final} map. A static field initializer runs at CLASS
     * LOAD, before any instance method and therefore before any catch — so touching
     * {@code EntityType} there made this class fail to load outright wherever the platform jars are
     * absent, turning a catchable degrade into an uncatchable {@code NoClassDefFoundError}. That is
     * strictly worse than the coupling it replaced, and only the integration harness could catch
     * it: this module's own suite compiles against the real jars, so the class always loads
     * here.</p>
     * <p>Package-private so {@code MdrMetadataAccessTest} can assert that EVERY {@link Kind}
     * has a mapping. A Kind without one is not a compile error and does not throw: it makes
     * {@link #list} return an empty inventory that reads exactly like "no such components".
     */
    static EntityType entityTypeFor(Kind kind) {
        switch (kind) {
            case TARGET:
                return EntityType.TARGET;
            case SOURCE:
                return EntityType.SOURCE;
            case STREAM:
                return EntityType.STREAM;
            case TYPE:
                return EntityType.TYPE;
            case FLOW:
                return EntityType.FLOW;
            case CQ:
                return EntityType.CQ;
            case APPLICATION:
                return EntityType.APPLICATION;
            case WINDOW:
                return EntityType.WINDOW;
            case CACHE:
                return EntityType.CACHE;
            case EXTERNALCACHE:
                return EntityType.EXTERNALCACHE;
            case EXTERNALSOURCE:
                return EntityType.EXTERNALSOURCE;
            case OPENPROCESSOR:
                return EntityType.OPENPROCESSOR;
            case WACTIONSTORE:
            case EXCEPTIONSTORE:
                return EntityType.WACTIONSTORE;
            case NAMESPACE:
                return EntityType.NAMESPACE;
            case SERVER:
                return EntityType.SERVER;
            case DEPLOYMENTGROUP:
                // The platform spells a deployment group DG; the seam spells it out, because
                // `Kind.DG` reads as an abbreviation a caller has to already know.
                return EntityType.DG;
            case PROPERTYSET:
                return EntityType.PROPERTYSET;
            case PROPERTYVARIABLE:
                return EntityType.PROPERTYVARIABLE;
            case CONNECTIONPROFILE:
                return EntityType.CONNECTIONPROFILE;
            case VAULT:
                return EntityType.VAULT;
            case SORTER:
                return EntityType.SORTER;
            default:
                return null;
        }
    }

    /** Converts a platform object into the JDK-only view, or null if it is the wrong kind. */
    private Component toComponent(MetaInfo.MetaObject obj, String requested, Kind expected) {
        if (obj == null) {
            return null;
        }
        Kind actual = kindOf(obj);
        if (expected != null && actual != expected && !accepts(expected, actual)) {
            // The caller asked for a TARGET and got something else. Doing the kind check here is
            // what spares every caller an `instanceof` against a platform type.
            final Kind got = actual;
            log(() -> "MetadataAccess: '" + requested + "' resolved to " + got
                    + ", not " + expected);
            return null;
        }
        Map<String, Object> props = obj instanceof MetaInfo.Target
                ? ((MetaInfo.Target) obj).properties
                : propertiesOf(obj);
        String uuid = obj.uuid == null ? null : obj.uuid.toString();
        return new Component(requested, namespaceOf(requested, null),
                simpleNameOf(requested), uuid, actual, props, adapterClassNameOf(obj));
    }

    private static Map<String, Object> propertiesOf(MetaInfo.MetaObject obj) {
        if (obj instanceof MetaInfo.Source) {
            return ((MetaInfo.Source) obj).properties;
        }
        if (obj instanceof MetaInfo.Target) {
            return ((MetaInfo.Target) obj).properties;
        }
        if (obj instanceof MetaInfo.WActionStore) {
            // The map WActionStores.getInstance(properties) needs to open the store, as stored.
            return ((MetaInfo.WActionStore) obj).properties;
        }
        return null;
    }

    /**
     * Whether a lookup that asked for {@code expected} may return a component of {@code actual}
     * kind: only the WACTIONSTORE ⊇ EXCEPTIONSTORE relation. Package-private so it is pinned.
     */
    static boolean accepts(Kind expected, Kind actual) {
        return expected == Kind.WACTIONSTORE && actual == Kind.EXCEPTIONSTORE;
    }

    /**
     * Deep-converts a platform-produced map into JDK types only.
     *
     * <p>The monitoring summary comes back with platform objects nested inside it, and a caller
     * that must serialise the result cannot be asked to know what they are. Anything that is not a
     * map, a collection, or a primitive wrapper is rendered with {@code String.valueOf} — a
     * degraded but always-serialisable answer.
     *
     * <p>{@link #MAX_CONVERT_DEPTH} is not a tidiness limit. The platform's monitoring objects are
     * free to reference each other, and an unbounded recursion over a cyclic structure is a stack
     * overflow — an {@code Error}, not something the surrounding {@code catch} would turn into a
     * degraded read.
     */
    static Map<String, Object> jdkOnlyMap(Map<?, ?> src) {
        LinkedHashMap<String, Object> out = new LinkedHashMap<String, Object>();
        if (src != null) {
            for (Map.Entry<?, ?> e : src.entrySet()) {
                out.put(String.valueOf(e.getKey()), jdkOnlyValue(e.getValue(), 0));
            }
        }
        return Collections.unmodifiableMap(out);
    }

    static final int MAX_CONVERT_DEPTH = 8;

    static Object jdkOnlyValue(Object v, int depth) {
        if (v == null || v instanceof String || v instanceof Number || v instanceof Boolean) {
            return v;
        }
        // 🚨 AN ARRAY IS A CONTAINER, NOT A LEAF, and falling through to String.valueOf DESTROYED
        // it: the result was the array's IDENTITY -- "[Ljava.lang.String;@1a968a59" -- which is
        // indistinguishable from a legitimate string value, so a caller could not even tell the
        // data had gone. Measured against the real seam before this was fixed: a String[] of two
        // log lines under a monitor-summary key converted to exactly that.
        //
        // Boxing into a List here rather than handling arrays separately routes them through the
        // Collection branch below, so an array is converted element-by-element at depth AND is
        // rendered readably if the depth cap is reached, both for free. Array.get boxes primitive
        // elements, so long[] and int[] are covered by the same three lines as Object[].
        if (v.getClass().isArray()) {
            final int length = java.lang.reflect.Array.getLength(v);
            final List<Object> boxed = new ArrayList<Object>(length);
            for (int i = 0; i < length; i++) {
                boxed.add(java.lang.reflect.Array.get(v, i));
            }
            v = boxed;
        }
        if (depth >= MAX_CONVERT_DEPTH) {
            return String.valueOf(v);
        }
        if (v instanceof Map) {
            LinkedHashMap<String, Object> nested = new LinkedHashMap<String, Object>();
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                nested.put(String.valueOf(e.getKey()), jdkOnlyValue(e.getValue(), depth + 1));
            }
            return Collections.unmodifiableMap(nested);
        }
        if (v instanceof Collection) {
            List<Object> nested = new ArrayList<Object>();
            for (Object item : (Collection<?>) v) {
                nested.add(jdkOnlyValue(item, depth + 1));
            }
            return Collections.unmodifiableList(nested);
        }
        return String.valueOf(v);
    }

    /**
     * The {@link Kind} an MDR object actually is.
     *
     * <p>🚨 <b>Answered from {@code MetaObject.type}, NOT from {@code instanceof} — and that is a
     * correctness requirement, not a style choice.</b> An APPLICATION and a FLOW are the SAME Java
     * class: there is no
     * {@code MetaInfo$Application}, and only {@code type} separates them. An {@code instanceof}
     * ladder therefore answered {@code FLOW} for an application, {@link #toComponent}'s kind gate
     * rejected it, and {@code byName(Kind.APPLICATION, …)} returned {@code null} for every
     * application that exists — silently, and indistinguishably from "no such component".</p>
     *
     * <p><b>That was not hypothetical and it was not confined to one kind.</b> It shipped:
     * the first {@code byName(Kind.APPLICATION, …)} caller got a {@code null} and its ack delay
     * silently resolved to zero. The ladder covered 6 of the {@link Kind}
     * enum's members while {@link #entityTypeFor} maps all of them, so the same silent {@code null}
     * was waiting for the first caller to ask for a CQ, a WINDOW, a CACHE and eleven others.
     * Inverting {@code entityTypeFor} fixes every kind at once and cannot drift from it.</p>
     */
    static Kind kindOf(MetaInfo.MetaObject obj) {
        if (obj == null) {
            return null;
        }
        if (isExceptionStore(obj)) {
            return Kind.EXCEPTIONSTORE;   // before the type loop: it shares WACTIONSTORE's EntityType
        }
        EntityType type = obj.getType();
        if (type != null) {
            for (Kind k : Kind.values()) {
                if (entityTypeFor(k) == type) {
                    return k;
                }
            }
        }
        // Fallback for an object whose type is unset. Kept deliberately: `type` is a plain public
        // field, so a hand-constructed or partially-populated object can reach here with it null,
        // and answering by class is better than answering `null` and being rejected.
        if (obj instanceof MetaInfo.Target) {
            return Kind.TARGET;
        }
        if (obj instanceof MetaInfo.Source) {
            return Kind.SOURCE;
        }
        if (obj instanceof MetaInfo.Stream) {
            return Kind.STREAM;
        }
        if (obj instanceof MetaInfo.Type) {
            return Kind.TYPE;
        }
        if (obj instanceof MetaInfo.Flow) {
            return Kind.FLOW;
        }
        return null;
    }

    // ------------------------------------------------------------------
    // Property reads — the case-insensitivity and the decryption dance that
    // a module otherwise re-implements.
    // ------------------------------------------------------------------

    @Override
    public String property(Component component, String key) {
        Object v = rawProperty(component, key);
        return v == null ? "" : v.toString().trim();
    }

    @Override
    public String secret(Component component, String key) {
        Object v = rawProperty(component, key);
        if (v == null) {
            return "";
        }
        if (v instanceof Password) {
            Password pw = (Password) v;
            try {
                String plain = pw.getPlain();
                if ((plain == null || plain.isEmpty()) && pw.getEncrypted() != null) {
                    byte[] salt = saltFor(component);
                    if (salt != null) {
                        pw.setSalt(salt);
                        plain = pw.getPlain();
                    }
                }
                return plain == null ? "" : plain;
            } catch (Exception | LinkageError e) {
                warnLookup("secret " + key, e);
                return "";
            }
        }
        if (v instanceof Map) {
            Object plain = ((Map<?, ?>) v).get("plain");
            return plain == null ? "" : plain.toString();
        }
        return v.toString();
    }

    private byte[] saltFor(Component component) {
        if (component == null || component.uuid() == null) {
            return null;
        }
        AuthToken t = token();
        if (t == null) {
            return null;
        }
        try {
            SecurityInfo si = MetadataRepository.getINSTANCE()
                    .getSecurityInfoByUUID(new UUID(component.uuid()), t);
            return si == null ? null : si.getSalt();
        } catch (Exception | LinkageError e) {
            warnLookup("securityInfo " + component.uuid(), e);
            return null;
        }
    }

    private static Object rawProperty(Component component, String key) {
        if (component == null || key == null) {
            return null;
        }
        Map<String, Object> props = component.properties();
        Object v = props.get(key);
        if (v != null) {
            return v;
        }
        for (Map.Entry<String, Object> e : props.entrySet()) {
            if (e.getKey() != null && e.getKey().equalsIgnoreCase(key)) {
                return e.getValue();
            }
        }
        return null;
    }

    // ------------------------------------------------------------------
    // Name splitting — the trap this seam exists to absorb.
    //
    // Package-private and separated from the platform call ON PURPOSE: it is pure logic
    // here, and keeping it callable without a live MetadataRepository is what lets it be tested
    // exhaustively -- see MdrMetadataAccessTest's split table.
    //
    // The guard is `dot > 0 && dot < len-1`, matching the prior art this seam consolidates. Both bounds are load-bearing:
    //   dot > 0     -- ".Name" must NOT yield an empty namespace; it is a simple name.
    //   dot < len-1 -- "ns." must NOT yield an empty simple name, which the platform answers
    //                  with the same silent null the seam exists to eliminate.
    // A first-dot split means "a.b.c" is namespace "a", name "b.c".
    // ------------------------------------------------------------------

    static boolean isQualified(String trimmedName) {
        final int dot = trimmedName.indexOf('.');
        return dot > 0 && dot < trimmedName.length() - 1;
    }

    static String namespaceOf(String trimmedName, String fallbackNamespace) {
        return isQualified(trimmedName)
                ? trimmedName.substring(0, trimmedName.indexOf('.'))
                : fallbackNamespace;
    }

    static String simpleNameOf(String trimmedName) {
        return isQualified(trimmedName)
                ? trimmedName.substring(trimmedName.indexOf('.') + 1)
                : trimmedName;
    }

    static String qualifiedNameOf(MetaInfo.MetaObject obj, String fallback) {
        if (obj == null) {
            return fallback;
        }
        String name = trimToNull(obj.getName());
        if (name == null) {
            return fallback;
        }
        String namespace = trimToNull(obj.getNsName());
        return namespace == null ? name : namespace + "." + name;
    }

    static Map<String, String> immutableOrderedFields(Map<String, String> fields) {
        LinkedHashMap<String, String> orderedCopy = new LinkedHashMap<String, String>();
        if (fields != null) {
            orderedCopy.putAll(fields);
        }
        return Collections.unmodifiableMap(orderedCopy);
    }

    static Optional<Boolean> encryptionState(MetaInfo.Flow owningFlow) {
        return owningFlow == null
                ? Optional.empty()
                : Optional.of(Boolean.valueOf(owningFlow.encrypted));
    }

    private static String trimToNull(String value) {
        if (value == null) {
            return null;
        }
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    // ------------------------------------------------------------------

    private void warnAuth(Throwable e) {
        if (!authWarned) {
            authWarned = true;
            log(() -> "MetadataAccess: security token unavailable; lookups will return null and"
                    + " callers should degrade: " + e);
        }
    }

    private void warnContext(Throwable e) {
        // Its OWN flag and message. Sharing the auth flag meant a context failure either reported
        // "security token unavailable" -- false, the token was obtained -- or, if the token had
        // already warned, logged nothing at all.
        if (!contextWarned) {
            contextWarned = true;
            log(() -> "MetadataAccess: security context could not be created from a valid token;"
                    + " lookups needing a context will degrade: " + e);
        }
    }

    private void warnLookup(String what, Throwable e) {
        if (!lookupWarned) {
            lookupWarned = true;
            log(() -> "MetadataAccess: metadata lookup failed for '" + what
                    + "'; returning null: " + e);
        }
    }

    private void log(java.util.function.Supplier<String> msg) {
        if (logger != null) {
            logger.logError(msg);
        }
    }
}
