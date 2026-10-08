package com.striim.testing.inttest;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.util.ArrayList;
import java.util.List;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
import java.util.stream.Stream;

/**
 * Shared plumbing for the tests in this package that drive a REAL, built plugin jar
 * ({@link IntegrationProcessorReferenceOpTest}, {@link UdfCoreReferenceUdfTest}, and a project's
 * own tests under {@code scripts/integration/java-field}) rather than a synthetic one.
 *
 * <p>The build-or-reuse decision is mtime-based, deliberately mirroring the Python
 * side's {@code opartifacts._stale_trigger} / {@code harness._is_stale}: reuse the jar
 * in {@code target/} only when it is newer than every file under the module's own
 * {@code src/} tree, its {@code pom.xml}, and any extra source roots the module
 * add-sources in. An unconditional reuse (the pre-fix behaviour) silently drives the OLD
 * bytecode against the NEW test, turning a rename or a signature change into a
 * misleading value assertion instead of a clear "stale jar" signal -- the failure mode
 * {@code opartifacts}'s docstring already warns about ("silently ships a stale jar").
 *
 * <p>Self-memoizing across call sites: the first caller in a JVM rebuilds, which makes
 * the jar newer than all sources, so the remaining callers in the same run see a fresh
 * jar and skip. One {@code mvn package} per module per test run, not one per test.
 *
 * <p>Known limitation, matching the Python precedent: {@code system}-scoped deps
 * resolved out of a Striim install's {@code lib/} (a UDF's Platform/Common/kryo) are
 * NOT in the staleness set. Changing STRIIM_HOME still needs a manual rebuild.
 */
final class ModuleJars {

    private ModuleJars() {
    }

    /**
     * The module dir {@code <relative>} with a {@code pom.xml}. When {@code SLT_PROJECT_ROOT} is set
     * (the consumer checkout that holds the modules), only under it, as the Python side resolves
     * {@code jar:} refs; unset, the first match walking up from {@code user.dir} (up to 8
     * levels). Returns null when not found, so callers can {@code assumeTrue} out.
     *
     * @throws IllegalStateException if {@code SLT_PROJECT_ROOT} is set but is not an absolute
     *         path (a leading {@code ~/} expands to {@code user.home}) or names no directory. A
     *         relative value is refused because surefire runs with this module as its working
     *         directory, so it would not mean what it means to the shell that set it.
     */
    /**
     * The fully qualified name of the class {@code simpleName} in {@code jar}: a project names
     * its own package (com.example in the template), so a test reads it from the built jar
     * rather than hard-coding one. Shaded copies are skipped.
     */
    static String classIn(Path jar, String simpleName) throws IOException {
        try (JarFile jf = new JarFile(jar.toFile())) {
            return jf.stream().map(JarEntry::getName)
                    .filter(n -> n.equals(simpleName + ".class") || n.endsWith("/" + simpleName + ".class"))
                    .filter(n -> !n.contains("/shaded/"))
                    .sorted().findFirst()
                    .map(n -> n.substring(0, n.length() - ".class".length()).replace('/', '.'))
                    .orElseThrow(() -> new IllegalStateException(simpleName + ".class is not in " + jar));
        }
    }

    static Path findModuleDir(String relative) {
        return findModuleDir(relative, System.getenv("SLT_PROJECT_ROOT"));
    }

    static Path findModuleDir(String relative, String projectRoot) {
        if (projectRoot != null && !projectRoot.isBlank()) {
            Path candidate = projectRootPath(projectRoot.strip()).resolve(relative);
            return Files.isRegularFile(candidate.resolve("pom.xml")) ? candidate : null;
        }
        Path dir = Path.of(System.getProperty("user.dir")).toAbsolutePath();
        for (int i = 0; i < 8 && dir != null; i++, dir = dir.getParent()) {
            Path candidate = dir.resolve(relative);
            if (Files.isRegularFile(candidate.resolve("pom.xml"))) {
                return candidate;
            }
        }
        return null;
    }

