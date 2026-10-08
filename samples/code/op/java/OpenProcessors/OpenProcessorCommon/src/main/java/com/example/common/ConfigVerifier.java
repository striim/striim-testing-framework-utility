package com.example.common;

import java.io.File;
import java.io.IOException;
import java.util.List;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.StreamReadFeature;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.json.JsonMapper;

/**
 * The body of every module's {@code ConfigCheck} main: verify a config file offline, report every
 * unknown key in one pass, and return a process exit code.
 *
 * <p>Shared rather than copied per module — near-identical copies drift, and their comments drift
 * first.</p>
 *
 * <p>Exit codes: <b>0</b> valid · <b>1</b> invalid config · <b>2</b> usage error or a file that
 * cannot be read. Malformed JSON counts as an invalid <em>config</em> (1), not an unreadable file
 * (2): a syntax error is the most common config mistake there is, and a CI gate that treats 2 as a
 * tool or infrastructure problem should not see it that way.</p>
 */
public final class ConfigVerifier {

    /**
     * Stricter than the parsers' own mappers, deliberately. Jackson's defaults accept trailing
     * content after the root value and silently let a duplicated key win, so a file with a valid
     * config followed by garbage — or one declaring {@code lookups} twice — verifies clean. Enabled
     * here only, so runtime parsing is unchanged.
     */
    private static final ObjectMapper MAPPER = JsonMapper.builder()
            .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
            .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
            .build();

    private ConfigVerifier() {
    }

    /**
     * @param productName used in the "not a &lt;product&gt; config" message, e.g.
     *                    {@code "MyOp"}
     * @param path        the config file to verify
     * @param parser      the module's parser, normally {@code OrmFactory::create}
     */
    public static int verify(String productName, String path, ConfigParser parser) {
        // An empty file, a top-level array or a bare scalar all parse into something the factories
        // tolerate (absent tables/lookups is legal), so without this the verifier greenlights
        // whatever file it was pointed at -- the likeliest operator mistake of all. Checked here,
        // not in create(), so runtime is unchanged.
        try {
            JsonNode root = MAPPER.readTree(new File(path));
            boolean empty = (root == null) || root.isMissingNode();
            if (empty || !root.isObject()) {
                System.err.println("Not a valid " + productName + " config: " + path);
                System.err.println(empty
                        ? "  file is empty; expected a JSON object at the root"
                        : "  expected a JSON object at the root, found " + root.getNodeType());
                return 1;
            }
        } catch (JsonProcessingException e) {
            System.err.println("INVALID: " + path);
            System.err.println("  not valid JSON: " + e.getOriginalMessage());
            return 1;
        } catch (IOException e) {
            System.err.println("Cannot read " + path + ": " + e.getMessage());
            return 2;
        }

        // Installing a collector puts ConfigKeys in gather-everything mode, so a config that sets
        // "rejectUnknownKeys": true is still fully reported rather than throwing at the first
        // offender. Running the verifier is an explicit request for the whole picture.
        ConfigIssues.install();
        try {
            parser.parse(path);
            List<String> issues = ConfigIssues.drain();
            if (issues.isEmpty()) {
                System.out.println("OK: " + path);
                return 0;
            }
            issues.forEach(System.err::println);
            // One issue per config LOCATION, and a location can name several keys, so this is not
            // a key count.
            System.err.println(issues.size() + " config location(s) with unknown keys in " + path);
            return 1;
        } catch (IllegalArgumentException e) {
            // Unknown keys found before the structural error still deserve reporting -- fixing only
            // the thrown error and re-running would be a wasted round trip.
            ConfigIssues.drain().forEach(System.err::println);
            System.err.println("INVALID: " + path);
            System.err.println("  " + e.getMessage());
            return 1;
        } catch (IOException e) {
            System.err.println("Cannot read " + path + ": " + e.getMessage());
            return 2;
        } catch (RuntimeException e) {
            // A parser is only contracted to throw IllegalArgumentException, but anything else
            // escaping would leave the CLI dying with a stack trace instead of a message and an exit
            // code. Defensive: no probe has triggered it.
            ConfigIssues.drain().forEach(System.err::println);
            System.err.println("INVALID: " + path);
            System.err.println("  " + e.getClass().getSimpleName() + ": " + e.getMessage());
            return 1;
        } finally {
            ConfigIssues.drain();
        }
    }
}
