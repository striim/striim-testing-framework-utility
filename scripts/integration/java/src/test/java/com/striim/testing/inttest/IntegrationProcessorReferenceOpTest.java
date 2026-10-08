package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The money test: drives the real, shaded {@code ReferenceOp} jar end-to-end through
 * {@link IntegrationProcessor#run} — proving the child-classloader + mock-value-type +
 * {@code Proxy(BuiltInFuncs)} + reflection wiring all work together against production
 * bytecode in an automated test.
 *
 * <p>
 * Gated on {@code STRIIM_HOME} because building the OP jar (if not already built) requires
 * a Striim install's {@code lib/} for its {@code system}-scoped dependencies (docs/INTEGRATION-TESTS.md);
 * *running* the harness itself needs no Striim install at all.
 */
class IntegrationProcessorReferenceOpTest {

    @Test
    void drivesReferenceOpAndAddsProcessedUserdata(@TempDir Path tempDir) throws Exception {
        String striimHome = System.getenv("STRIIM_HOME");
        assumeTrue(striimHome != null && !striimHome.isBlank(), "STRIIM_HOME not set; skipping the ReferenceOp end-to-end test");

        Path referenceOpDir = ModuleJars.findModuleDir("java/OpenProcessors/ReferenceOp");
        assumeTrue(referenceOpDir != null, "Could not locate java/OpenProcessors/ReferenceOp from " + System.getProperty("user.dir"));

        // ensureFreshJar folds OpenProcessorCommon's src/ into the staleness set for
        // every module automatically (ReferenceOp add-sources it into its own
        // compilation via pom.xml's add-common-source execution), so no explicit
        // extraSourceRoots argument is needed here.
        Path jar = ModuleJars.ensureFreshJarForSeries(referenceOpDir, "5.4");

        String inputJson = """
                [
                  {
                    "metadata": { "TableName": "CUSTOMER", "OperationName": "INSERT" },
                    "data": { "values": [1, "Record01"], "present": [true, true] }
                  }
                ]
                """;
        Path inputFile = tempDir.resolve("input.json");
        Path outputFile = tempDir.resolve("output.json");
        Files.writeString(inputFile, inputJson);

        IntegrationProcessor.Request request = new IntegrationProcessor.Request();
        request.opJar = jar.toAbsolutePath().toString();
        request.properties = Map.of(); // EnableInspection defaults to "false"
        request.inputFile = inputFile.toAbsolutePath().toString();
        request.outputFile = outputFile.toAbsolutePath().toString();
        request.types = null;

        IntegrationProcessor.run(request);

        assertTrue(Files.exists(outputFile), "IntegrationProcessor must write outputFile");
        List<WAEvent> output = WAEventJsonFactory.readEvents(Files.readString(outputFile));

        assertEquals(1, output.size(), "ReferenceOp is a 1-in/1-out enricher");
        WAEvent emitted = output.get(0);

        // (a) same data values/presence as the input.
        assertNotNull(emitted.data);
        assertEquals(1, emitted.data[0]);
        assertEquals("Record01", emitted.data[1]);
        assertTrue(emitted.isDataPresent(0));
        assertTrue(emitted.isDataPresent(1));

        // (b) carries userdata processed=true (ReferenceOp's Processor.processEvent stamps
        // the STRING "true", not a boolean -- see java/OpenProcessors/ReferenceOp Processor.java).
        assertEquals("true", emitted.userdata.get("processed"));
    }

    /** Also exercisable directly via {@link IntegrationProcessor#drive}, in-process, no files. */
    @Test
    void driveLowLevelApiWorksWithoutFileIo() throws Exception {
        String striimHome = System.getenv("STRIIM_HOME");
        assumeTrue(striimHome != null && !striimHome.isBlank(), "STRIIM_HOME not set; skipping the ReferenceOp end-to-end test");

        Path referenceOpDir = ModuleJars.findModuleDir("java/OpenProcessors/ReferenceOp");
        assumeTrue(referenceOpDir != null, "Could not locate java/OpenProcessors/ReferenceOp from " + System.getProperty("user.dir"));

        // ensureFreshJar folds OpenProcessorCommon's src/ into the staleness set for
        // every module automatically (ReferenceOp add-sources it into its own
        // compilation via pom.xml's add-common-source execution), so no explicit
        // extraSourceRoots argument is needed here.
        Path jar = ModuleJars.ensureFreshJarForSeries(referenceOpDir, "5.4");

        WAEvent input = WAEventJsonFactory.readEvent(
                new com.fasterxml.jackson.databind.ObjectMapper().readTree(
                        "{\"metadata\":{\"TableName\":\"CUSTOMER\",\"OperationName\":\"INSERT\"},\"data\":{\"values\":[42],\"present\":[true]}}"));

        List<WAEvent> output = IntegrationProcessor.drive(jar.toFile(), Map.of(), List.of(input), null);

        assertEquals(1, output.size());
        assertEquals("true", output.get(0).userdata.get("processed"));
        assertEquals(42, output.get(0).data[0]);
    }
}