    private static Path projectRootPath(String value) {
        String expanded = value.equals("~") || value.startsWith("~/")
                ? System.getProperty("user.home") + value.substring(1) : value;
        Path root = Path.of(expanded);
        if (!root.isAbsolute()) {
            throw new IllegalStateException("SLT_PROJECT_ROOT=" + value + " is not an absolute path"
                    + " (only a leading ~/ is expanded). Set an absolute path, or unset it.");
        }
        root = root.normalize();
        if (!Files.isDirectory(root)) {
            throw new IllegalStateException("SLT_PROJECT_ROOT=" + value + " does not exist: " + root
                    + ". Fix it, or unset SLT_PROJECT_ROOT.");
        }
        return root;
    }

    /**
     * Return {@code moduleDir/target/<jarName>}, running {@code mvn -q -DskipTests=true
     * package} first when the jar is missing or older than any tracked source.
     *
     * <p>{@code OpenProcessorCommon}'s {@code src/} is folded into the staleness set
     * unconditionally (unless {@code moduleDir} IS Common) -- mirroring {@code
     * opartifacts._stale_trigger} exactly, not just for callers that remember to ask:
     * it add-sources into every op's build (SPEC §8's "build-staleness gotcha"), and per
     * that function's own reasoning, a false-positive rebuild for a module that doesn't
     * actually depend on Common is harmless (a no-op {@code mvn package}), while a false
     * negative silently ships a stale jar -- exactly the bug class this class exists to
     * close. If Common can't be located (e.g. this test tier run from outside the full
     * repo checkout), that half of the check is skipped rather than failing the caller.
     *
     * @param extraSourceRoots additional directories add-source'd into this module's
     *                         compilation beyond its own {@code src/} and Common's.
     *                         Non-existent roots are skipped.
     */
    static Path ensureFreshJar(Path moduleDir, String jarName, Path... extraSourceRoots)
            throws IOException, InterruptedException {
        Path jar = moduleDir.resolve("target/" + jarName);
        return ensureFresh(moduleDir, jar, jarName, extraSourceRoots);
    }

