package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
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

import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.proc.events.WAEvent;

/**
 * the {@code source:} block driven end to end, from an op JAR rather than from a
 * hand-loaded class ({@link SourceCoreTest}'s level).
 *
 * <p>Builds a minimal stub READER jar on the fly — a top-level {@code Channel} interface plus a
 * {@code Processor} with {@code tick(Channel)} — and drives it through
 * {@link IntegrationProcessor#drive}, so what is proven here is the whole path a {@code test.yaml}
 * takes: manifest to core package, sole constructor, channel discovered from the tick signature,
 * the tick budget, and the loud shortfall.</p>
 */
class IntegrationProcessorSourceTest {

    @Test
    void withNoExpectationTheBudgetIsTheExactTickCount(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 3;

        List<WAEvent> output = drive(readerJar, Map.of("EventsPerTick", "2"), source);

        // 3 ticks x 2 events: nothing stops early, because the case declared no expectation.
        assertEquals(6, output.size());
        assertEquals(1, output.get(0).userdata.get("tick"));
        assertEquals(3, output.get(5).userdata.get("tick"));
    }

    @Test
    void aQuietSourceIsDrivenTheWholeBudgetAndEmitsNothing(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 4;

        // The case that expect_events cannot express: proving a source stays quiet needs every
        // tick driven, not a stop as soon as some count is reached.
        assertEquals(List.of(), drive(readerJar, Map.of("EventsPerTick", "0"), source));
    }

    @Test
    void tickingStopsAsSoonAsTheExpectedCountIsReached(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 50;
        source.expectEvents = 3;

        List<WAEvent> output = drive(readerJar, Map.of("EventsPerTick", "1"), source);

        // Stopped at tick 3 of a 50-tick budget -- the budget is a ceiling, not a target.
        assertEquals(3, output.size());
        assertEquals(3, output.get(2).userdata.get("tick"));
    }

    @Test
    void aTickThatOvershootsTheExpectationKeepsWhatItEmitted(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 10;
        source.expectEvents = 3;

        List<WAEvent> output = drive(readerJar, Map.of("EventsPerTick", "4"), source);

        // A tick is atomic: the 4th event is NOT dropped to hit the expectation exactly. Silently
        // truncating would let a case assert an event count the source never actually produced.
        assertEquals(4, output.size());
    }

    @Test
    void anExhaustedBudgetFailsNamingBothNumbers(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 2;
        source.expectEvents = 9;

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> drive(readerJar, Map.of("EventsPerTick", "1"), source));

