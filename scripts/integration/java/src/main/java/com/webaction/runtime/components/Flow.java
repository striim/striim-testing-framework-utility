package com.webaction.runtime.components;

import com.webaction.runtime.meta.MetaInfo;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.Flow}: the type
 * {@code RetriableWriter.getOwnerFlow()} returns. Only {@code flowInfo} is reproduced, with the
 * real class's shape ({@code public final MetaInfo$Flow flowInfo}); the real class extends
 * {@code FlowComponent}, as this one does.
 *
 * <p>Nothing in this tier constructs one: {@code Target.start}, which calls
 * {@code setOwnerFlow}, is not modelled, so a writer here sees {@code getOwnerFlow() == null},
 * the platform's own "not on a server" answer.</p>
 */
public class Flow extends FlowComponent {

    public final MetaInfo.Flow flowInfo;

    /**
     * Harness-only. The real constructor takes a {@code BaseServer} and a parent flow; module code
     * never constructs a Flow, so it never reaches for this shape.
     */
    public Flow(final MetaInfo.Flow flowInfo) {
        this.flowInfo = flowInfo;
    }
}
