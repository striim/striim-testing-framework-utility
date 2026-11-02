package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.List;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import com.webaction.event.Event;
import com.webaction.exceptionhandling.ExceptionType;
import com.webaction.proc.events.WAEvent;
import com.webaction.runtime.ActionType;
import com.webaction.runtime.ExceptionEvent;
import com.webaction.runtime.NotifyExceptionStore;
import com.webaction.runtime.channels.Channel;
import com.webaction.runtime.components.EntityType;
import com.webaction.runtime.components.Flow;
import com.webaction.runtime.components.FlowComponent;
import com.webaction.runtime.components.Stream;
import com.webaction.runtime.containers.ITaskEvent;
import com.webaction.runtime.containers.TaskEvent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.uuid.UUID;

/**
 * The exception-store route field's {@code ExceptionStorePublisher} takes since OpenProcessorCommon
 * I9: it reads the application off the component, builds the platform's 13-argument
 * {@code ExceptionEvent} and publishes it on the exception stream's channel. Each step below is one
 * that call makes, in its order; before these stand-ins existed it died at the first, and every
 * DatabaseJdbcWriter case that skips a row failed with it.
 */
class ExceptionStoreRouteTest {

    @BeforeEach
    void drainEarlierNotifications() {
        NotifyExceptionStore.drain();
    }

    /** What {@code ExceptionStorePublisher.notify} does, written against the stand-ins. */
    private static void publishAsTheFieldPublisherDoes(FlowComponent component, Exception cause,
            String reason, Event[] events) throws Exception {
        MetaInfo.Flow app = (MetaInfo.Flow) component.getTopLevelFlow().getMetaInfo();
        assertNotNull(app.getExceptionstoreName());
        Channel channel = NotifyExceptionStore.getInstance().getExceptionStream().getChannel();
        ExceptionEvent event = new ExceptionEvent(
                ExceptionType.getExceptionType(cause, component.getMetaType()), ActionType.IGNORE,
                app.getNsName() + "." + app.getName(), app.uuid, component.getMetaFullName(),
                component.getMetaType(), cause.getClass().getName(), cause.getMessage(), 42L, reason,
                events, null, null);
        List<com.webaction.runtime.containers.WAEvent> batch = new ArrayList<>(1);
        batch.add(new com.webaction.runtime.containers.WAEvent(event));
        synchronized (NotifyExceptionStore.getInstance()) {
            channel.publish(TaskEvent.createStreamEvent(batch));
        }
    }

    @Test
    void aPublishOnTheExceptionChannelIsDrainedAsANotification() throws Exception {
        WAEvent first = new WAEvent();
        WAEvent second = new WAEvent();
        publishAsTheFieldPublisherDoes(new HarnessFlowComponent(),
                new SQLException("duplicate key", "23505"), "ignored Exception",
                new Event[] {first, second});

        List<NotifyExceptionStore.Notification> got = NotifyExceptionStore.drain();
        assertEquals(1, got.size());
        NotifyExceptionStore.Notification n = got.get(0);
        assertEquals("ignored Exception", n.reason);
        // The form the direct route recorded: String.valueOf(exception).
        assertEquals(String.valueOf(new SQLException("duplicate key", "23505")), n.cause);
        assertEquals("java.sql.SQLException: duplicate key", n.cause);
        // By identity: a case names the input events a skipped row carried by ordinal.
        assertEquals(2, n.events.size());
        assertSame(first, n.events.get(0));
        assertSame(second, n.events.get(1));
        assertTrue(NotifyExceptionStore.drain().isEmpty(), "drain empties the record");
    }

    @Test
    void aCauseWithNoMessageIsRecordedAsItsClassNameAlone() throws Exception {
        publishAsTheFieldPublisherDoes(new HarnessFlowComponent(), new IllegalStateException(),
                "ignored Exception", new Event[] {new WAEvent()});
        NotifyExceptionStore.Notification n = NotifyExceptionStore.drain().get(0);
        assertEquals(String.valueOf(new IllegalStateException()), n.cause);
    }

    @Test
    void publishExceptionAndTheDirectRouteRecordAlike() throws Exception {
        WAEvent row = new WAEvent();
        SQLException cause = new SQLException("refused", "23000");
        HarnessFlowComponent component = new HarnessFlowComponent();

        NotifyExceptionStore.getInstance().notify(component, cause, 1L, "ignored Exception",
                new Event[] {row});
        MetaInfo.Flow app = (MetaInfo.Flow) component.getTopLevelFlow().getMetaInfo();
        NotifyExceptionStore.getInstance().publishException(new ExceptionEvent(
                ExceptionType.getExceptionType(cause, null), ActionType.IGNORE, "inttest.app",
                app.uuid, null, null, cause.getClass().getName(), cause.getMessage(), 1L,
                "ignored Exception", new Event[] {row}, null, null));

        List<NotifyExceptionStore.Notification> got = NotifyExceptionStore.drain();
        assertEquals(2, got.size());
        for (NotifyExceptionStore.Notification n : got) {
            assertEquals("ignored Exception", n.reason);
            assertEquals(String.valueOf(cause), n.cause);
            assertEquals(1, n.events.size());
            assertSame(row, n.events.get(0));
        }
    }

