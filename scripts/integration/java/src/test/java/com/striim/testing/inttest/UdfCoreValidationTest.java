package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.File;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;

/**
 * Unit tests of {@link UdfCore#build}'s shape validation -- the "defence in depth"
 * block its own javadoc says exists because a direct-Java caller (like this test) has
 * not gone through the Python loader's own checks. Every case here fails validation
 * BEFORE the jar is ever opened, so a non-existent {@link File} is deliberately used
 * throughout: reaching a real classloading attempt would itself be a test failure.
 */
class UdfCoreValidationTest {

    private static final File FAKE_JAR = new File("/nonexistent/should-never-be-opened.jar");

    private static UdfSpec.Step step(String function, Object... args) {
        UdfSpec.Step s = new UdfSpec.Step();
        s.function = function;
        for (Object a : args) {
            s.args.add(Map.of("val", a));
        }
        return s;
    }

    @Test
    void missingClassNameThrows() {
        UdfSpec spec = new UdfSpec();
        spec.pipeline = List.of(step("f"));
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("udf.class"));
    }

    @Test
    void blankClassNameThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "   ";
        spec.pipeline = List.of(step("f"));
        assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
    }

    @Test
    void unknownKindThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.kind = "bogus";
        spec.pipeline = List.of(step("f"));
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("udf.kind"));
    }

    @Test
    void sourceSetOnWaeventKindThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.source = "data[0]";
        spec.pipeline = List.of(step("f"));
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("udf.source/udf.target"));
    }

    @Test
    void targetSetOnWaeventKindThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.target = "data[0]";
        spec.pipeline = List.of(step("f"));
        assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
    }

    @Test
    void invalidSlotOnJsonnodeKindThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.kind = "jsonnode";
        spec.source = "not-a-slot";
        spec.pipeline = List.of(step("f"));
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("udf.source"));
    }

    @Test
    void emptyPipelineThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.pipeline = List.of();
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("udf.pipeline"));
    }

    @Test
    void nullPipelineThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        spec.pipeline = null;
        assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
    }

    @Test
    void blankStepFunctionThrows() {
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.Foo";
        UdfSpec.Step bad = new UdfSpec.Step();
        bad.function = "  ";
        spec.pipeline = List.of(bad);
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(FAKE_JAR, spec, null));
        assertTrue(e.getMessage().contains("non-blank"));
    }

    @Test
    void classNotFoundNamesTheClassAndJar() {
        // The one case that DOES reach classloading -- proves the failure names both
        // the requested class and the jar, not a bare ClassNotFoundException.
        UdfSpec spec = new UdfSpec();
        spec.className = "com.example.DoesNotExist";
        spec.pipeline = List.of(step("f"));
        // A real, URL-classloadable path is needed here since the classloader does get
        // constructed -- the current working directory is always both, and certainly
        // contains no com.example.DoesNotExist.
        File cwd = new File(".");
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> UdfCore.build(cwd, spec, null));
        assertTrue(e.getMessage().contains("com.example.DoesNotExist"));
    }
}