    /**
     * Series-globbing sibling of {@link #ensureFreshJar}: resolves the module's shaded jar as
     * {@code target/*-<series>.jar} instead of naming its version, mirroring the Python side's
     * {@code opartifacts._release_jar_candidates} exactly.
     *
     * <p>Naming the file in full couples every caller to a module's {@code UDF_VERSION}/{@code
     * OP_VERSION}, so a version bump breaks these tests -- and because this tier does not run on
     * every change, it breaks them SILENTLY until someone happens to invoke it. That is not
     * hypothetical: a UDF's V1C->V1D bump did exactly this, and every other tier stayed
     * green throughout. Globbing on the series removes the coupling entirely; the series moves
     * once per Striim minor, in lockstep across every module, and is still asserted rather than
     * guessed.
     *
     * <p>A {@code target/} holding more than one match is treated as needing a clean rebuild
     * (again mirroring the Python precedent) rather than picked from arbitrarily: two jars means a
     * stale one from a previous version is still present, and choosing wrong drives the OLD
     * bytecode against the NEW test -- the exact failure this class exists to prevent.
     */
    static Path ensureFreshJarForSeries(Path moduleDir, String series, Path... extraSourceRoots)
            throws IOException, InterruptedException {
        Path existing = globSeriesJar(moduleDir, series);
        // existing == null means EITHER no jar yet OR several (a stale one from a previous
        // version bump alongside the current one). Both need a build whose output name we
        // cannot predict -- naming it would re-couple this to UDF_VERSION/OP_VERSION, which
        // is the coupling this method exists to remove -- so the post-build check is left to
        // the re-glob below rather than asserted against a placeholder path.
        //
        // The AMBIGUOUS case additionally needs `clean`: a plain `package` writes the new jar
        // BESIDE the stale one, so target/ is still ambiguous afterwards and the next run
        // fails identically. That is what the javadoc above means by "treated as needing a
        // clean rebuild", and it is what the Python precedent (opartifacts.build_jar) does.
        boolean ambiguous = existing == null && countSeriesJars(moduleDir, series) > 1;
        Path jar = existing != null ? existing : moduleDir.resolve("target/UNBUILT-" + series + ".jar");
        Path built = ensureFresh(moduleDir, jar, "*-" + series + ".jar", existing != null, ambiguous,
                extraSourceRoots);
        // After a rebuild the name is knowable only by globbing again.
        Path after = globSeriesJar(moduleDir, series);
        boolean recovered = false;
        if (after == null && countSeriesJars(moduleDir, series) > 1) {
            // A VERSION BUMP lands here, and the ambiguity check above cannot catch it.
            //
            // After a bump target/ holds exactly ONE jar -- the previous version's -- so
            // globSeriesJar returns it, `existing != null`, and `clean` was therefore false.
            // Sources are newer than that stale jar, so `package` ran and wrote the NEW name
            // BESIDE the old one. Only now is target/ ambiguous, and the pre-build check has
            // already been and gone.
            //
            // Before this, the run failed with "Build succeeded but no unambiguous jar" and
            // stayed failed: every retry reproduced it, because nothing ever removed the stale
            // jar. Worse, the state is self-healing under `rm -rf target`, so it did not
            // reproduce once anyone cleaned -- which is exactly how it survived.
            ensureFresh(moduleDir, moduleDir.resolve("target/UNBUILT-" + series + ".jar"),
                    "*-" + series + ".jar", false, true, extraSourceRoots);
            after = globSeriesJar(moduleDir, series);
            recovered = true;
        }
        if (after == null) {
            throw new IllegalStateException(
                    "Build succeeded but no unambiguous target/*-" + series + ".jar in " + moduleDir
                            + " (found " + countSeriesJars(moduleDir, series) + "; a clean rebuild "
                            + "was already attempted)");
        }
        // `recovered` is why this is not just `built.equals(existing) ? built : after`. In the
        // version-bump case `built` IS `existing` -- the OLD jar, which the recovery's `clean` has
        // just DELETED -- so returning it hands the caller a path that no longer exists. Callers
        // use the path directly (IntegrationProcessor: "opJar does not exist or is not a file"),
        // so the run still failed, just with a different message than before the recovery existed.
        // Verified by reconstructing the post-bump state: before this line, that error; after it,
        // the case passes.
        if (recovered) {
            return after;
        }
        // Belt and braces on the same class of bug: `ensureFresh` returns its `jar` argument on
        // every path, so `built` is whatever was passed in and nothing here has checked that it
        // still exists. Deciding on the FILE rather than on the path keeps that impossible by
        // construction, instead of relying on nobody adding a build step that renames the jar.
        return Files.isRegularFile(built) ? built : after;
    }

    /** How many {@code target/*-<series>.jar} there are -- 0, 1, or several (ambiguous). */
    private static int countSeriesJars(Path moduleDir, String series) throws IOException {
        Path target = moduleDir.resolve("target");
        if (!Files.isDirectory(target)) {
            return 0;
        }
        try (Stream<Path> paths = Files.list(target)) {
            return (int) paths
                    .filter(p -> p.getFileName().toString().endsWith("-" + series + ".jar"))
                    .filter(p -> !p.getFileName().toString().startsWith("original-"))
                    .count();
        }
    }

    /** The single {@code target/*-<series>.jar}, or null when absent or ambiguous. */
    private static Path globSeriesJar(Path moduleDir, String series) throws IOException {
        Path target = moduleDir.resolve("target");
        if (!Files.isDirectory(target)) {
            return null;
        }
        try (Stream<Path> paths = Files.list(target)) {
            List<Path> matches = paths
                    .filter(p -> p.getFileName().toString().endsWith("-" + series + ".jar"))
                    .filter(p -> !p.getFileName().toString().startsWith("original-"))
                    .sorted()
                    .toList();
            return matches.size() == 1 ? matches.get(0) : null;
        }
    }

