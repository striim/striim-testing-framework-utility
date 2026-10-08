package com.example.common;

import com.webaction.recovery.ComponentCheckpoint;
import com.webaction.runtime.components.Flow;
import com.webaction.runtime.components.FlowComponent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.proc.SourceProcess;

import java.util.Map;

/**
 * The reader shell's shared platform identity: which application this reader belongs to, and
 * which component inside it this reader is. Every reader {@code App} needs both to name anything
 * per-component — a checkpoint file, a metric, a log tag — and before this class each one
 * hand-rolled them.
 *
 * <p><b>What was actually wrong with the hand-rolls.</b> Both earlier copies reached the right
 * objects and then made two avoidable mistakes with what they found:</p>
 *
 * <ul>
 *   <li><b>They tested {@code getMetaInfo() instanceof MetaInfo.Flow} and branched on it.</b>
 *       Unnecessary: {@link Flow#flowInfo} is a {@code public final} field already typed
 *       {@code MetaInfo.Flow}, so reading it needs no cast and admits no {@code else}. That
 *       {@code else} is where the two copies diverged — one fell back to the component's own name,
 *       the other returned {@code null} — which is a divergence in <i>checkpoint identity</i>.</li>
 *   <li><b>They guarded with {@code getFullName() != null && !isEmpty()}.</b> That guard provably
 *       cannot fire: {@code MetaInfo.MetaObject.getFullName()} is
 *       {@code return this.nsName + "." + this.name;} — plain concatenation. It cannot yield
 *       {@code null} or {@code ""}; its degenerate output is the literal text {@code "null.null"},
 *       which sails through the guard and becomes a checkpoint identity. {@link FlowIdentity}
 *       composes from the parts instead, so that value is unrepresentable.</li>
 * </ul>
 *
 * <p><b>⚠ Read this before "simplifying" to {@code getOwnerFlow()}.</b> {@code SourceProcess}
 * extends {@code BaseProcess}, which also has-a {@code Flow} exposed as {@code getOwnerFlow()}, and
 * for a source it is set to the <b>top-level</b> flow before {@code init()} runs
 * ({@code Source.start()} calls {@code setOwnerFlow(getTopLevelFlow())}, then
 * {@code setFlowComponent(this)}, then {@code init(...)}; neither {@code BaseProcess}'s nor
 * {@code SourceProcess}'s seven-argument {@code init} touches the field afterwards — the former
 * ignores its {@code Flow} parameter entirely). It looks like the shorter, more direct answer, and
 * this class used it in its first revision.
 *
 * <p><b>It is not safe here, and a code review caught it.</b> {@code getOwnerFlow()} is populated
 * only on the paths that bother to: {@code Source} and {@code Target} do,
 * <b>{@code historicalcache.Cache} and {@code Lookup} do NOT</b> — they call
 * {@code setFlowComponent(this)} alone, and {@code QueryManager} invokes the seven-argument
 * {@code init} with a {@code null} flow. A {@code SourceProcess}-based reader deployed as a cache
 * adapter (for example {@code CREATE CACHE ... USING DatabaseReader(...)}) would therefore see
 * {@code getOwnerFlow() == null} for its entire life — <b>permanently, not "not yet"</b> — while
 * {@code getFlowComponent().getTopLevelFlow()} answers correctly, because {@code Flow.createComponent}
 * calls {@code setFlow} for cache components too. Reading through the component also keeps this
 * class evaluating the same expression as {@link AbstractOpenProcessorApp}, rather than a snapshot
 * that can drift from it.</p>
 *
 * <p><b>Testability — and a claim this class carried for its whole life that was FALSE.</b> It
 * used to say both methods were "compile-verified only, because a {@code SourceProcess} subclass
 * cannot be constructed in a test". The {@code NoClassDefFoundError: zmq/ZError$IOException}
 * behind that was a missing {@code jeromq} dependency in this module's pom — one every reader
 * module already declared. With it, a subclass constructs and both accessors run;
 * a test can drive them, including the wired, unwired and degenerate cases.
 * <b>Four mutations to this file and its sibling survived the entire suite while that sentence
 * stood</b>, so the cost of an unexamined "cannot be tested" is on record.</p>
 *
 * <p><b>⚠ {@code public}, and NOT {@code final} — both deliberate, and the second is a trade.</b>
 * {@code public} because the adopting modules expose these through their own seam interfaces
 * (a {@code PlatformContext}, a {@code SourceChannel}), and Java forbids narrowing.
 * {@code final} is what would actually prevent the divergence this class exists to end, and was the first choice; it is rejected because it
 * makes the shared change and every module's adoption <b>one indivisible commit</b>, which
 * Operating Rule 1 forbids. <b>When the last adoption lands, make it {@code final}.</b></p>
 *
 * <p><b>⚠ Adoption is NOT simply "delete the module's copy".</b> An earlier revision of this note
 * said it was, and that is wrong for a module whose {@code appQualifiedName()} falls
 * back to the component's own full name where this one returns {@code null}, and its
 * {@code Processor} keys the checkpoint filename off the result. Deleting it unreviewed changes
 * which file that reader recovers from. <b>Each module's adoption is a checkpoint-identity change
 * and must be reasoned about in its own track</b> — the base is deliberately not a superset of
 * every hand-roll, because a fallback that returns a different <i>kind</i> of name is a second
 * identity rather than a safety net.</p>
 *
 * <p><b>In-stream OPs get the same two accessors</b> from {@link AbstractOpenProcessorApp}, which
 * reaches them by a different route — {@code StriimOpenProcessor} is not a {@code BaseProcess}, but
 * the {@code OpenProcessor} it is handed <i>is</i> a {@code FlowComponent}. Java's single
 * inheritance forces two reaches; both walk from the component and both end at the same
 * {@link FlowIdentity} decision.</p>
 *
 * <p><b>The {@code ComponentCheckpoint} seam lives here too, as of the {@link #checkpointCore()}
 * hook.</b> {@link AbstractCheckpointSourceApp} and {@link AbstractSourceApp} are siblings — both
 * extend this class, neither the other — so a reader base that needs both the {@code
 * SourcePosition} emit/identity shape ({@code AbstractSourceApp}) and a hand-built {@code
 * ComponentCheckpoint} (previously only {@code AbstractCheckpointSourceApp}'s territory) could not
 * get both without one of them duplicating the other's {@code getCheckpoint()} body. One
 * reader's {@code App} did exactly that: a byte-identical copy of {@code
 * AbstractCheckpointSourceApp.getCheckpoint()} against its own field, because it extends {@code
 * AbstractSourceApp} for emit and could not also extend the other. Hoisting the body one level up,
 * behind a method every reader can override regardless of which sibling it descends through, is
 * what lets that copy be deleted.</p>
 */
