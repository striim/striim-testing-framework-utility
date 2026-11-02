package com.webaction.runtime.components;

import com.webaction.runtime.meta.MetaInfo;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.Flow}: the type
 * {@code RetriableWriter.getOwnerFlow()} and {@code FlowComponent.getTopLevelFlow()} return.
 * {@code flowInfo} has the real class's shape ({@code public final MetaInfo$Flow flowInfo}); the
 * real class extends {@code FlowComponent}, as this one does, and hands {@code flowInfo} to it as
 * the component's metadata, so {@code getMetaInfo()} answers the same object.
 *
 * <p>The tier builds one: the application {@code HarnessFlowComponent} sits in, which
 * {@code ExceptionStorePublisher} reads the exception store's name from. {@code Target.start},
 * which calls {@code setOwnerFlow}, is still not modelled, so a writer here sees
 * {@code getOwnerFlow() == null}, the platform's own "not on a server" answer.</p>
 */
public class Flow extends FlowComponent {

    public final MetaInfo.Flow flowInfo;

    /**
     * Harness-only. The real constructor takes a {@code BaseServer}, a parent flow and the flows
     * being deployed; module code never constructs a Flow, so it never reaches for this shape.
     * A sub-flow's parent is the flow it is attached to with {@code setFlow}.
     */
    public Flow(final MetaInfo.Flow flowInfo) {
        super(flowInfo);
        this.flowInfo = flowInfo;
    }

    /**
     * The real override: an application is its own top-level flow; a sub-flow asks its parent. A
     * sub-flow with no parent answers null, where the real one would throw (every real sub-flow
     * has a parent).
     */
    @Override
    public Flow getTopLevelFlow() {
        if (getMetaType() == EntityType.APPLICATION) {
            return this;
        }
        Flow parent = super.getFlow();
        return parent == null ? null : parent.getTopLevelFlow();
    }

    /** The real override: an application is its own flow; anything else is in its parent's. */
    @Override
    public Flow getFlow() {
        return getMetaType() == EntityType.APPLICATION ? this : super.getFlow();
    }
}
