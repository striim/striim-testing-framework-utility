package com.example.common;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * The shared seam for reading the Striim Metadata Repository.
 *
 * <p><b>No platform type appears on this surface.</b> That is the whole point, and it is what an
 * earlier revision got wrong: returning {@code AuthToken}, {@code Context},
 * {@code MetaInfo.MetaObject} and {@code SecurityInfo}, and taking {@code EntityType}, meant an
 * adopter stopped <i>calling</i> the platform but still <i>imported</i> it — and a unit test still
 * needed those classes on its classpath just to build a fake. Escaping the server while staying
 * bound to the jars is not the goal; being able to mock live Striim is. {@link SecretResolver}
 * already stated this principle; this interface follows it.</p>
 *
 * <p>So everything here is a JDK type or a nested type declared below. A fake is a few lines with
 * no {@code STRIIM_HOME} on the test classpath at all.</p>
 *
 * <h2>Contract: best-effort, never fatal</h2>
 * Every method returns {@code null} (or an empty value) rather than throwing. A module that cannot
 * reach the MDR should degrade — pace open-loop, skip an enrichment, log and carry on — never fail
 * application start.
 *
 * <p>Implementations MUST catch {@code LinkageError} as well as {@code Exception}. The MDR classes
 * are {@code system}-scoped and absent wherever a module runs outside a real Striim server, where a
 * missing class raises {@code NoClassDefFoundError} — an {@code Error} that {@code catch
 * (Exception)} does not catch.</p>
 *
 * <h2>Contract: nothing at construction</h2>
 * Implementations MUST NOT touch the MDR while being constructed: {@code MDCache} is not guaranteed
 * initialized when a module is built, so credentials are acquired on first use. Callers must honour
 * this too — <b>resolving from inside a {@code Processor} constructor defeats the laziness</b> and
 * turns an absent MDR into a construction-time failure.
 *
 * @see MdrMetadataAccess the production implementation
 */
public interface MetadataAccess {

    /**
     * The component kinds this seam can look up — a local enum precisely so callers need not import
     * the platform's {@code EntityType}.
     */
    enum Kind {
        TARGET,
        SOURCE,
        STREAM,
        TYPE,
        FLOW,
        CQ,

        // Added for inventory reads (see #list). Each maps 1:1 onto the EntityType the platform's
        // own `LIST <plural>;` command uses, so an inventory taken through this seam and one taken
        // from the console agree by construction rather than by coincidence.
        APPLICATION,
        WINDOW,
        CACHE,
        EXTERNALCACHE,
        EXTERNALSOURCE,
        OPENPROCESSOR,
        WACTIONSTORE,
        NAMESPACE,
        SERVER,
        DEPLOYMENTGROUP,
        PROPERTYSET,
        PROPERTYVARIABLE,
        CONNECTIONPROFILE,
        VAULT,
        SORTER,

        /**
         * A WActionStore the platform created as an application's exception store
         * ({@code MetaInfo.WActionStore.isExceptionstore}; named {@code <App>_ExceptionStore} in
         * the application's namespace). It shares {@link #WACTIONSTORE}'s entity type, so
         * {@code list(EXCEPTIONSTORE)} is the flagged subset of {@code list(WACTIONSTORE)}, and a
         * lookup that asks for {@link #WACTIONSTORE} still accepts one — every exception store is
         * a WActionStore, and a caller asking for the broader kind means the broader kind.
         */
        EXCEPTIONSTORE
    }

    /**
     * What a lookup found, in JDK types only.
     *
     * <p>{@code properties} is the component's own property map as the platform stored it. Values
     * may still be platform objects — an encrypted password, for instance — which is why callers
     * should read them through {@link MetadataAccess#property} and {@link MetadataAccess#secret}
     * rather than reaching into the map. Those absorb the case-insensitivity and the decryption
     * dance that a module otherwise re-implements.</p>
     */
    final class Component {

        private final String qualifiedName;
        private final String namespace;
        private final String name;
        private final String uuid;
        private final Kind kind;
        private final Map<String, Object> properties;
        private final String adapterName;