public abstract class AbstractReaderApp extends SourceProcess {

    /**
     * The recovery-participating core behind this reader, or {@code null} for a reader with
     * nothing of its own to add to the framework's default checkpoint. Most readers never
     * override this and get the
     * platform's own {@link #getCheckpoint()} unchanged. {@link AbstractCheckpointSourceApp}
     * overrides it to return its {@code processor}; a reader may
     * override it the same way despite descending through {@link AbstractSourceApp} instead.
     */
    protected CheckpointBuilder checkpointCore() {
        return null;
    }

    /**
     * The shared {@code ComponentCheckpoint} seam for every reader, whichever
     * recovery family it belongs to. Delegates to {@link #checkpointCore()} so a subclass
     * participates by overriding one method rather than {@code getCheckpoint()} itself.
     *
     * <p>{@code core == null} is the common case (no recovery participation) and returns the
     * framework's own default unchanged — the same guard {@link AbstractCheckpointSourceApp} used
     * to declare on its own, load-bearing because the framework may call this before {@code
     * init()} has built the core.</p>
     */
    @Override
    public ComponentCheckpoint getCheckpoint() {
        CheckpointBuilder core = checkpointCore();
        ComponentCheckpoint defaultCheckpoint = super.getCheckpoint();
        return (core == null) ? defaultCheckpoint
                : core.buildCheckpoint(defaultCheckpoint, this.sourceUUID, this.distributionID);
    }

    /**
     * Reports TQL properties this reader never declared, then hands the map to the platform.
     * Every {@code init} overload the platform calls ({@code BaseProcess} chains the 7-, 4- and
     * 2-argument forms down to this one) reaches here, so a reader gets the guard by extending
     * this base and calling {@code super.init(properties)} as every reader does
     * Reporting only: the app still starts. The logger is per component, so the line
     * names the source that carries the typo.
     */
    @Override
    public void init(Map<String, Object> properties) throws Exception {
        String tag = componentQualifiedName();
        DeclaredProperties.report(getClass(), properties,
                new Logger(tag != null ? tag : getClass().getSimpleName(), false));
        super.init(properties);
    }

    /**
     * This reader's owning application as {@code namespace.appName}, or {@code null} while the
     * platform has not attached the flow yet.
     *
     * <p>{@code null} is a defer signal. A caller that turns it into a placeholder produces an
     * identity shared by every reader on the node — the exact collision a per-component name
     * exists to prevent.</p>
     */
    public String appQualifiedName() {
        try {
            FlowComponent component = getFlowComponent();
            if (component == null) {
                return null;
            }
            Flow app = component.getTopLevelFlow();
            if (app == null) {
                return null;
            }
            MetaInfo.Flow info = app.flowInfo;
            return (info == null) ? null : FlowIdentity.compose(info.getNsName(), info.getName());
        } catch (Throwable ignore) {
            // ⚠ Throwable, not Exception, and it is a trade rather than an oversight. Under the
            // plugin classloader the realistic failure here is a LinkageError from a mis-shaded
            // jar, which is permanent and structural — and swallowing it costs the operator a
            // stack trace they would want. It is caught anyway because BOTH implementations this
            // replaces caught Throwable, and a shared layer reproduces the behaviour
            // it hoists rather than improving it in the same change. This base has no logger to
            // report through; giving it one is a follow-up.
            return null;
        }
    }

    /**
     * This reader component as {@code namespace.sourceName}, or {@code null} while the platform
     * has not attached the component yet.
     *
     * <p><b>Prefer this over {@link #appQualifiedName()} when keying per-reader state.</b> It is
     * strictly more specific — it is what distinguishes two readers configured inside one
     * application, which is the thing a per-component name exists to do — and it is the one of the
     * two that answers on <i>every</i> adapter path, because {@code FlowComponent.info} is assigned
     * in the {@code FlowComponent(BaseServer, MetaObject)} constructor while the flow is attached
     * later.</p>
     */
    public String componentQualifiedName() {
        try {
            FlowComponent component = getFlowComponent();
            if (component == null) {
                return null;
            }
            MetaInfo.MetaObject info = component.getMetaInfo();
            return (info == null) ? null : FlowIdentity.compose(info.getNsName(), info.getName());
        } catch (Throwable ignore) {
            return null;
        }
    }
}
