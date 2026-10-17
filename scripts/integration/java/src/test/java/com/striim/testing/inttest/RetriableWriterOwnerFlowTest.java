package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.webaction.event.Event;
import com.webaction.proc.RetriableWriter;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.runtime.components.Flow;
import com.webaction.runtime.components.FlowComponent;
import com.webaction.runtime.meta.MetaInfo;
import com.webaction.source.lib.prop.Property;
import com.webaction.source.lib.prop.RetryPolicy;
import com.webaction.uuid.UUID;

/**
 * {@code getOwnerFlow()}, which DatabaseJdbcWriter's {@code init} calls. Absent from the mock, every
 * DatabaseJdbcWriter case died with {@code NoSuchMethodError} at {@code App.init}.
 */
class RetriableWriterOwnerFlowTest {

    /** Reads the owner flow in init, as DatabaseJdbcWriter's {@code ownerAppInfo()} does. */
    static final class OwnerReadingWriter extends RetriableWriter {
        boolean initRan;
        MetaInfo.Flow ownerApp = new MetaInfo.Flow();

        @Override
        public void init(Map<String, Object> properties, Map<String, Object> formatterProperties,
                UUID inputStream, String distributionID) throws Exception {
            super.init(properties, formatterProperties, inputStream, distributionID);
            Flow owner = getOwnerFlow();
            ownerApp = owner == null ? null : owner.flowInfo;
            initRan = true;
        }

        @Override public void processEvent(int channel, Event event, ImmutableStemma position) { }
        @Override public void processEvent(int channel, Event event) { }
        @Override public void cleanup() { }
        @Override public boolean isDiscarded(Event event) { return false; }
        @Override public RetryPolicy getRetryPolicy(Property property) { return null; }
    }

    @Test
    void ownerFlowIsNullByDefaultLikeAWriterOffAServer() throws Exception {
        // This tier does not model Target.start, the only caller of setOwnerFlow.
        OwnerReadingWriter writer = new OwnerReadingWriter();
        writer.init(Map.of(), Map.of(), null, null);
        assertTrue(writer.initRan);
        assertNull(writer.ownerApp);
    }

    @Test
    void setOwnerFlowRoundTripsToFlowInfo() throws Exception {
        MetaInfo.Flow app = new MetaInfo.Flow();
        OwnerReadingWriter writer = new OwnerReadingWriter();
        writer.setOwnerFlow(new Flow(app));
        writer.init(Map.of(), Map.of(), null, null);
        assertSame(app, writer.ownerApp);
    }

    @Test
    void signaturesMatchJavapOfThePlatform() throws Exception {
        // Checks the mock against this transcription, not the platform jar.
        // BaseProcess on 5.4.0.6C (javap): public Flow getOwnerFlow(); public void
        // setOwnerFlow(Flow); protected Flow ownerFlow. Flow extends FlowComponent and carries
        // public final MetaInfo$Flow flowInfo; MetaInfo$Flow extends MetaInfo$MetaObject.
        Method get = RetriableWriter.class.getMethod("getOwnerFlow");
        assertEquals(Flow.class, get.getReturnType());
        assertTrue(Modifier.isPublic(get.getModifiers()));
        RetriableWriter.class.getMethod("setOwnerFlow", Flow.class);
        assertTrue(Modifier.isProtected(
                RetriableWriter.class.getDeclaredField("ownerFlow").getModifiers()));

        assertEquals(FlowComponent.class, Flow.class.getSuperclass());
        Field info = Flow.class.getField("flowInfo");
        assertEquals(MetaInfo.Flow.class, info.getType());
        assertTrue(Modifier.isFinal(info.getModifiers()));
        assertEquals(MetaInfo.MetaObject.class, MetaInfo.Flow.class.getSuperclass());
    }
}
