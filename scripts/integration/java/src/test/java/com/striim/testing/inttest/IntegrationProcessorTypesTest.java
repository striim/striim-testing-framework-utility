package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
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

import com.webaction.proc.events.WAEvent;

/**
 * Proves the {@code types:} schema harness capability (docs/INTEGRATION-TESTS.md): that
 * {@link IntegrationProcessor#drive} re-stamps an input event's {@code typeUUID} from
 * {@code request.types} and that the mock {@code BuiltInFuncs}/{@code TypeResolver}
 * proxies actually serve field/alias introspection and type creation off it, rather than
 * stubbing/throwing.
 *
 * <p>
 * Builds a minimal type-consuming "stub op" jar on the fly (mirroring
 * {@link IntegrationProcessorReservedKeysTest}'s on-the-fly stub-jar pattern), whose
 * {@code Processor} declares the canonical type-consuming Gold Standard constructor
 * {@code (Map, BuiltInFuncs, TypeResolver, Logger)}. Because this harness module never
 * compiles against {@code OpenProcessorCommon} (see {@code IntegrationProcessor}'s class
 * javadoc), the stub jar carries its own local {@code BuiltInFuncs}/{@code
 * TypeResolver}/{@code Logger} declarations — exactly what a real OP's shaded-in copy of
 * those contracts looks like from the harness's point of view. {@code processEvent}
 * echoes what it observed from the injected seams into {@code userdata} so the test can
 * assert on it without any reflection into the child classloader.
 */
class IntegrationProcessorTypesTest {

