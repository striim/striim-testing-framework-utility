package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import java.util.jar.Attributes;
import java.util.jar.JarEntry;
import java.util.jar.JarOutputStream;
import java.util.jar.Manifest;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** main() writes the errorFile for an op's Error too, not only for an Exception. */
class MainFailureFileTest {

    @Test
    void anErrorTheOpThrowsReachesTheErrorFile(@TempDir Path tempDir) throws Exception {
        Path log = drive(tempDir, buildJar(tempDir, "static { if (true) throw new AssertionError(\"refused at load\"); }"));
        Path error = tempDir.resolve("error.txt");
        assertTrue(Files.exists(error), Files.readString(log));
        assertTrue(Files.readString(error).contains("refused at load"), Files.readString(error));
    }

    @Test
    void theJvmExitsEvenWhenReportingTheFailureThrowsAnError(@TempDir Path tempDir) throws Exception {
        // A cause whose toString() throws while main reports it (never while the harness runs);
        // a live non-daemon thread then keeps the JVM up unless the exit is unconditional.
        drive(tempDir, buildJar(tempDir, """
                static {
                    new Sleeper().start();
                    if (true) throw new Bad();
                }

                // Its own class: a lambda's body lives on Processor and could not run after its failed init.
                static final class Sleeper extends Thread {
                    @Override public void run() {
                        try { Thread.sleep(600_000); } catch (InterruptedException e) { }
                    }
                }

                static final class Bad extends RuntimeException {
                    @Override public String toString() {
                        boolean reporting = StackWalker.getInstance().walk(frames -> frames
                                .filter(f -> f.getClassName().endsWith(".IntegrationProcessor"))
                                .map(StackWalker.StackFrame::getMethodName)
                                .allMatch("main"::equals));
                        if (reporting) {
                            throw new StackOverflowError("toString");
                        }
                        return "Bad";
                    }
                }
                """));
    }

    /** Runs main in a child JVM; asserts it exits 1 within 60s. Returns its combined output. */
    private static Path drive(Path tempDir, File jar) throws Exception {
        Path input = tempDir.resolve("input.json");
        Files.writeString(input, "[{\"metadata\": {\"TableName\": \"T\"}}]");
        Path error = tempDir.resolve("error.txt");
        Path request = tempDir.resolve("request.json");
        Files.writeString(request, "{\"opJar\": \"" + jar.getAbsolutePath() + "\", \"inputFile\": \"" + input
                + "\", \"outputFile\": \"" + tempDir.resolve("out.json") + "\", \"errorFile\": \"" + error + "\"}");

        Path log = tempDir.resolve("log.txt");
        Process p = new ProcessBuilder(Path.of(System.getProperty("java.home"), "bin", "java").toString(),
                "-cp", System.getProperty("java.class.path"),
                "com.striim.testing.inttest.IntegrationProcessor", request.toString())
                .redirectErrorStream(true).redirectOutput(log.toFile()).start();
        boolean exited = p.waitFor(60, TimeUnit.SECONDS);
        if (!exited) {
            p.destroyForcibly();
        }
        assertTrue(exited, "the JVM did not exit: " + Files.readString(log));
        assertEquals(1, p.exitValue(), Files.readString(log));
        return log;
    }

    private static File buildJar(Path tempDir, String member) throws IOException {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        assumeTrue(compiler != null, "No system Java compiler available");
        Path pkg = tempDir.resolve("src/com/example/errorop");
        Path classes = tempDir.resolve("classes");
        Files.createDirectories(pkg);
        Files.createDirectories(classes);
        Path source = pkg.resolve("Processor.java");
        Files.writeString(source, """
                package com.example.errorop;

                import java.util.List;
                import java.util.Map;

                import com.webaction.proc.events.WAEvent;

                public class Processor {
                    %s

                    public Processor(Map<String, Object> props) {
                    }

                    public List<WAEvent> processEvent(WAEvent e) {
                        return List.of(e);
                    }
                }
                """.formatted(member));
        if (compiler.run(null, null, null, "-cp", System.getProperty("java.class.path"),
                "-d", classes.toString(), source.toString()) != 0) {
            throw new IllegalStateException("compiling the stub failed");
        }
        Path jar = tempDir.resolve("errorop.jar");
        Manifest manifest = new Manifest();
        manifest.getMainAttributes().put(Attributes.Name.MANIFEST_VERSION, "1.0");
        manifest.getMainAttributes().putValue("Striim-Service-Implementation", "com.example.errorop.App");
        try (JarOutputStream out = new JarOutputStream(Files.newOutputStream(jar), manifest);
                var classFiles = Files.list(classes.resolve("com/example/errorop"))) {
            for (Path classFile : (Iterable<Path>) classFiles::iterator) {
                out.putNextEntry(new JarEntry("com/example/errorop/" + classFile.getFileName()));
                Files.copy(classFile, out);
                out.closeEntry();
            }
        }
        return jar.toFile();
    }
}
