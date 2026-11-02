package com.webaction.runtime.components;

/**
 * Mock of the platform's {@code com.webaction.runtime.components.EntityType}: the kind of a
 * metadata object. {@code FlowComponent.getMetaType()} returns one, {@code Flow} recognises an
 * application by {@link #APPLICATION}, and field's MDR helpers name several.
 *
 * <p>Every constant of the real enum on 5.4.0.6G, in its order, so a reference to any of them
 * resolves. The real constants carry an int code and a set of predicates
 * ({@code isSourceOrTarget()}, {@code canBePartOfFlow()}, ...); nothing that runs in this tier
 * calls them, so they are omitted rather than stubbed.</p>
 */
public enum EntityType {
    UNKNOWN,
    APPLICATION,
    STREAM,
    WINDOW,
    TYPE,
    CQ,
    SOURCE,
    TARGET,
    FLOW,
    PROPERTYSET,
    WACTIONSTORE,
    PROPERTYTEMPLATE,
    CACHE,
    ALERTSUBSCRIBER,
    SERVER,
    USER,
    ROLE,
    INITIALIZER,
    DG,
    VISUALIZATION,
    NAMESPACE,
    EXCEPTIONHANDLER,
    STREAM_GENERATOR,
    SORTER,
    WASTOREVIEW,
    AGENT,
    DASHBOARD,
    PAGE,
    QUERYVISUALIZATION,
    QUERY,
    POSITION,
    PROPERTYVARIABLE,
    OPENPROCESSOR,
    SYSALERTRULE,
    GROUP,
    EXTERNALCACHE,
    LICENSE,
    ROUTER,
    VAULT,
    GENERIC_CDC,
    TEST_CDC,
    DATAPIPELINE,
    CONNECTIONPROFILE,
    RESOURCELIMIT,
    EXTERNALSOURCE,
    EMBEDDINGGENERATOR,
    SENTINEL,
    AIAGENT
}