    @Test
    void driveSeedsFieldIntrospectionAndTypeCreationFromRequestTypes(@TempDir Path tempDir) throws Exception {
        File stubJar = buildStubTypeOpJar(tempDir);

        WAEvent input = new WAEvent();
        input.metadata.put("TableName", "SRC.TBL");

        Map<String, Object> types = Map.of("SRC.TBL", List.of("COL_A", "COL_B", "COL_C"));

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), types, "ns", "src");

        assertEquals(1, output.size());
        WAEvent emitted = output.get(0);
        assertEquals(3, emitted.userdata.get("fieldCount"));
        assertEquals("COL_A", emitted.userdata.get("alias0"));
        assertEquals(true, emitted.userdata.get("typeUuidPresent"));
    }

    @Test
    void getTypeByUuidResolvesTheDeclaredSourceSchema(@TempDir Path tempDir) throws Exception {
        // Before type lookup was implemented, getTypeByUUID returned null and a type-consuming core could
        // not run under the harness AT ALL -- a re-typing operator's consistency guard fires on a null
        // source type, so every column-filtering case would have failed for a harness reason.
        File stubJar = buildStubTypeOpJar(tempDir);

        WAEvent input = new WAEvent();
        input.metadata.put("TableName", "SRC.TBL");
        Map<String, Object> types = Map.of("SRC.TBL", List.of("COL_A", "COL_B", "COL_C"));

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), types, "ns", "src");

        WAEvent emitted = output.get(0);
        assertEquals(true, emitted.userdata.get("sourceResolved"),
                "the UUID drive() stamped onto the event must resolve back to its declared schema");
        assertEquals("COL_A,COL_B,COL_C", emitted.userdata.get("sourceColumns"),
                "columns must arrive in DECLARATION order -- an index-mapping core reads position");
        assertEquals(0, emitted.userdata.get("sourceKeyCount"),
                "keyFields is empty rather than absent: the field must exist for a reflective read");
        assertEquals(true, emitted.userdata.get("byNameResolved"),
                "a declared table resolves by (namespace, name) too");
        assertEquals(true, emitted.userdata.get("unknownIsNull"),
                "an UNdeclared type still resolves to null -- that is what keeps a core's "
                        + "create-if-absent path working, and what keeps this change additive");
        assertEquals("SRC.TBL", emitted.userdata.get("sourceIdentity"),
                "nsName/name must be populated: a core builds its derived type name from them, "
                        + "and a mock missing them raises NoSuchFieldError -- an Error no operator "
                        + "catch (Exception) will stop");
    }

    @Test
    void aCreatedTypeCanBeResolvedBackByUuidAndByName(@TempDir Path tempDir) throws Exception {
        // A real MDR registers what createType creates, and cores rely on it: a re-typing operator
        // re-resolves its own filtered type to enrich the event further, and re-verifies cached
        // types by UUID before reuse. A resolver that forgets returns null both times, so the
        // second transform silently does nothing -- a harness-manufactured wrong answer, with no
        // exception and no log, which is the worst thing an integration tier can produce.
        File stubJar = buildStubTypeOpJar(tempDir);

        WAEvent input = new WAEvent();
        input.metadata.put("TableName", "SRC.TBL");
        Map<String, Object> types = Map.of("SRC.TBL", List.of("COL_A", "COL_B", "COL_C"));

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), types, "ns", "src");

        WAEvent emitted = output.get(0);
        assertEquals(true, emitted.userdata.get("createdReadBack"),
                "a type this resolver created must resolve back by its own UUID");
        assertEquals(true, emitted.userdata.get("createdByNameResolved"),
                "and by the name it was created under");
        assertEquals("M1", emitted.userdata.get("createdFields"),
                "the readback must carry the fields it was asked to create, not a bare UUID");
        assertEquals("M1", emitted.userdata.get("createdKeys"),
                "including which of them are key fields");
    }

    @Test
    void withNoTypesBlockTheResolverStillReturnsNull(@TempDir Path tempDir) throws Exception {
        // The additivity pin: every case that declares no types: must behave exactly as it did
        // before type lookup was implemented, which for a type-consuming core means null and its create path.
        File stubJar = buildStubTypeOpJar(tempDir);

        WAEvent input = new WAEvent();
        input.metadata.put("TableName", "SRC.TBL");

        List<WAEvent> output = IntegrationProcessor.drive(stubJar, Map.of(), List.of(input), null, "ns", "src");

        WAEvent emitted = output.get(0);
        assertEquals(false, emitted.userdata.get("sourceResolved"));
        assertEquals(true, emitted.userdata.get("unknownIsNull"));
    }

    /**
     * Compiles a tiny type-consuming stub op (local {@code BuiltInFuncs}/{@code
     * TypeResolver}/{@code Logger} + a {@code Processor} declaring the canonical
     * {@code (Map, BuiltInFuncs, TypeResolver, Logger)} constructor) against this module's
     * own compiled classes (so its {@code WAEvent}/{@code UUID}/{@code MetaInfo}
     * references resolve to the same mock classes this test uses) and packs it into a jar
     * whose manifest declares a {@code Striim-Service-Implementation} — the App class
     * named there need not actually exist; {@link IntegrationProcessor} only derives the
     * core's package from it.
     */
    private static File buildStubTypeOpJar(Path tempDir) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available; skipping stub-type-op-jar test");

        Path srcDir = tempDir.resolve("src");
        Path classesDir = tempDir.resolve("classes");
        Path pkgDir = srcDir.resolve("com/example/stubtypeop");
        Files.createDirectories(pkgDir);
        Files.createDirectories(classesDir);

        writeSource(pkgDir, "BuiltInFuncs.java", """
                package com.example.stubtypeop;

                import java.lang.reflect.Field;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;
                import com.webaction.uuid.UUID;

                public interface BuiltInFuncs {
                    Field[] getFieldsArray(UUID sourceTypeUUID);

                    Map<String, String> getAliasFieldName(UUID sourceTypeUUID);

                    boolean IS_PRESENT(WAEvent event, Object[] image, int index);
                }
                """);

        writeSource(pkgDir, "TypeResolver.java", """
                package com.example.stubtypeop;

                import java.util.Map;

                import com.webaction.runtime.meta.MetaInfo;
                import com.webaction.uuid.UUID;

                public interface TypeResolver {
                    MetaInfo.Type getTypeByUUID(UUID uuid) throws Exception;

                    MetaInfo.Type getTypeByName(String namespace, String name) throws Exception;

                    MetaInfo.Type createType(String typeName, Map<String, String> fields,
                            Map<String, String> fieldAliases, Map<String, Boolean> fieldKeys) throws Exception;
                }
                """);

        writeSource(pkgDir, "Logger.java", """
                package com.example.stubtypeop;

                public class Logger {
                    public Logger(String tag, boolean enableDebugLogging) {
                    }
                }
                """);

        writeSource(pkgDir, "Processor.java", """
                package com.example.stubtypeop;

                import java.lang.reflect.Field;
                import java.util.List;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;
                import com.webaction.runtime.meta.MetaInfo;

                public class Processor {
                    private final Map<String, Object> props;
                    private final BuiltInFuncs funcs;
                    private final TypeResolver typeResolver;
                    private final Logger logger;

                    public Processor(Map<String, Object> props, BuiltInFuncs funcs, TypeResolver typeResolver, Logger logger) {
                        this.props = props;
                        this.funcs = funcs;
                        this.typeResolver = typeResolver;
                        this.logger = logger;
                    }

                    public List<WAEvent> processEvent(WAEvent event) throws Exception {
                        WAEvent copy = WAEvent.makeCopy(event);
                        Field[] fields = funcs.getFieldsArray(event.typeUUID);
                        Map<String, String> aliases = funcs.getAliasFieldName(event.typeUUID);
                        MetaInfo.Type type = typeResolver.createType(
                                "ns.T", Map.of("C", "java.lang.String"), Map.of(), Map.of("C", false));
                        copy.putUserdata("fieldCount", fields.length);
                        copy.putUserdata("alias0", aliases.get("f0"));
                        copy.putUserdata("typeUuidPresent", type.getUuid() != null);

                        // Resolve the SOURCE type and read it exactly as a real core does --
                        // reflectively, by field name. A getter would pass here and fail in
                        // production, so the test must not take a shortcut the fleet cannot.
                        MetaInfo.Type source = typeResolver.getTypeByUUID(event.typeUUID);
                        if (source == null) {
                            copy.putUserdata("sourceResolved", false);
                        } else {
                            java.lang.reflect.Field f = source.getClass().getField("fields");
                            Map<String, String> declared = (Map<String, String>) f.get(source);
                            copy.putUserdata("sourceResolved", true);
                            copy.putUserdata("sourceColumns", String.join(",", declared.keySet()));
                            java.lang.reflect.Field kf = source.getClass().getField("keyFields");
                            copy.putUserdata("sourceKeyCount", ((List<String>) kf.get(source)).size());
                        }
                        // Read the DIRECT fields too, not just the reflective ones: a real core
                        // builds a derived type name as nsName + "." + name, and a mock missing
                        // those raises NoSuchFieldError -- an Error, which sails past every
                        // catch (Exception) in the operator.
                        if (source != null) {
                            copy.putUserdata("sourceIdentity", source.nsName + "." + source.name);
                        }

                        // Create a type, then RESOLVE IT BACK, the way a core re-reads a type it
                        // just made in order to enrich the event further, and the way it verifies
                        // a cached type before reuse. A resolver that forgets what it minted
                        // makes that silently return null.
                        MetaInfo.Type made = typeResolver.createType(
                                "ns.MADE", Map.of("M1", "java.lang.String"), Map.of(), Map.of("M1", true));
                        MetaInfo.Type readBack = typeResolver.getTypeByUUID(made.getUuid());
                        copy.putUserdata("createdReadBack", readBack != null);
                        if (readBack != null) {
                            java.lang.reflect.Field rf = readBack.getClass().getField("fields");
                            copy.putUserdata("createdFields",
                                    String.join(",", ((Map<String, String>) rf.get(readBack)).keySet()));
                            java.lang.reflect.Field rk = readBack.getClass().getField("keyFields");
                            copy.putUserdata("createdKeys", String.join(",", (List<String>) rk.get(readBack)));
                        }
                        copy.putUserdata("createdByNameResolved",
                                typeResolver.getTypeByName("ns", "MADE") != null);

                        MetaInfo.Type byName = typeResolver.getTypeByName("SRC", "TBL");
                        copy.putUserdata("byNameResolved", byName != null);
                        MetaInfo.Type unknown = typeResolver.getTypeByName("ns", "NotDeclared");
                        copy.putUserdata("unknownIsNull", unknown == null);
                        return List.of(copy);
                    }

                    public void close() {
                    }
                }
                """);

        String classpath = System.getProperty("java.class.path");
        int compileResult = compiler.run(null, null, null,
                "-cp", classpath,
                "-d", classesDir.toString(),
                pkgDir.resolve("BuiltInFuncs.java").toString(),
                pkgDir.resolve("TypeResolver.java").toString(),
                pkgDir.resolve("Logger.java").toString(),
                pkgDir.resolve("Processor.java").toString());
        if (compileResult != 0) {
            throw new IllegalStateException("Compiling stub type op sources failed (exit " + compileResult + ")");
        }

        Path jarPath = tempDir.resolve("stubtypeop.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation", "com.example.stubtypeop.App");

        try (JarOutputStream jarOut = new JarOutputStream(Files.newOutputStream(jarPath), manifest)) {
            for (String className : new String[] { "BuiltInFuncs", "TypeResolver", "Logger", "Processor" }) {
                Path classFile = classesDir.resolve("com/example/stubtypeop/" + className + ".class");
                String entryName = "com/example/stubtypeop/" + className + ".class";
                jarOut.putNextEntry(new JarEntry(entryName));
                Files.copy(classFile, jarOut);
                jarOut.closeEntry();
            }
        }

        return jarPath.toFile();
    }

    private static void writeSource(Path pkgDir, String fileName, String source) throws IOException {
        Files.writeString(pkgDir.resolve(fileName), source);
    }
}
