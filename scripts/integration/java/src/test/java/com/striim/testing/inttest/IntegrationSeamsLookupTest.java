package com.striim.testing.inttest;

import java.net.URL;
import java.net.URLClassLoader;
import java.util.LinkedHashMap;
import java.util.Map;

import com.striim.testing.inttest.seamfixtures.ModuleSeam;
import com.striim.testing.inttest.seamfixtures.good.IntegrationSeams.RealSeam;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * the harness asks the op for seams it cannot invent.
 *
 * <p>Each fixture below stands in for a real op's {@code IntegrationSeams}. They live in this
 * package so the lookup can find them by name through the test classloader — the production path
 * resolves the same convention out of the op jar's child loader instead, which is the only
 * difference.</p>
 */
class IntegrationSeamsLookupTest {

    private static final String GOOD = "com.striim.testing.inttest.seamfixtures.good";
    private static final String WRONG_TYPE = "com.striim.testing.inttest.seamfixtures.wrongtype";
    private static final String WRONG_SHAPE = "com.striim.testing.inttest.seamfixtures.wrongshape";
    private static final String THROWING = "com.striim.testing.inttest.seamfixtures.throwing";
    private static final String NULL_PRIMITIVE =
            "com.striim.testing.inttest.seamfixtures.nullprimitive";

    private static URLClassLoader thisLoader() {
        return new URLClassLoader(new URL[0], IntegrationSeamsLookupTest.class.getClassLoader());
    }

    private static Map<String, Object> props() {
        Map<String, Object> props = new LinkedHashMap<String, Object>();
        props.put("ProjectId", "p");
        return props;
    }

    private static Object lookup(String corePackage, Class<?> paramType) {
        return IntegrationSeamsLookup.seamFor(corePackage, paramType, props(), thisLoader(),
                corePackage + ".Processor");
    }

    @Test
    void theOpSuppliesTheSeamAndSeesTheCasesProperties() {
        Object seam = lookup(GOOD, ModuleSeam.class);

        assertTrue(seam instanceof RealSeam, "the op's own instance must come back: " + seam);
        assertEquals("p", ((RealSeam) seam).sawProperties.get("ProjectId"),
                "the case's properties must reach the op — a seam usually needs them to decide"
                        + " what to build");
        assertEquals(ModuleSeam.class.getName(),
                com.striim.testing.inttest.seamfixtures.good.IntegrationSeams.lastRequestedType,
                "the op is told which parameter type is being asked for, by full name");
    }

    /**
     * null is an ANSWER, not a failure. A reader's recovered-position parameter is null on a
     * first run and the harness cannot know that on the op's behalf.
     */
    @Test
    void nullIsAcceptedAsADeliberateAnswer() {
        assertNull(lookup(GOOD, CharSequence.class),
                "returning null must pass null through, not be treated as 'no seam available'");
    }

    @Test
    void anOpWithNoSeamsClassFailsNamingTheConvention() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> lookup("com.example.nosuchop", ModuleSeam.class));

        String message = thrown.getMessage();
        assertTrue(message.contains("com.example.nosuchop.IntegrationSeams"),
                "the message must name the class the op is expected to ship: " + message);
        assertTrue(message.contains("seamFor"),
                "and the method signature it must have: " + message);
    }

    /** A seam of the wrong type would otherwise surface as an opaque newInstance failure. */
    @Test
    void aSeamOfTheWrongTypeIsRejectedWhereItIsProduced() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> lookup(WRONG_TYPE, ModuleSeam.class));

        assertTrue(thrown.getMessage().contains("java.lang.String"),
                "the message must name what came back: " + thrown.getMessage());
        assertTrue(thrown.getMessage().contains("ModuleSeam"),
                "and what was asked for: " + thrown.getMessage());
    }

    @Test
    void aSeamsClassWithTheWrongSignatureSaysSo() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> lookup(WRONG_SHAPE, ModuleSeam.class));

        assertTrue(thrown.getMessage().contains("no static seamFor(String, Map)"),
                "the message must state the exact signature expected: " + thrown.getMessage());
    }

    @Test
    void anOpsOwnFailureSurfacesWithItsMessage() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> lookup(THROWING, ModuleSeam.class));

        assertSame(IllegalArgumentException.class, thrown.getCause().getClass());
        assertEquals("scripted DAO needs a Tables property", thrown.getCause().getMessage(),
                "the op's own diagnosis must survive, not be flattened into a reflection error");
    }

    /**
     * A primitive parameter: {@code int.class.isInstance(Integer.valueOf(7))} is FALSE, so a naive
     * {@code isInstance} check rejects a value {@code newInstance} would have unboxed happily.
     */
    @Test
    void aPrimitiveParameterIsSatisfiedByItsWrapper() {
        assertEquals(Integer.valueOf(7), lookup(GOOD, int.class),
                "a primitive param must accept its wrapper; isInstance() alone never does");
    }

    /** The mirror: null for a primitive would sail through to an opaque newInstance failure. */
    @Test
    void nullForAPrimitiveIsRejectedRatherThanPassedOn() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> lookup(NULL_PRIMITIVE, int.class));

        assertTrue(thrown.getMessage().contains("primitive parameter type int"),
                "the message must name the primitive: " + thrown.getMessage());
    }

    /**
     * The seam must see the SAME view of the case the core does — reserved keys present, and a
     * defensive copy so a mutating seam cannot change what the core then receives.
     */
    @Test
    void theSeamSeesTheEnrichedPropertiesTheCoreGets() throws Exception {
        Map<String, Object> caseProps = props();
        Object seam = IntegrationProcessor.resolveConstructorArgument(
                GOOD + ".Processor", ModuleSeam.class, caseProps, Map.of(), Map.of(), Map.of(),
                thisLoader(), null, "ns", "src");

        Map<String, Object> seen =
                com.striim.testing.inttest.seamfixtures.good.IntegrationSeams.lastProperties;
        assertTrue(seam instanceof RealSeam);
        assertEquals("ns", seen.get("striim.op.namespace"),
                "the seam must see the reserved keys the core sees");
        assertEquals("src", seen.get("striim.op.sourceName"));
        assertTrue(seen != caseProps, "and a copy, so a mutating seam cannot alter the core's view");
    }

    /**
     * Finding 4: the routing itself. Everything above calls the lookup directly; this drives
     * resolveConstructorArgument's default branch, which derives the core package from the class
     * name — the wiring no other test touched.
     */
    @Test
    void anUnknownParameterTypeRoutesThroughResolveConstructorArgument() throws Exception {
        Object seam = IntegrationProcessor.resolveConstructorArgument(
                GOOD + ".Processor", ModuleSeam.class, props(), Map.of(), Map.of(), Map.of(),
                thisLoader(), null, null, null);

        assertTrue(seam instanceof RealSeam,
                "an unknown type must reach the op's IntegrationSeams, not the old hard failure");
    }

    @Test
    void aCoreClassWithNoPackageSaysSoRatherThanThrowingFromSubstring() {
        IllegalStateException thrown = assertThrows(IllegalStateException.class,
                () -> IntegrationProcessor.corePackageOf("Processor"));

        assertTrue(thrown.getMessage().contains("no package"),
                "a bare class name must be diagnosed, not StringIndexOutOfBounds: "
                        + thrown.getMessage());
    }
}
