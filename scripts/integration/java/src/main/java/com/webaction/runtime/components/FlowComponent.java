package com.webaction.runtime.components;

import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.FlowComponent}: the type
 * {@code RetriableWriter.getFlowComponent()} names, and the supertype of {@link Flow} and
 * {@link Stream}. Abstract, as the platform class is.
 *
 * <p>The members are the ones field's common bases call on the component they are handed:
 * {@code ExceptionStorePublisher} reads the application ({@link #getTopLevelFlow()}) and the
 * component's own name and type, and {@code ComponentBackgroundFailures} reports through
 * {@link #notifyAppMgr(Exception)} and asks {@link #isStopping()}. Without them the writer's
 * exception-store route died with {@code NoSuchMethodError} the first time a row was skipped.
 * Signatures match the platform class on 5.4.0.6G (javap); the semantics below are the real
 * class's (cfr) except where a note says otherwise.</p>
 */
public abstract class FlowComponent {

    private final MetaInfo.MetaObject info;
    private Flow flow;

    /** Set while the component's flow is stopping. Never set by this tier. */
    protected volatile boolean isStopping;

    /**
     * Harness-only: a component with no metadata of its own. The real constructor takes a
     * {@code BaseServer} and the component's {@code MetaObject}; module code never constructs a
     * component, so it never reaches for either shape.
     */
    protected FlowComponent() {
        this(null);
    }

    /** Harness-only: a component described by {@code info}. See {@link #FlowComponent()}. */
    protected FlowComponent(final MetaInfo.MetaObject info) {
        this.info = info;
    }

    /** Attaches the component to the flow it runs in. */
    public void setFlow(final Flow flow) {
        this.flow = flow;
    }

    /** The flow the component runs in, or null when it is in none. */
    public Flow getFlow() {
        return flow;
    }

    /**
     * The application the component belongs to: null when it is in no flow, the flow itself when
     * that flow has no parent, else the flow's own top-level flow -- the real class's walk.
     */
    public Flow getTopLevelFlow() {
        if (flow == null) {
            return null;
        }
        if (flow.getFlow() == null) {
            return flow;
        }
        return flow.getTopLevelFlow();
    }

    /** The component's metadata; null for a harness component built without any. */
    public MetaInfo.MetaObject getMetaInfo() {
        return info;
    }

    /**
     * {@code getMetaInfo().getType()} on the real class.
     *
     * <p><b>Null-tolerant where the real class is not:</b> with no metadata it answers null rather
     * than throwing {@code NullPointerException}. The writer's component in this tier is built
     * without metadata (see {@code HarnessFlowComponent} for why), and the exception-store route
     * reads the type of exactly that component. A null type is what the platform's own
     * {@code ExceptionType.getExceptionType} and {@code ExceptionEvent} already accept.</p>
     */
    public EntityType getMetaType() {
        return info == null ? null : info.getType();
    }

    /** {@code getMetaInfo().getUuid()}; null-tolerant for the reason {@link #getMetaType()} gives. */
    public UUID getMetaID() {
        return info == null ? null : info.getUuid();
    }

    /** {@code getMetaInfo().getName()}; null-tolerant for the reason {@link #getMetaType()} gives. */
    public String getMetaName() {
        return info == null ? null : info.getName();
    }

    /** {@code getMetaInfo().getNsName()}; null-tolerant for the reason {@link #getMetaType()} gives. */
    public String getMetaNsName() {
        return info == null ? null : info.getNsName();
    }

    /**
     * {@code getMetaInfo().getFullName()}, i.e. {@code nsName + "." + name}; null-tolerant for the
     * reason {@link #getMetaType()} gives.
     */
    public String getMetaFullName() {
        return info == null ? null : info.getFullName();
    }

    /** Whether the component's flow is stopping. False throughout a tier run. */
    public boolean isStopping() {
        return isStopping;
    }

    /** Set by the platform around {@code stop()}. */
    public void setIsStopping(final boolean isStopping) {
        this.isStopping = isStopping;
    }

    /**
     * The platform transitions the application here (an {@code ExceptionEvent} to the node
     * manager, which halts or crashes the application). This tier does not model an application,
     * so it refuses, as {@code RetriableWriter.notifyException} does: a caller that reports a
     * failure this way sees "the platform refused the report" and logs it, rather than a report
     * that went nowhere looking delivered.
     *
     * <p>The real method returns without telling anyone when the component is in no flow; that
     * case returns here too.</p>
     */
    public void notifyAppMgr(final Exception exception) {
        if (getTopLevelFlow() == null) {
            return;
        }
        throw new UnsupportedOperationException(
                "notifyAppMgr is not modelled by the integration tier", exception);
    }
}
