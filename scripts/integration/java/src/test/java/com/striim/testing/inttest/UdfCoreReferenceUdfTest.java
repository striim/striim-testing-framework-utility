package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Drives the real, built {@code ReferenceUdf} jar end-to-end through {@link
 * IntegrationProcessor} via {@link UdfCore}, proving the {@code kind: waevent} envelope
 * against production bytecode: a WAEvent in, a WAEvent out, {@code userdata} carried
 * through, and the null-return "no emission" contract.
 *
 * <p><b>Why ReferenceUdf and not a feature module.</b> This tier's real-jar tests
 * exist to prove the HARNESS can load and drive a real plugin -- one exemplar per code
 * path ({@link IntegrationProcessorReferenceOpTest} for the Open Processor path, this for
 * {@code kind: waevent}; {@code kind: jsonnode} has no exemplar here yet). They
 * are not module coverage; that lives in the Python tier's per-module
 * {@code regression/udf/<module>/} cases.
 *
 * <p>Pinning a harness proof to a churning feature module is therefore a category error,
 * and it bit: this test previously drove a feature UDF, and a routine
 * {@code UDF_VERSION} bump there broke the harness tier silently -- silently because this
 * tier does not run on every change. {@code ReferenceUdf} is the deliberately minimal
 * gold-shape exemplar (docs/TESTING-YOUR-JAVA.md); it exists to be stable, which is exactly what
 * a harness proof needs. Jar resolution now globs the SERIES rather than naming a version
 * ({@link ModuleJars#ensureFreshJarForSeries}), so even a bump here cannot recreate that
 * failure.
 *
 * <p>Gated on {@code STRIIM_HOME} because the module has {@code system}-scoped deps
 * (Platform/Common) that need a Striim install's {@code lib/} to BUILD. Running the
 * harness itself needs no Striim install at all.
 */
class UdfCoreReferenceUdfTest {

    private static final String SERIES = "5.4";
    private static final String MODULE = "java/UserDefinedFunctions/ReferenceUdf";

    private static Path referenceUdfJar() throws Exception {
        String striimHome = System.getenv("STRIIM_HOME");
        assumeTrue(striimHome != null && !striimHome.isBlank(),
                "STRIIM_HOME not set; skipping the ReferenceUdf end-to-end test");
        Path moduleDir = ModuleJars.findModuleDir(MODULE);
        assumeTrue(moduleDir != null,
                "Could not locate " + MODULE + " from " + System.getProperty("user.dir"));
        return ModuleJars.ensureFreshJarForSeries(moduleDir, SERIES);
    }

    @Test
    void waeventEnvelopeCarriesTheEventThroughAndStampsUserdata(@TempDir Path tempDir) throws Exception {
        Path jar = referenceUdfJar();

        String inputJson = """
                [
                  {
                    "metadata": { "TableName": "CUSTOMER", "OperationName": "UPDATE" },
                    "before": { "values": [1, "Record01", "old"], "present": [true, true, true] },
                    "data":   { "values": [1, "Record01", "new"], "present": [true, true, true] }
                  }
                ]
                """;
        Path inputFile = tempDir.resolve("input.json");
        Path outputFile = tempDir.resolve("output.json");
        Files.writeString(inputFile, inputJson);

        IntegrationProcessor.Request request = new IntegrationProcessor.Request();
        request.opJar = jar.toAbsolutePath().toString();
        request.properties = Map.of();
        request.inputFile = inputFile.toAbsolutePath().toString();
        request.outputFile = outputFile.toAbsolutePath().toString();
        request.types = Map.of("CUSTOMER", List.of("ID", "NAME", "STATUS"));

        UdfSpec udf = new UdfSpec();
        udf.className = ModuleJars.classIn(jar, "ReferenceUdf");
        // kind defaults to "waevent" -- the envelope under test.

        UdfSpec.Step step = new UdfSpec.Step();
        step.function = "ReferenceUdfMarkProcessed";
        step.args = List.of(Map.of("reg", true));
        udf.pipeline = List.of(step);
        request.udf = udf;

        IntegrationProcessor.run(request);

        assertTrue(Files.exists(outputFile));
        List<WAEvent> output = WAEventJsonFactory.readEvents(Files.readString(outputFile));
        assertEquals(1, output.size());
        WAEvent emitted = output.get(0);

        // The transform's whole contract: userdata stamped, every other field byte-identical.
        assertEquals("true", emitted.userdata.get("processed"));
        assertArrayEquals(new Object[] {1, "Record01", "new"}, emitted.data);
        assertArrayEquals(new Object[] {1, "Record01", "old"}, emitted.before);
        assertEquals("CUSTOMER", emitted.metadata.get("TableName"));
        // Presence bitmaps must survive the round trip through the envelope, not just values.
        assertTrue(emitted.isDataPresent(0) && emitted.isDataPresent(1) && emitted.isDataPresent(2));
    }

    @Test
    void waeventPipelineEndingInNullEmitsNothing(@TempDir Path tempDir) throws Exception {
        Path jar = referenceUdfJar();

        WAEvent input = WAEventJsonFactory.readEvent(
                new com.fasterxml.jackson.databind.ObjectMapper().readTree(
                        "{\"metadata\":{\"TableName\":\"CUSTOMER\"},\"data\":{\"values\":[1],\"present\":[true]}}"));

        UdfSpec udf = new UdfSpec();
        udf.className = ModuleJars.classIn(jar, "ReferenceUdf");
        UdfSpec.Step step = new UdfSpec.Step();
        step.function = "ReferenceUdfMarkProcessed";
        // A literal null arg (not "$"): the function's own `if (in == null) return null`
        // fail-safe echoes null back. No `as:`, so the register itself becomes null --
        // kind: waevent's "no emission" case (mirroring OperatorCore.processEvent's
        // null-return contract), proven end to end rather than only at the Java level.
        step.args = List.of(java.util.Collections.singletonMap("val", null));
        udf.pipeline = List.of(step);

        List<WAEvent> output = IntegrationProcessor.drive(
                jar.toFile(), Map.of(), List.of(input), null, null, null, null, udf);
        assertTrue(output.isEmpty());
    }
}
