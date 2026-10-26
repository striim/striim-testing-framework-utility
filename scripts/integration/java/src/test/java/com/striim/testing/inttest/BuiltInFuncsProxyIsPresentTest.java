package com.striim.testing.inttest;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URL;
import java.net.URLClassLoader;
import java.util.Map;

import com.webaction.proc.events.WAEvent;
import org.junit.jupiter.api.Test;

/** The proxy's IS_PRESENT is the platform static, as production's BuiltInFuncResolver delegates. */
class BuiltInFuncsProxyIsPresentTest {

    public interface Funcs {
        boolean IS_PRESENT(WAEvent event, Object[] image, int index);
    }

    private static Funcs funcs() {
        URLClassLoader loader = new URLClassLoader(new URL[0], Funcs.class.getClassLoader());
        return (Funcs) IntegrationProcessor.newBuiltInFuncsProxy(Funcs.class, Map.of(), loader);
    }

    @Test
    void matchesTheRealBody() {
        WAEvent e = new WAEvent(9, null);
        e.data = new Object[9];
        e.setData(8, "v");
        Funcs f = funcs();
        assertTrue(f.IS_PRESENT(e, e.data, 8));
        assertFalse(f.IS_PRESENT(e, e.data, 7));
        assertFalse(f.IS_PRESENT(e, e.data.clone(), 8));
        assertThrows(ArrayIndexOutOfBoundsException.class, () -> f.IS_PRESENT(e, e.data, 14));
    }
}
