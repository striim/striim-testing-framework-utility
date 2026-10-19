package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.jar.Attributes;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import com.webaction.proc.events.WAEvent;
import com.webaction.recovery.ImmutableStemma;
import com.webaction.uuid.UUID;

/**
 * {@link TargetCore} delivers an event under the writer's {@code synchObject}, as the platform's
 * {@code RetriableWriter.receive} does (javap, SourceCommons-5.4.0.6C: {@code monitorenter} on
 * {@code synchObject} around {@code handleEvent}, which calls {@code processEvent}). Without it the
 * event thread races a writer's flush timer, which locks the same object.
 *
 * <p>{@code flush} is called without the lock, as the platform's {@code Target.flush} calls
 * {@code adapter.flush()}; a writer's own {@code flush} takes it.</p>
 */
class TargetCoreLockTest {

    @TempDir
    Path tempDir;

    @Test
    void processEventRunsUnderSynchObjectAndFlushDoesNot() throws Exception {
        TargetCore core = TargetCore.build(lockRecordingWriterJar(), Map.of(), List.of(), "d",
                null, true);
        try {
            core.accept(new WAEvent(), new ImmutableStemma(new UUID(0L, -1L), "d",
                    new OrdinalPosition(1)));
            core.accept(new WAEvent(), null);
            core.flush();
            // Read back through the monitor feed: PROCESSED = calls under the lock,
            // NUM_OF_EXCEPTIONS_IGNORED = calls without it, NO_OP_OPERATIONS = flush held it.
            Map<String, String> seen = core.monitorEvents();
            assertEquals("2", seen.get("PROCESSED"), seen.toString());
            assertEquals("0", seen.get("NUM_OF_EXCEPTIONS_IGNORED"), seen.toString());
            assertEquals("false", seen.get("NO_OP_OPERATIONS"), seen.toString());
        } finally {
            core.close();
        }
    }

    /** A writer that records, per call, whether the calling thread holds {@code synchObject}. */
    private File lockRecordingWriterJar() throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-writer test");

        Path srcDir = tempDir.resolve("writer-src");
        Path classesDir = tempDir.resolve("writer-classes");
        Path pkgDir = srcDir.resolve("com/example/lockwriter");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        Path writerFile = pkgDir.resolve("LockWriter.java");
        Files.writeString(writerFile, """
                package com.example.lockwriter;

                import com.webaction.event.Event;
                import com.webaction.proc.RetriableWriter;
                import com.webaction.recovery.ImmutableStemma;
                import com.webaction.runtime.monitor.MonitorEvent;
                import com.webaction.runtime.monitor.MonitorEventsCollection;
                import com.webaction.source.lib.prop.Property;
                import com.webaction.source.lib.prop.RetryPolicy;

                public class LockWriter extends RetriableWriter {
                    private int locked;
                    private int unlocked;
                    private boolean flushLocked;

                    @Override
                    public void processEvent(int channel, Event event, ImmutableStemma position) {
                        if (Thread.holdsLock(synchObject)) {
                            locked++;
                        } else {
                            unlocked++;
                        }
                    }

                    @Override
                    public void processEvent(int channel, Event event) {
                        processEvent(channel, event, null);
                    }

                    @Override
                    public void flush() {
                        flushLocked = Thread.holdsLock(synchObject);
                    }

                    @Override
                    public void publishMonitorEvents(MonitorEventsCollection events) {
                        events.add(MonitorEvent.Type.PROCESSED, (long) locked);
                        events.add(MonitorEvent.Type.NUM_OF_EXCEPTIONS_IGNORED, (long) unlocked);
                        events.add(MonitorEvent.Type.NO_OP_OPERATIONS, String.valueOf(flushLocked));
                    }

                    @Override public void cleanup() { }
                    @Override public boolean isDiscarded(Event event) { return false; }
                    @Override public RetryPolicy getRetryPolicy(Property property) { return null; }
                }
                """);

        int result = compiler.run(null, null, null,
                "-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString(), writerFile.toString());
        if (result != 0) {
            throw new IllegalStateException("Compiling the stub writer failed (exit " + result + ")");
        }

        Path jarPath = tempDir.resolve("lockwriter.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation",
                "com.example.lockwriter.LockWriter");
        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            String entry = "com/example/lockwriter/LockWriter.class";
            jarOut.putNextEntry(new JarEntry(entry));
            Files.copy(classesDir.resolve(entry), jarOut);
            jarOut.closeEntry();
        }
        return jarPath.toFile();
    }
}