        // The shortfall is reported here rather than left to the WAEvent comparison, where a
        // half-delivered stream reads like an op defect instead of an under-budgeted case.
        assertTrue(e.getMessage().contains("expected at least 9"), e.getMessage());
        assertTrue(e.getMessage().contains("emitted 2"), e.getMessage());
        assertTrue(e.getMessage().contains("2 tick(s)"), e.getMessage());
    }

    @Test
    void anInStreamOpDrivenAsASourceSaysWhatIsMissing(@TempDir Path tempDir) throws Exception {
        File opJar = buildStubInStreamOpJar(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 1;

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> drive(opJar, Map.of(), source));

        assertTrue(e.getMessage().contains("has no tick(<channel interface>)"), e.getMessage());
    }

    @Test
    void inputEventsAlongsideASourceAreRejectedRatherThanIgnored(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 1;

        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.drive(readerJar, Map.of(), List.of(new WAEvent()), null,
                        null, null, null, null, source));

        assertTrue(e.getMessage().contains("has no input events"), e.getMessage());
    }

    @Test
    void aBudgetBelowOneIsRejected(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec zero = new SourceSpec();
        zero.maxTicks = 0;
        assertTrue(assertThrows(IllegalArgumentException.class,
                () -> drive(readerJar, Map.of(), zero)).getMessage().contains("at least 1"));

        SourceSpec absent = new SourceSpec();
        assertTrue(assertThrows(IllegalArgumentException.class,
                () -> drive(readerJar, Map.of(), absent)).getMessage().contains("at least 1"));
    }

    @Test
    void aThrowingCloseDoesNotReplaceTheTicksOwnFailure(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 3;

        Exception e = assertThrows(Exception.class, () -> drive(readerJar,
                Map.of("ThrowOnTick", "true", "ThrowOnClose", "true"), source));

        // The tick's exception is the one that says what went wrong; close's is kept alongside
        // it, not in place of it -- which is what a bare finally would have done.
        assertTrue(e.getMessage().contains("the source could not be read"), e.getMessage());
        assertEquals(1, e.getSuppressed().length);
        assertTrue(e.getSuppressed()[0].getMessage().contains("closing the source failed too"),
                e.getSuppressed()[0].getMessage());
    }

    @Test
    void aReaderWithAGenericSupertypeIsNotConfusedByItsBridgeMethod(@TempDir Path tempDir) throws Exception {
        // The shape slices 50-52 are building toward: a core whose tick comes from a generic
        // supertype. javac emits a BRIDGE tick(Channel) alongside the real tick(TypedChannel),
        // and getMethods() returns both -- so channel discovery must ignore synthetic methods or
        // it rejects a perfectly ordinary reader as "more than one tick".
        File readerJar = buildStubGenericReaderJar(tempDir);

        SourceSpec source = new SourceSpec();
        source.maxTicks = 2;

        List<WAEvent> output = drive(readerJar, Map.of(), source);

        assertEquals(2, output.size());
        assertEquals("generic", output.get(0).userdata.get("shape"));
    }

    @Test
    void aRequestJsonCarriesTheSourceBlockThroughRun(@TempDir Path tempDir) throws Exception {
        File readerJar = StubReaderJars.build(tempDir);
        Path inputFile = tempDir.resolve("input.json");
        Files.writeString(inputFile, "[]");
        Path outputFile = tempDir.resolve("output.json");

        // The request shape `inttest.harness.drive` writes, with the key SPELLINGS
        // `manifest.source_to_wire` produces. This is the ONLY place the two halves of the wire
        // contract meet: nothing else in either suite deserializes a Request, so without this a
        // rename on either side would compile, pass every other test, and fail only in a real case.
        String requestJson = """
                {
                  "opJar": "%s",
                  "properties": {"EventsPerTick": "2"},
                  "inputFile": "%s",
                  "outputFile": "%s",
                  "types": null,
                  "passwordProperties": null,
                  "udf": null,
                  "source": {"maxTicks": 5, "expectEvents": 4}
                }
                """.formatted(readerJar.getAbsolutePath(), inputFile, outputFile);

        IntegrationProcessor.Request request =
                new ObjectMapper().readValue(requestJson, IntegrationProcessor.Request.class);
        assertEquals(5, request.source.maxTicks);
        assertEquals(4, request.source.expectEvents);

        IntegrationProcessor.run(request);

        // 2 per tick, expectation 4 -- so it stopped after tick 2 of a 5-tick budget, and the
        // events reached the outputFile rather than only the in-process return value.
        List<WAEvent> output = WAEventJsonFactory.readEvents(Files.readString(outputFile));
        assertEquals(4, output.size());
        assertEquals(2, output.get(3).userdata.get("tick"));
    }

    @Test
    void aSourceCaseCannotAlsoBeAUdfCase() {
        SourceSpec source = new SourceSpec();
        source.maxTicks = 1;

        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> IntegrationProcessor.requireCoherentSource(source, new UdfSpec(), List.of()));

        assertTrue(e.getMessage().contains("both 'source:' and 'udf:'"), e.getMessage());
    }

    private static List<WAEvent> drive(File opJar, Map<String, Object> properties, SourceSpec source)
            throws Exception {
        return IntegrationProcessor.drive(opJar, properties, List.of(), null, null, null, null, null, source);
    }

    /**
     * A reader whose {@code tick} is inherited from a generic supertype, so the compiler emits a
     * bridge method for it.
     */
    private static File buildStubGenericReaderJar(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("generic-src");
        Path classesDir = tempDir.resolve("generic-classes");
        Path pkgDir = srcDir.resolve("com/example/stubgenericreader");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        Files.writeString(pkgDir.resolve("Channel.java"), """
                package com.example.stubgenericreader;

                import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.proc.events.WAEvent;

                public interface Channel {
                    void emit(WAEvent event) throws Exception;

                    void checkpoint(Object position);
                }
                """);
        Files.writeString(pkgDir.resolve("TypedChannel.java"), """
                package com.example.stubgenericreader;

                public interface TypedChannel extends Channel {
                }
                """);
        Files.writeString(pkgDir.resolve("AbstractReader.java"), """
                package com.example.stubgenericreader;

                public abstract class AbstractReader<C extends Channel> {
                    public abstract void tick(C channel) throws Exception;
                }
                """);
        Files.writeString(pkgDir.resolve("Processor.java"), """
                package com.example.stubgenericreader;

                import java.util.Map;

                import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.proc.events.WAEvent;

                public class Processor extends AbstractReader<TypedChannel> {
                    public Processor(Map<String, Object> props) {
                    }

                    @Override
                    public void tick(TypedChannel channel) throws Exception {
                        WAEvent event = new WAEvent();
                        event.putUserdata("shape", "generic");
                        channel.emit(event);
                    }
                }
                """);

        int compileResult = compiler.run(null, null, null,
                "-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString(),
                pkgDir.resolve("Channel.java").toString(),
                pkgDir.resolve("TypedChannel.java").toString(),
                pkgDir.resolve("AbstractReader.java").toString(),
                pkgDir.resolve("Processor.java").toString());
        if (compileResult != 0) {
            throw new IllegalStateException("Compiling the generic stub reader failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("stubgenericreader.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation",
                "com.example.stubgenericreader.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            for (String name : List.of("Channel.class", "TypedChannel.class", "AbstractReader.class", "Processor.class")) {
                String entryName = "com/example/stubgenericreader/" + name;
                jarOut.putNextEntry(new JarEntry(entryName));
                Files.copy(classesDir.resolve(entryName), jarOut);
                jarOut.closeEntry();
            }
        }

        return jarPath.toFile();
    }

    /**
     * An in-stream stub: a {@code processEvent} core with no {@code tick} at all, so a
     * {@code source:} case pointed at it hits the discovery failure rather than a reflection error.
     */
    private static File buildStubInStreamOpJar(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-op-jar test");

        Path srcDir = tempDir.resolve("instream-src");
        Path classesDir = tempDir.resolve("instream-classes");
        Path pkgDir = srcDir.resolve("com/example/stubinstream");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        Path processorFile = pkgDir.resolve("Processor.java");
        Files.writeString(processorFile, """
                package com.example.stubinstream;

                import java.util.List;
                import java.util.Map;

                import com.fasterxml.jackson.databind.ObjectMapper;
import com.webaction.proc.events.WAEvent;

                public class Processor {
                    public Processor(Map<String, Object> props) {
                    }

                    public List<WAEvent> processEvent(WAEvent e) {
                        return List.of(e);
                    }
                }
                """);

        int compileResult = compiler.run(null, null, null,
                "-cp", System.getProperty("java.class.path"),
                "-d", classesDir.toString(),
                processorFile.toString());
        if (compileResult != 0) {
            throw new IllegalStateException("Compiling the stub in-stream op failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("stubinstream.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation",
                "com.example.stubinstream.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            String entryName = "com/example/stubinstream/Processor.class";
            jarOut.putNextEntry(new JarEntry(entryName));
            Files.copy(classesDir.resolve(entryName), jarOut);
            jarOut.closeEntry();
        }

        return jarPath.toFile();
    }

}