    private static Path ensureFresh(Path moduleDir, Path jar, String jarName, Path... extraSourceRoots)
            throws IOException, InterruptedException {
        return ensureFresh(moduleDir, jar, jarName, true, false, extraSourceRoots);
    }

    /**
     * @param nameIsKnown whether {@code jar} is a real resolved path (assert it after the
     *                    build) or a placeholder standing in for a name only a re-glob can
     *                    determine (do not assert it -- the caller re-globs).
     * @param clean       prepend {@code clean} to the maven goals, which is mandatory when
     *                    target/ already holds a stale jar for this series: a plain
     *                    {@code package} leaves it in place and the ambiguity persists.
     */
    private static Path ensureFresh(Path moduleDir, Path jar, String jarName, boolean nameIsKnown,
            boolean clean, Path... extraSourceRoots)
            throws IOException, InterruptedException {
        List<Path> roots = new ArrayList<>(List.of(extraSourceRoots));
        Path commonDir = findModuleDir("java/OpenProcessors/OpenProcessorCommon");
        if (commonDir != null && !commonDir.toAbsolutePath().normalize()
                .equals(moduleDir.toAbsolutePath().normalize())) {
            roots.add(commonDir.resolve("src"));
        }
        Path trigger = staleTrigger(jar, moduleDir, roots.toArray(new Path[0]));
        if (trigger == null) {
            return jar;
        }
        System.out.println("[inttest] rebuilding " + jarName + " in " + moduleDir
                + " -- stale against " + trigger);

        String mvn = System.getProperty("os.name", "").toLowerCase().contains("win") ? "mvn.cmd" : "mvn";
        List<String> goals = new ArrayList<>(List.of(mvn, "-q", "-DskipTests=true"));
        if (clean) {
            goals.add("clean");
        }
        goals.add("package");
        ProcessBuilder pb = new ProcessBuilder(goals);
        pb.directory(moduleDir.toFile());
        pb.redirectErrorStream(true);
        pb.redirectOutput(ProcessBuilder.Redirect.INHERIT);
        Process process = pb.start();
        int exit = process.waitFor();
        if (exit != 0) {
            throw new IllegalStateException("Building jar failed (exit " + exit + ") in " + moduleDir);
        }
        if (nameIsKnown && !Files.isRegularFile(jar)) {
            throw new IllegalStateException("Build succeeded but jar not found at " + jar);
        }
        return jar;
    }

    /**
     * The newest source strictly newer than the jar (so the rebuild note can NAME what
     * changed, as {@code _stale_trigger} does), or null when the jar is up to date. A
     * missing jar returns the jar path itself.
     */
    private static Path staleTrigger(Path jar, Path moduleDir, Path... extraSourceRoots) throws IOException {
        if (!Files.isRegularFile(jar)) {
            return jar;
        }
        // FileTime.compareTo compares at the filesystem's actual mtime resolution (sub-ms
        // on APFS/most modern filesystems) -- unlike toMillis(), which would silently
        // truncate and could miss a source written less than 1ms after the jar.
        FileTime newestTime = Files.getLastModifiedTime(jar);
        Path newest = null;

        List<Path> roots = new ArrayList<>();
        roots.add(moduleDir.resolve("src"));
        roots.addAll(List.of(extraSourceRoots));
        Path pom = moduleDir.resolve("pom.xml");

        for (Path root : roots) {
            if (!Files.isDirectory(root)) {
                continue;
            }
            List<Path> files;
            try (Stream<Path> walk = Files.walk(root)) {
                files = walk.filter(Files::isRegularFile).toList();
            }
            for (Path p : files) {
                FileTime time = Files.getLastModifiedTime(p);
                if (time.compareTo(newestTime) > 0) {
                    newest = p;
                    newestTime = time;
                }
            }
        }
        if (Files.isRegularFile(pom) && Files.getLastModifiedTime(pom).compareTo(newestTime) > 0) {
            newest = pom;
        }
        return newest;
    }
}