    @Test
    void theExceptionChannelRefusesAPayloadThatIsNotAnExceptionEvent() throws Exception {
        List<com.webaction.runtime.containers.WAEvent> batch = new ArrayList<>();
        batch.add(new com.webaction.runtime.containers.WAEvent("not an exception event"));
        Channel channel = NotifyExceptionStore.getInstance().getExceptionStream().getChannel();
        assertThrows(IllegalArgumentException.class,
                () -> channel.publish(TaskEvent.createStreamEvent(batch)));
        assertTrue(NotifyExceptionStore.drain().isEmpty());
    }

    @Test
    void theHarnessApplicationDeclaresAnExceptionStore() {
        HarnessFlowComponent component = new HarnessFlowComponent();
        Flow top = component.getTopLevelFlow();
        assertNotNull(top, "the component sits in an application");
        assertSame(top, top.getTopLevelFlow(), "an application is its own top-level flow");
        MetaInfo.MetaObject meta = top.getMetaInfo();
        assertTrue(meta instanceof MetaInfo.Flow, "the publisher refuses anything else");
        assertSame(top.flowInfo, meta);
        MetaInfo.Flow app = (MetaInfo.Flow) meta;
        assertEquals(EntityType.APPLICATION, app.getType());
        assertFalse(app.getExceptionstoreName() == null || app.getExceptionstoreName().isBlank());
        assertNotNull(app.uuid, "ExceptionEvent stores the application's uuid as a string");
    }

    @Test
    void theHarnessComponentCarriesNoMetadataOfItsOwn() {
        // A writer names its logger and MBean after getMetaInfo(); null keeps the module name.
        HarnessFlowComponent component = new HarnessFlowComponent();
        assertNull(component.getMetaInfo());
        assertNull(component.getMetaType());
        assertNull(component.getMetaFullName());
        assertNull(component.getMetaID());
        assertFalse(component.isStopping());
    }

    @Test
    void notifyAppMgrRefusesInAFlowAndReturnsOutsideOne() {
        Exception failure = new RuntimeException("background thread died");
        UnsupportedOperationException refused = assertThrows(UnsupportedOperationException.class,
                () -> new HarnessFlowComponent().notifyAppMgr(failure));
        assertSame(failure, refused.getCause());
        FlowComponent detached = new FlowComponent() { };
        assertNull(detached.getTopLevelFlow());
        assertDoesNotThrow(() -> detached.notifyAppMgr(failure));
    }

    @Test
    void getTopLevelFlowWalksAsThePlatformDoes() {
        MetaInfo.Flow appInfo = HarnessFlowComponent.application();
        Flow app = new Flow(appInfo);
        MetaInfo.Flow subInfo = new MetaInfo.Flow();
        subInfo.type = EntityType.FLOW;
        Flow sub = new Flow(subInfo);
        sub.setFlow(app);

        FlowComponent inApp = new FlowComponent() { };
        inApp.setFlow(app);
        assertSame(app, inApp.getTopLevelFlow());
        FlowComponent inSubFlow = new FlowComponent() { };
        inSubFlow.setFlow(sub);
        assertSame(app, inSubFlow.getTopLevelFlow(), "a component in a sub-flow is in its application");
        Flow orphan = new Flow(subInfo);
        FlowComponent inOrphan = new FlowComponent() { };
        inOrphan.setFlow(orphan);
        // A flow that is in no flow is the top, as the real FlowComponent walk has it.
        assertSame(orphan, inOrphan.getTopLevelFlow());
        assertSame(app, app.getFlow(), "an application is its own flow");
        assertSame(app, sub.getFlow(), "a sub-flow is in its parent");
    }

    @Test
    void exceptionTypeClassifiesAsThePlatformDoes() {
        // By simple class name when it names a constant ...
        assertEquals(ExceptionType.NullPointerException,
                ExceptionType.getExceptionType(new NullPointerException(), EntityType.TARGET));
        // ... else AdapterException for a source or target, UnknownException otherwise.
        assertEquals(ExceptionType.AdapterException,
                ExceptionType.getExceptionType(new SQLException("x"), EntityType.TARGET));
        assertEquals(ExceptionType.AdapterException,
                ExceptionType.getExceptionType(new SQLException("x"), EntityType.SOURCE));
        assertEquals(ExceptionType.UnknownException,
                ExceptionType.getExceptionType(new SQLException("x"), null));
        assertEquals(ExceptionType.UnknownException, ExceptionType.getExceptionType(null, null));
    }