        public Component(String qualifiedName, String namespace, String name, String uuid,
                         Kind kind, Map<String, Object> properties) {
            this(qualifiedName, namespace, name, uuid, kind, properties, null);
        }

        /**
         * the seven-argument form carries {@link #adapterName()}. The six-argument form
         * stays so every existing fake compiles and reads {@code null} there.
         */
        public Component(String qualifiedName, String namespace, String name, String uuid,
                         Kind kind, Map<String, Object> properties, String adapterName) {
            this.qualifiedName = qualifiedName;
            this.namespace = namespace;
            this.name = name;
            this.uuid = uuid;
            this.kind = kind;
            this.properties = properties == null
                    ? Collections.emptyMap()
                    : Collections.unmodifiableMap(new LinkedHashMap<String, Object>(properties));
            this.adapterName = adapterName;
        }

        /** As requested, e.g. {@code "myns.LedgerTarget"}. */
        public String qualifiedName() {
            return qualifiedName;
        }

        /** The namespace half of {@link #qualifiedName()}, with no trailing dot. */
        public String namespace() {
            return namespace;
        }

        /** The simple name, with no namespace prefix. */
        public String name() {
            return name;
        }

        /** The component's UUID rendered as a string — never the platform's UUID type. */
        public String uuid() {
            return uuid;
        }

        /** What kind of component this is, which decides how its properties are read. */
        public Kind kind() {
            return kind;
        }

        /** The component's properties, unmodifiable and never null — empty when it has none. */
        public Map<String, Object> properties() {
            return properties;
        }

        /**
         * The adapter class the platform records for a {@link Kind#SOURCE} or {@link Kind#TARGET}
         * — {@code MetaInfo.Source.adapterClassName} / {@code MetaInfo.Target.adapterClassName},
         * e.g. {@code "com.webaction.proc.DatabaseReader_1_0"} — exactly as stored, so a caller that keys
         * on the adapter (is this source DatabaseReader?) can do so without reaching the MDR itself.
         * Normalising the name is the caller's business: the seam does not decide which suffixes
         * are version noise. {@code null} for every other kind, and for a component built through
         * the six-argument constructor.
         */
        public String adapterName() {
            return adapterName;
        }

        @Override
        public String toString() {
            return "Component[" + kind + " " + qualifiedName + "]";
        }
    }

    /**
     * Looks up one component by name, or {@code null} if it is absent, unreachable, or of a
     * different kind.
     *
     * <p>{@code name} may be qualified ({@code "myns.MyTarget"}) or simple ({@code "MyTarget"});
     * implementations split it. <b>This is the trap the seam exists to absorb:</b> the platform's
     * own lookup takes namespace and simple name as SEPARATE arguments, so passing a qualified
     * string with a null namespace silently finds nothing — no error, just a null that reads like
     * "not configured". Adopters have had to discover that independently.</p>
     *
     * <p>The kind is checked by the implementation, so a caller never needs an {@code instanceof}
     * against a platform type: ask for a {@link Kind#TARGET} and a stream comes back as
     * {@code null}.</p>
     *
     * @param kind              the component kind required
     * @param name              qualified or simple name
     * @param fallbackNamespace namespace to use when {@code name} is unqualified; may be null
     * @param version           metadata version, or {@code null} for any
     */
    Component byName(Kind kind, String name, String fallbackNamespace, Integer version);

    /**
     * Reads the property set attached to a stream, as a defensive JDK-only map snapshot.
     *
     * <p>This is additive by design: implementers compiled against the earlier seam keep
     * working and report that stream property-set discovery is unavailable until they opt in.
     * Implementations must perform reads only and return {@link Optional#empty()} when the stream,
     * its property set, or the repository cannot be resolved.
     *
     * @param namespace the stream namespace
     * @param streamName the simple stream name
     */
    default Optional<Map<String, Object>> streamPropertySet(
            String namespace, String streamName) {
        return Optional.empty();
    }

