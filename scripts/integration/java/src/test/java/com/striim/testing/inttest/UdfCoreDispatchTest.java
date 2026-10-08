package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;

import org.junit.jupiter.api.Test;

/**
 * Pure unit tests of {@link UdfCore#resolveMethod} against a purpose-built fixture class
 * reproducing the overload shapes real UDFs ship (an exact-arity
 * non-varargs sibling of a varargs method, a bare {@code Object...} varargs method, a
 * primitive-boolean method, and a {@code List} method) -- no jar, no classloader, no
 * {@code STRIIM_HOME} needed, since {@link UdfCore#resolveMethod} only reflects on
 * whatever {@link Class} it is handed.
 */
class UdfCoreDispatchTest {

    /** Static fixture mirroring real UDFs' overload shapes. */
    static final class Fixture {
        private Fixture() {
        }

        public static String f(String a) {
            return "1arg:" + a;
        }

        public static String f(String a, String b) {
            return "2arg:" + a + "," + b;
        }

        public static String f(String a, String b, String... rest) {
            return "vararg:" + a + "," + b + "," + String.join(",", rest);
        }

        public static String g(Object... items) {
            return "objvararg:" + items.length;
        }

        public static boolean h(boolean b) {
            return b;
        }

        public static String k(List<Object> list) {
            return "list:" + list.size();
        }

        public static String amb(Number n) {
            return "num";
        }

        public static String amb(Comparable<?> c) {
            return "cmp";
        }
    }

    @Test
    void exactArityNonVarargsPreferredOverVarargsForOneArg() {
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "f", new Object[] { "x" });
        assertFalse(call.varargs);
        assertEquals(1, call.method.getParameterCount());
    }

    @Test
    void exactArityNonVarargsPreferredOverVarargsForTwoArgs() {
        // (String,String) is a real overload of (String,String,String...) -- the two-arg
        // call must resolve to the concrete overload, not the varargs one with zero
        // trailing elements (this is exactly why WAPipeline/JSONPipeline ship a concrete
        // 2-/1-arg sibling: a CQ's generated code cannot synthesize an empty varargs array).
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "f", new Object[] { "x", "y" });
        assertFalse(call.varargs);
        assertEquals(2, call.method.getParameterCount());
    }

    @Test
    void threeArgsFallsThroughToVarargsPackingTheTail() throws Exception {
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "f", new Object[] { "x", "y", "z" });
        assertTrue(call.varargs);
        assertEquals(2, call.fixedCount);
        assertEquals(String.class, call.varargComponent);
        Object packed = java.lang.reflect.Array.newInstance(call.varargComponent, 1);
        java.lang.reflect.Array.set(packed, 0, "z");
        Object result = call.method.invoke(null, "x", "y", packed);
        assertEquals("vararg:x,y,z", result);
    }

    @Test
    void zeroVarargsElementsPacksAnEmptyArray() throws Exception {
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "g", new Object[0]);
        assertTrue(call.varargs);
        assertEquals(0, call.fixedCount);
        assertEquals(Object.class, call.varargComponent);
        Object packed = java.lang.reflect.Array.newInstance(call.varargComponent, 0);
        assertEquals("objvararg:0", call.method.invoke(null, (Object) packed));
    }

    @Test
    void booleanWideningResolvesThePrimitiveOverload() throws Exception {
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "h", new Object[] { Boolean.TRUE });
        assertFalse(call.varargs);
        assertEquals(Boolean.TRUE, call.method.invoke(null, Boolean.TRUE));
    }

    @Test
    void listParameterResolvesDirectly() throws Exception {
        UdfCore.ResolvedCall call = UdfCore.resolveMethod(Fixture.class, "k", new Object[] { List.of("a", "b", "c") });
        assertEquals("list:3", call.method.invoke(null, List.of("a", "b", "c")));
    }

    @Test
    void trueAmbiguityBetweenUnrelatedInterfacesThrows() {
        // Integer implements both Number and Comparable<Integer>; neither amb(Number) nor
        // amb(Comparable) is assignable-from the other, so neither is "no worse than" the
        // other -- a genuine tie, unlike a String/Object pair (String IS more specific).
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> UdfCore.resolveMethod(Fixture.class, "amb", new Object[] { 42 }));
        assertTrue(e.getMessage().contains("ambiguous"));
    }

    @Test
    void noCandidateAtAllNamesTheClassAndDeclaredMethods() {
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> UdfCore.resolveMethod(Fixture.class, "doesNotExist", new Object[] { "x" }));
        assertTrue(e.getMessage().contains("doesNotExist"));
        assertTrue(e.getMessage().contains(Fixture.class.getName()));
    }

    @Test
    void noOverloadAcceptsTheGivenArgsNamesCandidatesAndArgTypes() {
        // f() exists at arity 1/2/3+ but never with an int -- Phase A finds nothing
        // (wrong argument type) and Phase B has no varargs candidate to fall back to since
        // f's varargs form requires at least 2 leading String args.
        IllegalStateException e = assertThrows(IllegalStateException.class,
                () -> UdfCore.resolveMethod(Fixture.class, "f", new Object[] { 1 }));
        assertTrue(e.getMessage().contains("f"));
        assertTrue(e.getMessage().contains("candidates"));
    }

    @Test
    void acceptsHandlesNullOnlyForNonPrimitiveParams() {
        assertTrue(UdfCore.accepts(String.class, null));
        assertFalse(UdfCore.accepts(int.class, null));
    }

    @Test
    void acceptsWideningTable() {
        assertTrue(UdfCore.accepts(long.class, 5));      // int -> long
        assertTrue(UdfCore.accepts(double.class, 5));     // int -> double
        assertFalse(UdfCore.accepts(int.class, 5L));      // long does NOT narrow to int
        assertTrue(UdfCore.accepts(Object.class, "s"));
        assertFalse(UdfCore.accepts(String.class, 5));
    }
}