    @Test
    void theExceptionEventStoresItsArgumentsAsThePlatformDoes() {
        UUID appId = new UUID(0L, 7L);
        ExceptionEvent e = new ExceptionEvent(ExceptionType.AdapterException, ActionType.IGNORE,
                "ns.app", appId, "ns.target", EntityType.TARGET, "java.sql.SQLException", "boom",
                9L, "ignored Exception", null, "rel", "23505");
        assertEquals("AdapterException", e.exceptionType);
        assertEquals("IGNORE", e.action);
        assertEquals("ns.app", e.appName);
        assertEquals(appId.toString(), e.appid);
        assertEquals("ns.target", e.entityName);
        assertEquals("TARGET", e.entityType);
        assertEquals("java.sql.SQLException", e.className);
        assertEquals("boom", e.message);
        assertEquals(9L, e.epochNumber);
        assertEquals("ignored Exception", e.relatedActivity);
        assertNull(e.relatedObjects, "no events, no string, as the real setter");
        assertEquals("rel", e.relatedEntity);
        assertEquals("23505", e.exceptionCode);
        assertThrows(NullPointerException.class, () -> new ExceptionEvent(
                ExceptionType.AdapterException, ActionType.IGNORE, "ns.app", null, null, null,
                "c", "m", 1L, "r", null, null, null), "a null application uuid throws, as the real");
    }

    @Test
    void signaturesMatchJavapOfThePlatform() throws Exception {
        // Checks the stand-ins against this transcription of javap on Platform-5.4.0.6G and
        // Common-5.4.0.6G, not against the jars.
        Constructor<ExceptionEvent> ctor = ExceptionEvent.class.getConstructor(ExceptionType.class,
                ActionType.class, String.class, UUID.class, String.class, EntityType.class,
                String.class, String.class, Long.class, String.class, Event[].class, String.class,
                String.class);
        assertTrue(Modifier.isPublic(ctor.getModifiers()));

        Method stream = NotifyExceptionStore.class.getMethod("getExceptionStream");
        assertEquals(Stream.class, stream.getReturnType());
        NotifyExceptionStore.class.getMethod("publishException", ExceptionEvent.class);
        assertEquals(Channel.class, Stream.class.getMethod("getChannel").getReturnType());
        assertEquals(FlowComponent.class, Stream.class.getSuperclass());
        assertTrue(Channel.class.isInterface());
        Channel.class.getMethod("publish", ITaskEvent.class);
        Method create = TaskEvent.class.getMethod("createStreamEvent", List.class);
        assertTrue(Modifier.isStatic(create.getModifiers()));
        assertEquals(TaskEvent.class, create.getReturnType());
        assertTrue(Modifier.isAbstract(TaskEvent.class.getModifiers()));
        assertTrue(ITaskEvent.class.isAssignableFrom(TaskEvent.class));
        com.webaction.runtime.containers.WAEvent.class.getConstructor(Object.class);
        com.webaction.runtime.containers.WAEvent.class.getConstructor(Object.class,
                com.webaction.recovery.ImmutableStemma.class);
        assertEquals(Object.class, com.webaction.runtime.containers.WAEvent.class.getField("data").getType());

        assertEquals(com.webaction.runtime.components.Flow.class,
                FlowComponent.class.getMethod("getTopLevelFlow").getReturnType());
        assertEquals(MetaInfo.MetaObject.class, FlowComponent.class.getMethod("getMetaInfo").getReturnType());
        assertEquals(EntityType.class, FlowComponent.class.getMethod("getMetaType").getReturnType());
        assertEquals(String.class, FlowComponent.class.getMethod("getMetaFullName").getReturnType());
        assertEquals(UUID.class, FlowComponent.class.getMethod("getMetaID").getReturnType());
        assertEquals(boolean.class, FlowComponent.class.getMethod("isStopping").getReturnType());
        FlowComponent.class.getMethod("notifyAppMgr", Exception.class);
        assertEquals(String.class, MetaInfo.Flow.class.getMethod("getExceptionstoreName").getReturnType());
        assertEquals(String.class, MetaInfo.Flow.class.getField("exceptionstoreName").getType());
        assertEquals(EntityType.class, MetaInfo.MetaObject.class.getMethod("getType").getReturnType());
        Method classify = ExceptionType.class.getMethod("getExceptionType", Throwable.class, EntityType.class);
        assertTrue(Modifier.isStatic(classify.getModifiers()));
        assertEquals(11, ExceptionType.values().length);
        assertEquals(48, EntityType.values().length);
        assertEquals(37, ActionType.values().length);
        assertNotNull(ActionType.valueOf("IGNORE"));
    }
}