    /**
     * Reads whether the owning application encrypts a persisted stream.
     *
     * <p>The default deliberately reports unknown rather than plaintext. Existing
     * implementers therefore remain source/binary compatible, while callers can distinguish a
     * confirmed {@code false} from an MDR state they could not resolve.
     */
    default Optional<Boolean> streamEncrypted(String namespace, String streamName) {
        return Optional.empty();
    }

    /**
     * Reads a Type's declaration-order field map by UUID as a defensive JDK-only snapshot.
     *
     * <p>The default is deliberately empty so existing implementers remain source and
     * binary compatible. Field order is part of a Striim event's positional schema and callers
     * must therefore iterate the returned map without sorting it.
     */
    default Optional<Map<String, String>> typeFields(String typeUuid) {
        return Optional.empty();
    }

    /** Looks up one component by its string UUID, or {@code null}. */
    Component byUUID(String uuid);

    /**
     * Every component of one kind that this repository holds, as a defensive JDK-only list.
     *
     * <p>The inventory read. This is what the platform's {@code LIST <plural>;} console command
     * does, and going through the repository rather than the command matters for a reason beyond
     * taste: the command surface is <b>name-bearing and unvalidated at the seam</b>. A caller that
     * assembles {@code "LIST " + something} discovers only at runtime, as an HTTP 400 with no
     * usable message, that {@code something} was never a legal object type. Asking for a
     * {@link Kind} cannot express that mistake at all.
     *
     * <p>Ordering is the repository's, which is unspecified; callers that need stable output must
     * sort. {@link Component#properties()} is populated only for the kinds that carry a property
     * map, so an inventory of a thousand components does not drag a thousand property maps with
     * it.
     *
     * <p>Additive by design: the default reports "inventory unavailable" as an empty list, so
     * implementers compiled against the earlier seam keep working. An empty list therefore does not
     * distinguish "no such components" from "not implemented" — a caller that needs the difference
     * should gate on {@link #available()} first.
     */
    default List<Component> list(Kind kind) {
        return Collections.emptyList();
    }

    /**
     * An application's LIVE run status, as the platform's own status name (e.g. {@code "RUNNING"},
     * {@code "CRASH"}, {@code "QUIESCED"}); {@link Optional#empty()} when the application or the
     * repository cannot be resolved.
     *
     * <p><b>Live, not desired.</b> An application object carries a status field that is the
     * platform's <i>desired</i> state: it is only rewritten when the currently-running action's
     * declared desired state matches the new one. Auto-resume recovers a crashed application via a
     * soft resume, which declares no desired state at all — so that field can read {@code CRASH}
     * forever after the application has actually recovered, while the platform's actual-status map
     * is updated on every real transition. Implementations MUST prefer the actual-status map and
     * fall back to the desired-state field only when the live lookup is unavailable, so a transient
     * repository error degrades to a stale answer rather than to none.
     *
     * @param qualifiedName the application name, qualified ({@code "myns.MyApp"}) or simple
     */
    default Optional<String> applicationStatus(String qualifiedName) {
        return Optional.empty();
    }

    /**
     * The platform's monitoring summary for one entity, keyed by its MDR UUID, as a nested
     * structure of JDK types only; {@link Optional#empty()} when it cannot be read.
     *
     * <p>This is monitoring data rather than metadata, and it lives on this seam deliberately: it
     * is addressed by MDR UUID and needs exactly the token, context and degrade-never-throw
     * machinery every method here already has. A separate seam would duplicate all of it to carry
     * one method.
     *
     * <p>It is the same source the {@code MON <entity>;} console command reads, and it answers for
     * <b>any</b> entity with a UUID — an application, a stream, a source, a target, or a
     * <i>server</i>. That last one is the point: there is no console command that reports one
     * server's monitoring summary by the literal word "server", and assembling one is how a caller
     * ends up sending a command the grammar rejects.
     *
     * <p>Implementations MUST convert values to JDK types (maps, lists, strings, numbers, booleans)
     * rather than passing platform objects through, so a caller can serialise the result without
     * knowing what produced it.
     *
     * @param uuid the entity's UUID as a string, e.g. from {@link Component#uuid()}
     */
    default Optional<Map<String, Object>> monitorSummary(String uuid) {
        return Optional.empty();
    }

