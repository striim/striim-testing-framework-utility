package com.striim.testing.inttest;

import com.webaction.runtime.components.EntityType;
import com.webaction.runtime.components.Flow;
import com.webaction.runtime.components.FlowComponent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * The flow component a harness-driven writer is attached to. It exists so the writer's
 * exception-store route ({@code common.ExceptionStoreNotifier}) has a non-null handle and sends.
 *
 * <p><b>It sits in an application that declares an exception store.</b> Since OpenProcessorCommon
 * I9 the route ({@code ExceptionStorePublisher}) reads the application off the component
 * ({@code getTopLevelFlow().getMetaInfo()}) and refuses to publish when it names no store, as the
 * platform drops such events. A case that asserts {@code exception_store} rows is a case whose
 * application, on a server, said {@code USE EXCEPTIONSTORE}; so every harness application does, by
 * default. The writer's owner flow ({@code setOwnerFlow}) stays unset:
 * setting it would send DatabaseJdbcWriter to the metadata repository, which this tier lacks.</p>
 *
 * <p><b>The component itself carries no metadata.</b> A writer names its logger and its MBean
 * after {@code getMetaInfo()} once that answers; left null, both keep the module name they had
 * before the component modelled anything, so no existing case's log or MBean changes. The
 * exception store records the component's name and type as null, which nothing asserts.</p>
 *
 * <p>The base's background-failure path reports through {@code notifyAppMgr}, which the stand-in
 * refuses (this tier has no application to halt); that path catches the refusal and logs it, as it
 * logged {@code notifyException}'s before.</p>
 */
final class HarnessFlowComponent extends FlowComponent {

    /** The namespace and name of the application every harness component sits in. */
    static final String APP_NAMESPACE = "inttest";
    static final String APP_NAME = "app";

    HarnessFlowComponent() {
        setFlow(new Flow(application()));
    }

    /** An application, with an exception store, as {@code CREATE APPLICATION ... USE EXCEPTIONSTORE}. */
    static MetaInfo.Flow application() {
        MetaInfo.Flow app = new MetaInfo.Flow();
        app.nsName = APP_NAMESPACE;
        app.name = APP_NAME;
        app.uuid = new UUID(0L, 1L);
        app.type = EntityType.APPLICATION;
        app.exceptionstoreName = APP_NAME + "_ExceptionStore";
        return app;
    }
}
