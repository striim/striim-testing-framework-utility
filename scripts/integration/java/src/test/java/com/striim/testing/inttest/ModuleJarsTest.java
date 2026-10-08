package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.nio.file.Files;
import java.nio.file.Path;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** {@link ModuleJars#findModuleDir}: only under the project root when one is set, else the walk. */
class ModuleJarsTest {

    private static final String RELATIVE = "java/OpenProcessors/NoSuchModuleForModuleJarsTest";

    /**
     * A dir the walk finds from any framework checkout: surefire runs in this module, and walking up
     * reaches the checkout root, whose {@code scripts/integration/java} is this module.
     */
    private static final String WALKABLE = "scripts/integration/java";

    private static Path module(Path root, String relative) throws Exception {
        Path module = Files.createDirectories(root.resolve(relative));
        Files.writeString(module.resolve("pom.xml"), "<project/>");
        return module;
    }

    @Test
    void aModuleUnderTheProjectRootIsFound(@TempDir Path root) throws Exception {
        Path module = module(root, RELATIVE);

        assertEquals(module, ModuleJars.findModuleDir(RELATIVE, root.toString()));
    }

    @Test
    void theProjectRootWinsOverTheWalk(@TempDir Path root) throws Exception {
        assumeTrue(ModuleJars.findModuleDir(WALKABLE, null) != null, "the walk must find " + WALKABLE);
        Path module = module(root, WALKABLE);

        assertEquals(module, ModuleJars.findModuleDir(WALKABLE, root.toString()));
    }

    @Test
    void aSetRootLackingTheModuleIsNotAMatchEvenWhereTheWalkWouldBe(@TempDir Path root) throws Exception {
        assumeTrue(ModuleJars.findModuleDir(WALKABLE, null) != null, "the walk must find " + WALKABLE);
        Files.createDirectories(root.resolve(WALKABLE).resolve("pom.xml"));   // a DIRECTORY, not a pom

        assertNull(ModuleJars.findModuleDir(WALKABLE, root.toString()));
    }

    @Test
    void noProjectRootMeansTheWalkAlone() {
        assertNull(ModuleJars.findModuleDir(RELATIVE, null));
        assertNull(ModuleJars.findModuleDir(RELATIVE, "  "));
    }

    @Test
    void aProjectRootThatDoesNotExistIsRefused(@TempDir Path root) {
        String missing = root.resolve("no-such-dir").toString();

        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> ModuleJars.findModuleDir(RELATIVE, missing));
        assertTrue(e.getMessage().contains("does not exist"), e.getMessage());
    }

    @Test
    void aRelativeProjectRootIsRefused() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> ModuleJars.findModuleDir(RELATIVE, "."));
        assertTrue(e.getMessage().contains("not an absolute path"), e.getMessage());
    }

    @Test
    void aLeadingTildeSlashIsTheUserHome() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> ModuleJars.findModuleDir(RELATIVE, "~/no-such-dir-for-ModuleJarsTest"));
        String expanded = Path.of(System.getProperty("user.home"), "no-such-dir-for-ModuleJarsTest").toString();
        assertTrue(e.getMessage().contains("does not exist: " + expanded), e.getMessage());
        assertFalse(e.getMessage().contains("/~/"), e.getMessage());
    }

    @Test
    void anotherUsersTildeIsRefusedAsRelative() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> ModuleJars.findModuleDir(RELATIVE, "~test-user/x"));
        assertTrue(e.getMessage().contains("not an absolute path"), e.getMessage());
    }
}