    /**
     * Reads a property off a component, case-insensitively, as a trimmed string; {@code ""} when
     * absent.
     *
     * <p>Case-insensitive because MDR key casing has varied by build, which every module reading
     * component properties has had to work around on its own.</p>
     */
    String property(Component component, String key);

    /**
     * An application's recovery configuration, or empty when it cannot be read.
     *
     * <p><b>Why this needs its own accessor rather than {@link #property}:</b> that method reads
     * the {@code properties} map, and a {@code MetaInfo.Flow} does not have one — only
     * {@code MetaInfo.Source} and {@code MetaInfo.Target} do. Recovery type and period are fields
     * on the flow itself, so no existing member of this interface can reach them. That is what
     * made every consumer walk {@code getTopLevelFlow().getMetaInfo()} and cast.</p>
     *
     * <p><b>Designed against BOTH consumers, not just the one that motivated it.</b>
     * A reader may want its OWN app's settings, to derive an ack delay from the
     * checkpoint interval, or ANOTHER app's, to report {@code isRecoveryEnabled}
     * for an app it monitors. Taking a {@link Component} serves both, because either can name its
     * subject — the first through {@code appQualifiedName()}, the second through the lookup it
     * already does.</p>
     *
     * <p>Empty is a normal answer, not an error: during early init the component is not wired yet,
     * and callers are expected to retry rather than fail.</p>
     */
    default Optional<Recovery> recoveryOf(Component application) {
        return Optional.empty();
    }

    /**
     * An application's recovery settings, in JDK types only.
     *
     * <p>🚨 <b>There is deliberately NO {@code configured()} predicate here.</b> An earlier
     * version carried one — {@code type != 0 && period > 0} —
     * justified as sparing both callers from re-deriving it. That justification was false: they do
     * not want the same predicate, and neither does the platform. On 5.4:</p>
     * <ul>
     *   <li>{@code components.Flow} tests the TYPE against a specific value;</li>
     *   <li>a monitoring reader tests {@code getRecoveryPeriod() > 0} — the PERIOD
     *       only — with a comment that it must match the platform's own {@code mon <app>;}
     *       semantics;</li>
     *   <li>a reader deriving an ack delay tests {@code type == 0 || periodSec <= 0}.</li>
     * </ul>
     * <p>{@code Context.putFlow} fills type and interval independently, so they are not guaranteed
     * to agree — and a conjunction of the two matches none of the three. Shipping it would have
     * created exactly the disagreement the rationale claimed to prevent, and would have silently
     * changed a monitoring reader's {@code isRecoveryEnabled} when the shared implementation was adopted.
     * <b>Expose the two fields; let each caller keep the predicate it can justify.</b></p>
     */
    final class Recovery {

        private final int type;
        private final long periodSeconds;

        public Recovery(int type, long periodSeconds) {
            this.type = type;
            this.periodSeconds = periodSeconds;
        }

        /** The platform's recovery type; {@code Capabilities.RECOVERY_NONE == 0} means disabled. */
        public int type() {
            return type;
        }

        /** The checkpoint interval in SECONDS, as the platform stores it. */
        public long periodSeconds() {
            return periodSeconds;
        }

        @Override
        public String toString() {
            return "Recovery{type=" + type + ", periodSeconds=" + periodSeconds + "}";
        }
    }


    /**
     * Reads a possibly-encrypted property as plaintext; {@code ""} when absent or undecryptable.
     *
     * <p>Absorbs the decryption dance — if the plaintext is not populated but an encrypted value
     * is, fetch the component's salt, apply it, and re-read — so that neither the platform's
     * password type nor its security-info type appears in a caller. A caller that gets {@code ""}
     * should degrade, falling back to default credentials, not fail.</p>
     */
    String secret(Component component, String key);

    /**
     * Whether the repository is reachable right now — the liveness gate a caller checks before
     * attempting resolution, replacing the platform-typed credentials an earlier revision exposed.
     * Lazy: the first call is what tries.
     */
    boolean available();
}
